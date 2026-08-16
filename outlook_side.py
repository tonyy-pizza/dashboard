"""Outlook (Microsoft Graph) side of the sync — primary calendar, read/write.

Auth is delegated OAuth2 against an Azure AD app registration in the Agrotek
tenant, using MSAL's device-code flow once and a cached refresh token
thereafter, so scheduled runs are unattended. A public client (no client
secret) is deliberate: there is nowhere safe to keep a secret on a desktop,
and delegated Calendars.ReadWrite doesn't need one.

Recurring events are read through /me/calendarView, which returns
materialized occurrences rather than the recurring series — the same
simplification the Google side makes with singleEvents=True.

As on the Google side, the conversion functions are pure and tested; the
client is a thin shell over HTTP.
"""

import datetime as dt
import html
import re

import requests

from paths import GRAPH_APP_PATH, GRAPH_TOKEN_CACHE_PATH
from storage import atomic_write_text, load_json
from sync_model import (
    FINGERPRINT_KEY, GOOGLE, ID_KEY, OUTLOOK, SOURCE_KEY, SyncEvent,
    parse_utc, render_description, utc_iso,
)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPES = ["Calendars.ReadWrite"]
PAGE_SIZE = 100
TIMEOUT = 30

# PS_PUBLIC_STRINGS — the namespace for named string properties on Graph.
_PROPERTY_NAMESPACE = "String {00020329-0000-0000-C000-000000000046} Name "

PIP_HINT = "pip install msal"

SETUP_STEPS = [
    "portal.azure.com → Microsoft Entra ID → App registrations → New registration",
    "Accounts in this organizational directory only; no redirect URI needed",
    "Authentication → Advanced → Allow public client flows: Yes",
    "API permissions → Microsoft Graph → Delegated → Calendars.ReadWrite",
    "API permissions → Grant admin consent for Agrotek",
    f'Overview → copy the app + tenant ids into {GRAPH_APP_PATH} as '
    '{"client_id": "...", "tenant_id": "..."}',
    "run: py calendar_sync.py --auth-outlook   (prints a code to enter once)",
]


class OutlookSetupNeeded(Exception):
    """App registration details missing, or MSAL isn't installed."""


def property_id(name: str) -> str:
    return _PROPERTY_NAMESPACE + name


MARKER_IDS = [property_id(k) for k in (SOURCE_KEY, ID_KEY, FINGERPRINT_KEY)]
MARKER_FILTER = " or ".join(f"id eq '{pid}'" for pid in MARKER_IDS)

SELECT_FIELDS = ("id,subject,body,bodyPreview,location,start,end,isAllDay,"
                 "lastModifiedDateTime,attendees,isCancelled,type")


# ── conversion ───────────────────────────────────────────────────────

def event_from_graph(item: dict) -> SyncEvent:
    markers = _read_markers(item.get("singleValueExtendedProperties"))
    all_day = bool(item.get("isAllDay"))
    return SyncEvent(
        uid=item.get("id", ""),
        summary=item.get("subject") or "",
        description=_read_body(item),
        location=((item.get("location") or {}).get("displayName") or ""),
        start=_read_edge(item.get("start"), all_day),
        end=_read_edge(item.get("end"), all_day),
        all_day=all_day,
        attendees=_read_attendees(item.get("attendees")),
        updated=parse_utc(item.get("lastModifiedDateTime")),
        source=markers.get(SOURCE_KEY),
        source_id=markers.get(ID_KEY),
        base_fingerprint=markers.get(FINGERPRINT_KEY),
    )


def _read_edge(edge, all_day: bool) -> str:
    """Graph dates are {dateTime, timeZone}. All-day events are midnight to
    midnight, and Graph's end is exclusive — same convention Google uses for
    its all-day `date`, so both sides store the exclusive end."""
    if not edge:
        return ""
    raw = edge.get("dateTime") or ""
    zone = edge.get("timeZone") or "UTC"
    if all_day:
        return raw[:10]
    moment = parse_utc(raw if zone.upper() == "UTC" else raw + "Z")
    return utc_iso(moment) if moment else ""


def _read_body(item: dict) -> str:
    """Outlook stores most bodies as HTML. We always write plain text, but an
    event created in Outlook itself won't be, so flatten it — otherwise the
    mirror would carry markup and never compare equal."""
    body = item.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        return _strip_html(content)
    return content or (item.get("bodyPreview") or "")


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text).replace(" ", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_attendees(attendees):
    out = []
    for attendee in attendees or []:
        address = attendee.get("emailAddress") or {}
        email = (address.get("address") or "").strip()
        name = (address.get("name") or "").strip()
        if name and email and name.casefold() != email.casefold():
            out.append(f"{name} <{email}>")
        elif email or name:
            out.append(email or name)
    return tuple(out)


def _read_markers(properties) -> dict:
    found = {}
    for prop in properties or []:
        pid = prop.get("id") or ""
        if pid.startswith(_PROPERTY_NAMESPACE):
            found[pid[len(_PROPERTY_NAMESPACE):]] = prop.get("value")
    return found


def graph_body(event: SyncEvent, source=None, source_id=None, fingerprint=None) -> dict:
    """The POST/PATCH payload. No `attendees` key: adding real attendees would
    make Outlook send meeting invitations from a mirrored copy."""
    body = {
        "subject": event.summary or "(no title)",
        "body": {"contentType": "text", "content": render_description(event)},
        "location": {"displayName": event.location or ""},
        "isAllDay": event.all_day,
        "start": _write_edge(event.start, event.all_day),
        "end": _write_edge(event.end, event.all_day),
    }
    markers = marker_properties(source, source_id, fingerprint)
    if markers:
        body["singleValueExtendedProperties"] = markers
    return body


def _write_edge(value: str, all_day: bool) -> dict:
    if all_day:
        return {"dateTime": f"{value[:10]}T00:00:00.0000000", "timeZone": "UTC"}
    return {"dateTime": (value or "").replace("Z", ""), "timeZone": "UTC"}


def marker_properties(source, source_id, fingerprint) -> list:
    pairs = ((SOURCE_KEY, source), (ID_KEY, source_id), (FINGERPRINT_KEY, fingerprint))
    return [{"id": property_id(key), "value": value}
            for key, value in pairs if value]


# ── client ───────────────────────────────────────────────────────────

class OutlookSide:
    name = OUTLOOK
    other = GOOGLE

    def __init__(self, session=None, token=None):
        self._session = session or requests.Session()
        self._token = token

    # -- auth ---------------------------------------------------------
    @staticmethod
    def _msal():
        try:
            import msal
        except ImportError as e:
            raise OutlookSetupNeeded(f"{e} — {PIP_HINT}")
        return msal

    @staticmethod
    def app_details():
        details = load_json(GRAPH_APP_PATH, {})
        client_id = (details.get("client_id") or "").strip()
        tenant_id = (details.get("tenant_id") or "").strip()
        if not client_id or not tenant_id:
            raise OutlookSetupNeeded(
                f"missing client_id/tenant_id in {GRAPH_APP_PATH}")
        return client_id, tenant_id

    @classmethod
    def _application(cls, msal):
        client_id, tenant_id = cls.app_details()
        cache = msal.SerializableTokenCache()
        if GRAPH_TOKEN_CACHE_PATH.exists():
            try:
                cache.deserialize(GRAPH_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=cache,
        )
        return app, cache

    @classmethod
    def access_token(cls, interactive=False) -> str:
        msal = cls._msal()
        app, cache = cls._application(msal)

        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])

        if not result and interactive:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise OutlookSetupNeeded(
                    f"device flow refused: {flow.get('error_description', '')}")
            print(flow["message"])          # the code to type; carries no secret
            result = app.acquire_token_by_device_flow(flow)

        if not result or "access_token" not in result:
            if not interactive:
                raise OutlookSetupNeeded(
                    "no Outlook token yet — run: py calendar_sync.py --auth-outlook")
            raise OutlookSetupNeeded(
                f"sign-in failed: {(result or {}).get('error_description', 'unknown')}")

        if cache.has_state_changed:
            atomic_write_text(GRAPH_TOKEN_CACHE_PATH, cache.serialize())
        return result["access_token"]

    @classmethod
    def authorize(cls) -> str:
        cls.access_token(interactive=True)
        return f"[outlook] Authorized. Token cached at {GRAPH_TOKEN_CACHE_PATH}"

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self.access_token()
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Ask Graph to hand back times already in UTC.
            "Prefer": 'outlook.timezone="UTC"',
        }

    def _request(self, method, url, **kwargs):
        response = self._session.request(
            method, url, headers=self._headers(), timeout=TIMEOUT, **kwargs)
        if response.status_code >= 400:
            # Graph puts the useful part in the body; the token is in the
            # headers and never gets logged.
            raise RuntimeError(
                f"Graph {method} {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    # -- reads --------------------------------------------------------
    def list_events(self, window_start: dt.datetime, window_end: dt.datetime) -> list:
        params = {
            "startDateTime": utc_iso(window_start),
            "endDateTime": utc_iso(window_end),
            "$select": SELECT_FIELDS,
            "$expand": f"singleValueExtendedProperties($filter={MARKER_FILTER})",
            "$top": PAGE_SIZE,
        }
        url = f"{GRAPH_ROOT}/me/calendarView"      # expands recurring series
        events = []
        while url:
            payload = self._request("GET", url, params=params)
            params = None                          # nextLink carries its own
            for item in payload.get("value", []):
                if item.get("isCancelled"):
                    continue
                events.append(event_from_graph(item))
            url = payload.get("@odata.nextLink")
        return events

    # -- writes -------------------------------------------------------
    def create(self, event: SyncEvent, source, source_id, fingerprint) -> str:
        created = self._request(
            "POST", f"{GRAPH_ROOT}/me/events",
            json=graph_body(event, source, source_id, fingerprint))
        return created.get("id", "")

    def update(self, uid: str, event: SyncEvent) -> None:
        self._request("PATCH", f"{GRAPH_ROOT}/me/events/{uid}",
                      json=graph_body(event))

    def set_markers(self, uid: str, source, source_id, fingerprint) -> None:
        self._request("PATCH", f"{GRAPH_ROOT}/me/events/{uid}", json={
            "singleValueExtendedProperties":
                marker_properties(source, source_id, fingerprint)})

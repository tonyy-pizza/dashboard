"""Google Calendar side of the sync — primary calendar, read/write.

Separate credentials from anything else in this project: the dashboard's
Calendar panel reads a public-ish ICS feed and needs no OAuth at all, while
this needs the full read/write `calendar` scope. Its own client secret, its
own token file.

Recurring events are never handled as series. `singleEvents=True` asks Google
to expand them into individual occurrences, which is what gets mirrored.

The conversion functions at the top are pure and carry the actual risk
(all-day boundaries, timezone normalization), so they're unit-tested; the
client below is a thin shell around the API.
"""

import datetime as dt

from paths import GOOGLE_SYNC_CREDENTIALS_PATH, GOOGLE_SYNC_TOKEN_PATH
from sync_model import (
    FINGERPRINT_KEY, GOOGLE, ID_KEY, OUTLOOK, SOURCE_KEY, SyncEvent,
    parse_utc, render_description, utc_iso,
)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = "primary"
PAGE_SIZE = 2500

PIP_HINT = "pip install google-api-python-client google-auth-oauthlib"

SETUP_STEPS = [
    "console.cloud.google.com → project → enable Google Calendar API",
    "OAuth consent screen → External → add your account as a test user",
    "Credentials → Create OAuth client ID → Desktop app",
    f"download it to {GOOGLE_SYNC_CREDENTIALS_PATH}",
    "run: py calendar_sync.py --auth-google",
]


class GoogleSetupNeeded(Exception):
    """Credentials missing, or the client libraries aren't installed."""


# ── conversion ───────────────────────────────────────────────────────

def event_from_google(item: dict) -> SyncEvent:
    start, all_day = _read_edge(item.get("start") or {})
    end, _ = _read_edge(item.get("end") or {})
    private = ((item.get("extendedProperties") or {}).get("private") or {})
    return SyncEvent(
        uid=item.get("id", ""),
        summary=item.get("summary") or "",
        description=item.get("description") or "",
        location=item.get("location") or "",
        start=start,
        end=end,
        all_day=all_day,
        attendees=_read_attendees(item.get("attendees")),
        updated=parse_utc(item.get("updated")),
        source=private.get(SOURCE_KEY),
        source_id=private.get(ID_KEY),
        base_fingerprint=private.get(FINGERPRINT_KEY),
    )


def _read_edge(edge: dict):
    """Google gives all-day events a `date` and timed events a `dateTime`."""
    if edge.get("date"):
        return edge["date"], True
    moment = parse_utc(edge.get("dateTime"))
    return (utc_iso(moment) if moment else ""), False


def _read_attendees(attendees):
    out = []
    for attendee in attendees or []:
        if attendee.get("self") and attendee.get("organizer"):
            continue
        email = (attendee.get("email") or "").strip()
        name = (attendee.get("displayName") or "").strip()
        if name and email:
            out.append(f"{name} <{email}>")
        elif email or name:
            out.append(email or name)
    return tuple(out)


def google_body(event: SyncEvent, source=None, source_id=None, fingerprint=None) -> dict:
    """The insert/patch payload. Attendees are deliberately absent — they ride
    along inside the description so the copy never sends invitations."""
    body = {
        "summary": event.summary or "(no title)",
        "description": render_description(event),
        "location": event.location or "",
        "start": _write_edge(event.start, event.all_day),
        "end": _write_edge(event.end, event.all_day),
    }
    private = _markers(source, source_id, fingerprint)
    if private:
        body["extendedProperties"] = {"private": private}
    return body


def _write_edge(value: str, all_day: bool) -> dict:
    if all_day:
        return {"date": value}
    return {"dateTime": value.replace("Z", "+00:00"), "timeZone": "UTC"}


def _markers(source, source_id, fingerprint) -> dict:
    markers = {}
    if source:
        markers[SOURCE_KEY] = source
    if source_id:
        markers[ID_KEY] = source_id
    if fingerprint:
        markers[FINGERPRINT_KEY] = fingerprint
    return markers


# ── client ───────────────────────────────────────────────────────────

class GoogleSide:
    name = GOOGLE
    other = OUTLOOK

    def __init__(self, service=None):
        self._service = service

    # -- auth ---------------------------------------------------------
    @staticmethod
    def _modules():
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as e:
            raise GoogleSetupNeeded(f"{e} — {PIP_HINT}")
        return Request, Credentials, InstalledAppFlow, build

    @classmethod
    def credentials(cls, interactive=False):
        Request, Credentials, InstalledAppFlow, _ = cls._modules()
        creds = None
        if GOOGLE_SYNC_TOKEN_PATH.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(GOOGLE_SYNC_TOKEN_PATH), SCOPES)
            except (ValueError, OSError):
                creds = None

        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                cls._store(creds)
                return creds
            except Exception:
                creds = None       # refresh token revoked, start over

        if not interactive:
            raise GoogleSetupNeeded(
                "no Google token yet — run: py calendar_sync.py --auth-google")
        if not GOOGLE_SYNC_CREDENTIALS_PATH.exists():
            raise GoogleSetupNeeded(f"missing {GOOGLE_SYNC_CREDENTIALS_PATH}")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(GOOGLE_SYNC_CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
        cls._store(creds)
        return creds

    @staticmethod
    def _store(creds):
        GOOGLE_SYNC_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOOGLE_SYNC_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    @classmethod
    def authorize(cls) -> str:
        cls.credentials(interactive=True)
        return f"[google] Authorized. Token cached at {GOOGLE_SYNC_TOKEN_PATH}"

    @property
    def service(self):
        if self._service is None:
            _, _, _, build = self._modules()
            self._service = build("calendar", "v3",
                                  credentials=self.credentials(),
                                  cache_discovery=False)
        return self._service

    # -- reads --------------------------------------------------------
    def list_events(self, window_start: dt.datetime, window_end: dt.datetime) -> list:
        events, page_token = [], None
        while True:
            response = self.service.events().list(
                calendarId=CALENDAR_ID,
                timeMin=utc_iso(window_start),
                timeMax=utc_iso(window_end),
                singleEvents=True,          # expand recurring series
                showDeleted=False,
                maxResults=PAGE_SIZE,
                pageToken=page_token,
            ).execute()
            for item in response.get("items", []):
                if item.get("status") == "cancelled":
                    continue
                events.append(event_from_google(item))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return events

    # -- writes -------------------------------------------------------
    def create(self, event: SyncEvent, source, source_id, fingerprint) -> str:
        created = self.service.events().insert(
            calendarId=CALENDAR_ID,
            body=google_body(event, source, source_id, fingerprint),
            sendUpdates="none",             # never mail anyone about a mirror
        ).execute()
        return created.get("id", "")

    def update(self, uid: str, event: SyncEvent) -> None:
        self.service.events().patch(
            calendarId=CALENDAR_ID, eventId=uid,
            body=google_body(event), sendUpdates="none",
        ).execute()

    def set_markers(self, uid: str, source, source_id, fingerprint) -> None:
        self.service.events().patch(
            calendarId=CALENDAR_ID, eventId=uid,
            body={"extendedProperties": {
                "private": _markers(source, source_id, fingerprint)}},
            sendUpdates="none",
        ).execute()

"""Google Calendar — read-only, primary calendar, current week.

Runs inside collector.py so the widget keeps its single data source
(cache.json) and its no-polling contract: the widget never talks to Google.

Setup is a one-time job in the Google Cloud Console — see SETUP_STEPS below.
Until credentials.json exists this module returns a `needs_setup` payload that
the Calendar panel renders as instructions rather than an error, so the rest
of the dashboard is unaffected.
"""

import datetime as dt
from pathlib import Path

from paths import GOOGLE_CREDENTIALS_PATH, GOOGLE_TOKEN_PATH

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

SETUP_STEPS = [
    "console.cloud.google.com → new project",
    "APIs & Services → enable 'Google Calendar API'",
    "OAuth consent screen → External → add your account as a test user",
    "Credentials → Create OAuth client ID → Desktop app",
    f"download the JSON as {GOOGLE_CREDENTIALS_PATH}",
    "run: py collector.py --auth   (opens a browser once)",
]

PIP_HINT = "pip install google-api-python-client google-auth-oauthlib"


class CalendarSetupNeeded(Exception):
    """Raised when the OAuth setup Joey has to do by hand isn't done yet."""


def week_bounds(today=None):
    """(Monday, Sunday) of the week containing `today`."""
    today = today or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)


def _google_modules():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise CalendarSetupNeeded(f"{e} — {PIP_HINT}")
    return Request, Credentials, InstalledAppFlow, build


def load_credentials(interactive: bool = False):
    """Cached token if it's good, refreshed if it's stale, and — only when
    `interactive` — the browser consent flow that creates it in the first
    place. The collector runs headless under pythonw, so the interactive path
    is reserved for `py collector.py --auth`."""
    Request, Credentials, InstalledAppFlow, _ = _google_modules()

    creds = None
    if Path(GOOGLE_TOKEN_PATH).exists():
        try:
            creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), SCOPES)
        except (ValueError, OSError):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            creds = None  # refresh token revoked — fall through to re-auth

    if not interactive:
        raise CalendarSetupNeeded(
            f"no usable OAuth token at {GOOGLE_TOKEN_PATH} — run: py collector.py --auth")

    if not Path(GOOGLE_CREDENTIALS_PATH).exists():
        raise CalendarSetupNeeded(f"missing {GOOGLE_CREDENTIALS_PATH}")

    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds) -> None:
    path = Path(GOOGLE_TOKEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def authorize() -> str:
    """One-time interactive consent. Returns a human-readable result line."""
    load_credentials(interactive=True)
    return f"[calendar] Authorized. Token cached at {GOOGLE_TOKEN_PATH}"


def collect_calendar(today=None) -> dict:
    """This week's events off the primary calendar, shaped for the week grid.

    Never raises: setup problems and API failures come back as a payload the
    panel can render.
    """
    monday, sunday = week_bounds(today)
    payload = {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "days": [{"date": (monday + dt.timedelta(days=i)).isoformat(), "events": []}
                 for i in range(7)],
    }

    try:
        _, _, _, build = _google_modules()
        creds = load_credentials(interactive=False)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        # Local midnight to local midnight, sent with the machine's UTC offset.
        tz = dt.datetime.now().astimezone().tzinfo
        time_min = dt.datetime.combine(monday, dt.time.min, tzinfo=tz)
        time_max = dt.datetime.combine(sunday + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,      # expand recurring events into instances
            orderBy="startTime",
            maxResults=250,
        ).execute()
    except CalendarSetupNeeded as e:
        print(f"[calendar] Setup needed: {e}")
        payload.update(error=str(e), needs_setup=True, setup_steps=SETUP_STEPS)
        return payload
    except Exception as e:
        print(f"[calendar] Failed: {e}")
        payload.update(error=str(e))
        return payload

    by_date = {day["date"]: day["events"] for day in payload["days"]}
    for item in result.get("items", []):
        for date_key, event in _spread_event(item, monday, sunday):
            if date_key in by_date:
                by_date[date_key].append(event)

    for events in by_date.values():
        events.sort(key=lambda e: (not e["all_day"], e["start"] or ""))

    print(f"[calendar] OK — {sum(len(v) for v in by_date.values())} events this week")
    return payload


def _spread_event(item, monday, sunday):
    """One calendar item → (iso date, event dict) per day it covers inside the
    week. An all-day Google event's `end.date` is exclusive."""
    summary = item.get("summary", "(no title)")
    location = item.get("location", "")
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})

    if start_raw.get("date"):
        start_date = _parse_date(start_raw["date"])
        end_date = _parse_date(end_raw.get("date")) or start_date
        if start_date is None:
            return
        last = max(start_date, end_date - dt.timedelta(days=1))
        day = max(start_date, monday)
        while day <= min(last, sunday):
            yield day.isoformat(), {"summary": summary, "location": location,
                                    "start": None, "end": None, "all_day": True}
            day += dt.timedelta(days=1)
        return

    start = _parse_datetime(start_raw.get("dateTime"))
    end = _parse_datetime(end_raw.get("dateTime"))
    if start is None:
        return
    yield start.date().isoformat(), {
        "summary": summary,
        "location": location,
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M") if end else None,
        "all_day": False,
    }


def _parse_date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value):
    if not value:
        return None
    try:
        # Google sends RFC3339; fromisoformat handles the offset form, and
        # 'Z' needs swapping out on Python < 3.11.
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None

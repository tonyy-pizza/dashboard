"""Calendar — read-only week view built from a secret iCal URL.

Replaces the Google Calendar API path entirely: no OAuth, no consent screen,
no credentials.json/token.json, no google-* packages. The collector just
fetches one .ics file over HTTPS and parses it, which means this works with
any provider that publishes a private iCal address (Google, Fastmail, iCloud,
Outlook), not only Google.

Paste your feed URL into ICAL_URL below. Anyone holding that URL can read your
calendar, so if this folder is a git repo, prefer the sidecar file instead:
put the URL on the first line of calendar_url.txt, which is gitignored.

Runs inside collector.py so the widget keeps its single data source
(cache.json) and its no-polling contract: the widget never fetches anything.
"""

import datetime as dt
import time
from pathlib import Path

import requests

from paths import ICAL_URL_PATH

# ── Paste your secret iCal address here ──────────────────────────────
ICAL_URL = "PASTE_YOUR_SECRET_ICAL_URL_HERE"
# ─────────────────────────────────────────────────────────────────────

PLACEHOLDER = "PASTE_YOUR_SECRET_ICAL_URL_HERE"

SETUP_STEPS = [
    "Google Calendar → Settings → click your calendar in the left sidebar",
    "scroll to 'Integrate calendar' → copy 'Secret address in iCal format'",
    f"paste it as ICAL_URL in calendar_feed.py, or into {ICAL_URL_PATH}",
]

PIP_HINT = "pip install icalendar recurring-ical-events"

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = 30
MAX_EVENTS = 500


class CalendarSetupNeeded(Exception):
    """The feed isn't configured yet, or its parser isn't installed."""


def week_bounds(today=None):
    """(Monday, Sunday) of the week containing `today`."""
    today = today or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)


def _ical_modules():
    try:
        import icalendar
        import recurring_ical_events
    except ImportError as e:
        raise CalendarSetupNeeded(f"{e} — {PIP_HINT}")
    return icalendar, recurring_ical_events


def resolve_url() -> str:
    """The feed URL from ICAL_URL, else the first usable line of
    calendar_url.txt. webcal:// is rewritten to https:// — some calendar UIs
    hand out the URL in that form and requests can't fetch it."""
    for candidate in (ICAL_URL, _url_from_file()):
        url = (candidate or "").strip().strip('"').strip("'")
        if not url or PLACEHOLDER in url:
            continue
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        if not url.startswith(("http://", "https://")):
            raise CalendarSetupNeeded(
                f"calendar URL doesn't look like a link: {url[:40]}…")
        return url
    raise CalendarSetupNeeded("no iCal URL set yet")


def _url_from_file():
    try:
        text = Path(ICAL_URL_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def fetch_ics(url: str) -> str:
    """Download the feed. Same retry/backoff/User-Agent shape as the weather
    and greed calls — a single reset connection shouldn't blank the panel."""
    last_error = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            if attempt:
                time.sleep(2 * attempt)
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"},
                timeout=FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            text = resp.text
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("response wasn't an iCal feed — check the URL")
            return text
        except Exception as e:
            last_error = e
            print(f"[calendar] Attempt {attempt + 1} failed: {e}")
    raise RuntimeError(str(last_error))


def collect_calendar(today=None, ics_text=None, tz=None) -> dict:
    """This week's events, shaped for the week grid.

    Never raises: a missing URL, a missing parser or a failed fetch all come
    back as a payload the panel can render. Pass `ics_text` to parse a feed
    you already have, and `tz` to render times somewhere other than the
    machine's own timezone (both used by the tests).
    """
    monday, sunday = week_bounds(today)
    payload = {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "days": [{"date": (monday + dt.timedelta(days=i)).isoformat(), "events": []}
                 for i in range(7)],
    }

    try:
        icalendar, recurring_ical_events = _ical_modules()
        if ics_text is None:
            ics_text = fetch_ics(resolve_url())
        calendar = icalendar.Calendar.from_ical(ics_text)
        # between() expands RRULEs into real occurrences and applies EXDATE
        # and RECURRENCE-ID overrides; the end is exclusive, hence +1 day.
        occurrences = recurring_ical_events.of(calendar).between(
            monday, sunday + dt.timedelta(days=1))
    except CalendarSetupNeeded as e:
        print(f"[calendar] Setup needed: {e}")
        payload.update(error=str(e), needs_setup=True, setup_steps=SETUP_STEPS)
        return payload
    except Exception as e:
        print(f"[calendar] Failed: {e}")
        payload.update(error=str(e))
        return payload

    by_date = {day["date"]: day["events"] for day in payload["days"]}
    for occurrence in list(occurrences)[:MAX_EVENTS]:
        for date_key, event in _spread_event(occurrence, monday, sunday, tz):
            if date_key in by_date:
                by_date[date_key].append(event)

    for events in by_date.values():
        events.sort(key=lambda e: (not e["all_day"], e["start"] or ""))

    print(f"[calendar] OK — {sum(len(v) for v in by_date.values())} events this week")
    return payload


def _spread_event(occurrence, monday, sunday, tz=None):
    """One occurrence → (iso date, event dict) per day it covers inside the
    week. All-day events carry an exclusive DTEND, so a Fri→Mon event ends on
    the Sunday. Timed events are listed on the day they start."""
    summary = _text(occurrence.get("SUMMARY")) or "(no title)"
    location = _text(occurrence.get("LOCATION"))
    start = _value(occurrence.get("DTSTART"))
    end = _value(occurrence.get("DTEND"))

    if start is None:
        return

    if isinstance(start, dt.datetime):
        start = _to_local(start, tz)
        end = _to_local(end, tz) if isinstance(end, dt.datetime) else None
        yield start.date().isoformat(), {
            "summary": summary,
            "location": location,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if end else None,
            "all_day": False,
        }
        return

    # date (not datetime) → an all-day event
    last = start
    if isinstance(end, dt.date):
        last = max(start, end - dt.timedelta(days=1))
    day = max(start, monday)
    while day <= min(last, sunday):
        yield day.isoformat(), {"summary": summary, "location": location,
                                "start": None, "end": None, "all_day": True}
        day += dt.timedelta(days=1)


def _value(field):
    """icalendar wraps values in vDDDTypes/vText; `.dt` is the real thing."""
    return getattr(field, "dt", None) if field is not None else None


def _text(field):
    return str(field).strip() if field is not None else ""


def _to_local(moment: dt.datetime, tz=None) -> dt.datetime:
    """Feeds mix UTC, TZID and floating times; the panel shows wall clock in
    the machine's timezone (or `tz` when one is given)."""
    try:
        return moment.astimezone(tz)
    except (ValueError, OSError):
        return moment

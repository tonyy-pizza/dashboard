"""Calendar — read-only week view built from a published iCal URL.

No OAuth, no consent screen, no credentials.json/token.json, no google-* or
msal packages. The collector fetches one .ics file over HTTPS and parses it,
which works with any provider that publishes a private iCal address.

Point this straight at the calendar that owns the events. Every extra hop
(Outlook → Google → here) adds its own refresh delay and its own chance of
pointing at the wrong calendar, because a provider's iCal address always
covers exactly ONE calendar — never a whole account, whatever the provider's
web UI overlays on screen.

For an Outlook/Exchange work calendar that means publishing it directly:
see SETUP_STEPS. Publishing is not the same as connecting an app — there is
no app registration, no consent and no token involved, just a URL.

Paste your feed URL into ICAL_URL below. Anyone holding that URL can read your
calendar, so if this folder is a git repo, prefer the sidecar file instead:
put the URL on the first line of calendar_url.txt, which is gitignored.

Runs inside collector.py so the widget keeps its single data source
(cache.json) and its no-polling contract: the widget never fetches anything.
"""

import datetime as dt
import re
import sys
import time
from pathlib import Path

import requests

from paths import ICAL_URL_PATH

# ── Paste your secret iCal address here ──────────────────────────────
ICAL_URL = "PASTE_YOUR_SECRET_ICAL_URL_HERE"
# ─────────────────────────────────────────────────────────────────────

PLACEHOLDER = "PASTE_YOUR_SECRET_ICAL_URL_HERE"

SETUP_STEPS = [
    "Outlook on the web → Settings → Calendar → Shared calendars",
    "under 'Publish a calendar' pick the work calendar, permission "
    "'Can view all details', then Publish",
    "copy the ICS link (the one ending .ics — not the HTML link)",
    f"paste it as ICAL_URL in calendar_feed.py, or into {ICAL_URL_PATH}",
]

# Kept for the Google route. Its address covers one calendar only, so it has
# to be the calendar the events actually live on — the one in the sidebar,
# not whichever is listed first.
GOOGLE_SETUP_STEPS = [
    "Google Calendar → Settings → click the calendar holding the events",
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
    return resolve_url_with_source()[0]


def resolve_url_with_source():
    """(url, where it came from) — the diagnostic wants to name the source."""
    sources = ((ICAL_URL, "ICAL_URL in calendar_feed.py"),
               (_url_from_file(), str(ICAL_URL_PATH)))
    for candidate, source in sources:
        url = (candidate or "").strip().strip('"').strip("'")
        if not url or PLACEHOLDER in url:
            continue
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        if not url.startswith(("http://", "https://")):
            # Only the scheme is safe to echo. The rest of the line ends up in
            # cache.json and on screen, and a mistyped scheme is no reason to
            # print the secret part of an otherwise valid address.
            head = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:?/*", url)
            raise CalendarSetupNeeded(
                f"calendar URL doesn't start with https:// (got "
                f"{(head.group(0) if head else url[:8]) or '?'}…)")
        return url, source
    raise CalendarSetupNeeded("no iCal URL set yet")


# Path segments that name the service rather than identify the calendar.
# Everything else is the secret and gets masked.
_PUBLIC_SEGMENTS = {"calendar", "ical", "owa", "basic.ics", "calendar.ics",
                    "reachcalendar.ics"}


def mask_url(url: str) -> str:
    """A URL safe to print. The path segments are the secret — anyone with
    the whole thing can read the calendar."""
    text = url or ""
    if text.startswith("webcal://"):
        text = "https://" + text[len("webcal://"):]
    match = re.match(r"^(https?://[^/]+)(/.*)?$", text)
    if not match:
        return "(unreadable)"
    host, path = match.group(1), match.group(2) or ""
    segments = []
    for segment in path.split("/"):
        if not segment or segment in _PUBLIC_SEGMENTS:
            segments.append(segment)
        else:
            segments.append(f"{segment[:3]}…{len(segment)} chars")
    return host + "/".join(segments)


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


def scrub(message, url: str) -> str:
    """Keep the secret out of logs. requests puts the whole URL in its error
    text, and that text ends up printed, written into cache.json's error
    field, and shown in the panel."""
    text = str(message)
    return text.replace(url, mask_url(url)) if url else text


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
            # RFC 5545 feeds are UTF-8, but requests falls back to
            # ISO-8859-1 for any text/* response that omits a charset —
            # which turns accented event titles into mojibake.
            if "charset=" not in (resp.headers.get("Content-Type") or "").lower():
                resp.encoding = "utf-8"
            text = resp.text
            if "BEGIN:VCALENDAR" not in text:
                raise ValueError("response wasn't an iCal feed — check the URL")
            return text
        except Exception as e:
            last_error = scrub(e, url)
            print(f"[calendar] Attempt {attempt + 1} failed: {last_error}")
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
        in_feed = len(calendar.walk("VEVENT"))
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

    payload["events_in_feed"] = in_feed
    if not in_feed:
        # A feed that parses but holds nothing is almost always the address of
        # the wrong calendar — and without this the panel would draw a week of
        # dashes and say nothing, which is indistinguishable from a quiet week.
        # No `error`: the fetch and the parse both worked, so the diagnostic
        # should still print its summary rather than stopping at "FAILED".
        payload.update(needs_setup=True, setup_steps=SETUP_STEPS,
                       notice="that address is a valid feed but holds no "
                              "events — most likely the wrong calendar")

    print(f"[calendar] OK — {sum(len(v) for v in by_date.values())} events this week "
          f"({in_feed} in the feed)")
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


# ─────────────────────────────────────────────────────────────────────
# Diagnostic: py calendar_feed.py
# ─────────────────────────────────────────────────────────────────────

def check() -> int:
    """Answer "is it my URL or the script?" in one command.

    Walks the same three steps the collector does — find the URL, fetch it,
    parse it — and says which one broke. Never prints the URL itself.
    """
    print("checking the calendar feed\n")

    try:
        url, source = resolve_url_with_source()
    except CalendarSetupNeeded as e:
        print(f"  url ..... NOT SET ({e})\n")
        print("  no URL to test. Publish the calendar that owns the events:")
        for i, step in enumerate(SETUP_STEPS, 1):
            print(f"    {i}. {step}")
        return 1
    print(f"  url ..... found in {source}")
    print(f"            {mask_url(url)}")

    try:
        ics_text = fetch_ics(url)
    except Exception as e:
        print(f"  fetch ... FAILED — {e}\n")
        print("  " + _fetch_advice(str(e), url))
        return 1
    print(f"  fetch ... ok, {len(ics_text) / 1024:.1f} KB from {_provider(url)}")

    payload = collect_calendar(ics_text=ics_text)
    if payload.get("error"):
        print(f"  parse ... FAILED — {payload['error']}")
        return 1

    total = payload.get("events_in_feed", 0)
    this_week = sum(len(day["events"]) for day in payload["days"])
    print(f"  parse ... ok, {total} events in the feed, "
          f"{this_week} in the week of {payload['week_start']}\n")

    for day in payload["days"]:
        events = day["events"] or []
        listed = ", ".join(
            (e["summary"] if e["all_day"] else f"{e['start']} {e['summary']}")
            for e in events) or "—"
        print(f"    {day['date']}  {listed}")

    if not total:
        print("\n  The feed is valid but contains no events at all — the address of "
              "an empty\n  or wrong calendar. An iCal address covers ONE calendar, "
              "never a whole\n  account, so it has to be the calendar the events "
              "are actually on:")
        for i, step in enumerate(SETUP_STEPS, 1):
            print(f"    {i}. {step}")
    elif not this_week:
        print("\n  The feed parsed but has nothing in the current week. That's "
              "normal for a\n  quiet week — check a date above against the "
              "calendar to be sure it's the\n  right one.")
    elif all(e["summary"].lower() in ("busy", "(no title)")
             for day in payload["days"] for e in day["events"]):
        print("\n  Every event came through titled 'Busy' — the calendar was "
              "published as\n  availability only. Re-publish it with 'Can view "
              "all details' to get titles.")
    else:
        print("\n  Feed is fine. If the panel still looks stale, the collector "
              "isn't running:\n  run `py collector.py` and check the last sync "
              "time in the dashboard.")
    return 0


def _provider(url: str) -> str:
    host = (re.match(r"^https?://([^/]+)", url or "") or [None, ""])[1].lower()
    if "outlook" in host or "office" in host or "microsoft" in host:
        return "Outlook/Exchange"
    if "google" in host:
        return "Google Calendar"
    return host or "the server"


def _fetch_advice(error: str, url: str = "") -> str:
    error = error.lower()
    outlook = _provider(url) == "Outlook/Exchange"
    if "404" in error:
        if outlook:
            return ("404 means the address doesn't exist. Un-publishing and "
                    "re-publishing a\n  calendar in Outlook mints a new link — "
                    "copy the current one.")
        return ("404 means the address doesn't exist. Google invalidates the "
                "secret address\n  when you press Reset — copy it again from "
                "Integrate calendar.")
    if "401" in error or "403" in error:
        if outlook:
            return ("Access denied. Many work tenants block calendar publishing "
                    "by policy — if\n  'Publish a calendar' is missing or greyed "
                    "out in Outlook, that's the cause,\n  and the Google route in "
                    "GOOGLE_SETUP_STEPS is the way round it.")
        return ("Access denied. That's usually the *public* URL or a calendar "
                "that isn't\n  shared — use the 'Secret address in iCal "
                "format', which ends in /basic.ics.")
    if "wasn't an ical feed" in error:
        return ("The server answered with something that isn't a calendar — "
                "usually a sign-in\n  page or the HTML view. Outlook offers two "
                "links when you publish; this\n  needs the one ending .ics.")
    if "name or service not known" in error or "connection" in error:
        return "Couldn't reach the server at all — network or firewall."
    return "Check the address, then try opening it in a browser."


if __name__ == "__main__":
    sys.exit(check())

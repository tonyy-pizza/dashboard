"""Calendar — read-only week view built from published iCal URLs.

The grid runs Sunday→Saturday and pages a few weeks either side of today.

No OAuth, no consent screen, no credentials.json/token.json, no google-* or
msal packages. The collector fetches each .ics file over HTTPS and parses it,
which works with any provider that publishes a private iCal address.

A provider's iCal address always covers exactly ONE calendar — never a whole
account, whatever the provider's web UI overlays on screen. That is why a
shared calendar sitting happily in your Google Calendar sidebar does NOT
appear in your own calendar's feed: the sidebar is a view over many
calendars, the feed is one of them.

So every calendar you want on the dashboard needs its own address, and they
are listed together — see ICAL_URLS below and SHARED_SETUP_STEPS for the
shared-calendar case. Events are merged into one grid and de-duplicated, so
an invite that lands on both your own and a shared calendar is drawn once.

Point each address straight at the calendar that owns the events. Every extra
hop (Outlook → Google → here) adds its own refresh delay and its own chance of
pointing at the wrong calendar.

For an Outlook/Exchange work calendar that means publishing it directly:
see SETUP_STEPS. Publishing is not the same as connecting an app — there is
no app registration, no consent and no token involved, just a URL.

Anyone holding one of these URLs can read that calendar, so if this folder is
a git repo, prefer the sidecar file: put one feed per line in
calendar_url.txt, which is gitignored.

Runs inside collector.py so the widget keeps its single data source
(cache.json) and its no-polling contract: the widget never fetches anything.
"""

import datetime as dt
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from paths import ICAL_URL_PATH

# ── Paste your secret iCal addresses here, one per calendar ──────────
# Either a bare URL or ("Label", "URL") — the label is what the event
# popup shows under "calendar", so name them the way you think of them.
ICAL_URLS = [
    # ("Personal", "https://calendar.google.com/calendar/ical/…/basic.ics"),
    # ("Family",   "https://calendar.google.com/calendar/ical/…/basic.ics"),
]
# Single-feed setups that predate ICAL_URLS keep working untouched.
ICAL_URL = "PASTE_YOUR_SECRET_ICAL_URL_HERE"
# ─────────────────────────────────────────────────────────────────────

PLACEHOLDER = "PASTE_YOUR_SECRET_ICAL_URL_HERE"

SETUP_STEPS = [
    "Outlook on the web → Settings → Calendar → Shared calendars",
    "under 'Publish a calendar' pick the work calendar, permission "
    "'Can view all details', then Publish",
    "copy the ICS link (the one ending .ics — not the HTML link)",
    f"add it to ICAL_URLS in calendar_feed.py, or as a line in {ICAL_URL_PATH}",
]

# Kept for the Google route. Its address covers one calendar only, so it has
# to be the calendar the events actually live on — the one in the sidebar,
# not whichever is listed first.
GOOGLE_SETUP_STEPS = [
    "Google Calendar → Settings → click the calendar holding the events",
    "scroll to 'Integrate calendar' → copy 'Secret address in iCal format'",
    f"add it to ICAL_URLS in calendar_feed.py, or as a line in {ICAL_URL_PATH}",
]

# A calendar someone else owns is the awkward case: Google only offers a
# secret address for calendars YOU own, so for a calendar merely shared with
# you that row is simply absent from Integrate calendar. Hence the fork.
SHARED_SETUP_STEPS = [
    "if YOU own the shared calendar: Google Calendar → Settings → pick it "
    "under 'Settings for my calendars' → Integrate calendar → copy "
    "'Secret address in iCal format'",
    "if SOMEONE ELSE owns it: that page offers no secret address, so either "
    "ask the owner to send you theirs (it is per-calendar, not per-account), "
    "or have them tick 'Make available to public' and use the public iCal "
    "address instead",
    f"add the address as its own line in {ICAL_URL_PATH} — one calendar per "
    "line, optionally as 'Label = URL'",
]

PIP_HINT = "pip install icalendar recurring-ical-events"

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = 30
# The widget never fetches, so every week it can page to has to already be
# in cache.json. This window is what the panel's ‹ › buttons move within;
# widen it here and the buttons reach further, at the cost of cache size
# (a busy week of Teams invites is roughly 20 KB).
WEEKS_BACK = 2
WEEKS_AHEAD = 6

MAX_EVENTS = 1200

# The detail popup carries the invite text into cache.json, and an Outlook
# invite body runs to kilobytes. Cap both — the panel shows a summary, not
# the whole thread.
MAX_DESCRIPTION = 1200
MAX_ATTENDEES = 12

# Conferencing hosts the join button knows how to name, most specific first.
MEETING_HOSTS = (
    ("teams.microsoft.com", "Teams"), ("teams.live.com", "Teams"),
    ("meet.google.com", "Meet"), ("zoom.us", "Zoom"),
    ("webex.com", "Webex"), ("gotomeeting.com", "GoToMeeting"),
    ("whereby.com", "Whereby"), ("chime.aws", "Chime"),
    ("bluejeans.com", "BlueJeans"), ("meet.jit.si", "Jitsi"),
)

# Angle brackets and quotes end a URL — Outlook writes its join link as
# "Click here to join the meeting<https://teams.microsoft.com/...>".
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# The rule of underscores Outlook staples above its join block.
_JOIN_BLOCK_RE = re.compile(r"\n?_{20,}\n.*", re.S)


class CalendarSetupNeeded(Exception):
    """The feed isn't configured yet, or its parser isn't installed."""


def week_bounds(today=None):
    """(Sunday, Saturday) of the week containing `today`."""
    today = today or dt.date.today()
    # weekday() is Mon=0 … Sun=6, so (weekday + 1) % 7 is days since Sunday
    # — and 0 when today *is* Sunday, which is the case a plain subtraction
    # of weekday() gets wrong.
    sunday = today - dt.timedelta(days=(today.weekday() + 1) % 7)
    return sunday, sunday + dt.timedelta(days=6)


def _ical_modules():
    try:
        import icalendar
        import recurring_ical_events
    except ImportError as e:
        raise CalendarSetupNeeded(f"{e} — {PIP_HINT}")
    return icalendar, recurring_ical_events


class Feed:
    """One calendar: where to fetch it, and what to call it on screen."""

    def __init__(self, url, label="", source="", text=None):
        self.url = url
        self.label = label
        self.source = source
        self.text = text  # pre-fetched .ics, used by the tests and check()

    def __repr__(self):
        return f"Feed({self.label!r}, {mask_url(self.url)!r})"


def _clean_url(candidate) -> str:
    """A pasted address, normalised. webcal:// is rewritten to https:// —
    some calendar UIs hand out the URL in that form and requests can't fetch
    it. Raises if it is set but unusable."""
    url = (candidate or "").strip().strip('"').strip("'")
    if not url or PLACEHOLDER in url:
        return ""
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
    return url


def resolve_feeds():
    """Every configured calendar, in the order they were listed.

    ICAL_URLS and ICAL_URL both feed in, then the sidecar file, so a setup
    that only ever set ICAL_URL keeps working and can grow a second calendar
    by adding one line to calendar_url.txt.

    Duplicates are dropped: the same address listed twice would otherwise
    draw every one of its events twice.
    """
    feeds, seen = [], set()
    for url, label, source in _configured_entries():
        url = _clean_url(url)
        if not url or url in seen:
            continue
        seen.add(url)
        feeds.append(Feed(url, label, source))
    if not feeds:
        raise CalendarSetupNeeded("no iCal URL set yet")
    return feeds


def _configured_entries():
    """(url, label, source) from each place a calendar can be configured."""
    here = "ICAL_URLS in calendar_feed.py"
    for entry in ICAL_URLS or []:
        if isinstance(entry, (tuple, list)):
            label, url = (list(entry) + ["", ""])[:2] if len(entry) >= 2 else ("", entry[0])
            yield url, str(label or "").strip(), here
        else:
            yield entry, "", here
    yield ICAL_URL, "", "ICAL_URL in calendar_feed.py"
    for label, url in _entries_from_file():
        yield url, label, str(ICAL_URL_PATH)


def resolve_url() -> str:
    """The first configured feed URL. Kept for callers that predate
    multi-calendar support."""
    return resolve_url_with_source()[0]


def resolve_url_with_source():
    """(url, where it came from) — the diagnostic wants to name the source."""
    first = resolve_feeds()[0]
    return first.url, first.source


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


def _entries_from_file():
    """(label, url) per usable line of the sidecar file.

    One calendar per line, blank lines and #comments ignored. A line may be
    a bare URL or 'Label = URL'; the split only counts when the '=' comes
    before the scheme, since a URL's query string is full of them.
    """
    try:
        text = Path(ICAL_URL_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, rest = line.partition("=")
        if sep and "://" not in label:
            yield label.strip(), rest.strip()
        else:
            yield "", line


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
    """A window of Sunday→Saturday weeks, shaped for the week grid.

    "weeks" holds every week the panel can page to, WEEKS_BACK before this
    one through WEEKS_AHEAD after it, and "current_week" indexes the one
    containing today. The current week is also repeated at the top level as
    "days"/"week_start"/"week_end", so a panel that predates paging still
    finds it where it always was.

    Never raises: a missing URL, a missing parser or a failed fetch all come
    back as a payload the panel can render. Pass `ics_text` to parse a feed
    you already have, and `tz` to render times somewhere other than the
    machine's own timezone (both used by the tests).
    """
    start, end = week_bounds(today)
    weeks = [_empty_week(start + dt.timedelta(weeks=offset))
             for offset in range(-WEEKS_BACK, WEEKS_AHEAD + 1)]
    current = WEEKS_BACK
    first_day = dt.date.fromisoformat(weeks[0]["week_start"])
    last_day = dt.date.fromisoformat(weeks[-1]["week_end"])
    payload = {
        # The current week stays at the top level: a cache written by an
        # older collector has no "weeks", and a panel that only knows about
        # the top level still finds this week where it always was.
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "days": weeks[current]["days"],
        "weeks": weeks,
        "current_week": current,
    }

    try:
        icalendar, recurring_ical_events = _ical_modules()
        feeds = _feeds_for_run(ics_text)
    except CalendarSetupNeeded as e:
        print(f"[calendar] Setup needed: {e}")
        payload.update(error=str(e), needs_setup=True, setup_steps=SETUP_STEPS)
        return payload

    by_date = {day["date"]: day["events"]
               for week in weeks for day in week["days"]}
    # One event invited to both your own and a shared calendar arrives once
    # per feed with the same UID. Draw it once, credited to the first feed
    # that carried it.
    seen = set()
    in_feed = 0
    summaries, failures = [], []

    for feed in feeds:
        try:
            text = feed.text if feed.text is not None else fetch_ics(feed.url)
            calendar = icalendar.Calendar.from_ical(text)
            # between() expands RRULEs into real occurrences and applies
            # EXDATE and RECURRENCE-ID overrides; the end is exclusive,
            # hence +1 day.
            occurrences = recurring_ical_events.of(calendar).between(
                first_day, last_day + dt.timedelta(days=1))
            count = len(calendar.walk("VEVENT"))
        except Exception as e:
            # One unreachable calendar must not blank the others: record it
            # and carry on, so a shared feed going stale still leaves your
            # own week on screen.
            message = scrub(e, feed.url)
            print(f"[calendar] {feed.label or mask_url(feed.url)} failed: {message}")
            failures.append({"calendar": feed.label, "error": str(message)})
            continue

        in_feed += count
        drawn = 0
        for occurrence in list(occurrences)[:MAX_EVENTS]:
            key_base = _text(occurrence.get("UID"))
            for date_key, event in _spread_event(
                    occurrence, first_day, last_day, tz, feed.label):
                if date_key not in by_date:
                    continue
                key = (key_base or event["summary"], date_key,
                       event["start"] or "all-day")
                if key in seen:
                    continue
                seen.add(key)
                by_date[date_key].append(event)
                drawn += 1
        summaries.append({"calendar": feed.label, "events_in_feed": count,
                          "events_drawn": drawn})

    for events in by_date.values():
        events.sort(key=lambda e: (not e["all_day"], e["start"] or ""))

    payload["events_in_feed"] = in_feed
    payload["feeds"] = summaries
    if failures:
        payload["feed_errors"] = failures

    if not summaries:
        # Every feed failed — there is nothing on screen, so this is an error
        # rather than a notice.
        payload["error"] = "; ".join(
            f"{f['calendar'] or 'calendar'}: {f['error']}" for f in failures)
        return payload

    if not in_feed:
        # A feed that parses but holds nothing is almost always the address of
        # the wrong calendar — and without this the panel would draw a week of
        # dashes and say nothing, which is indistinguishable from a quiet week.
        # No `error`: the fetch and the parse both worked, so the diagnostic
        # should still print its summary rather than stopping at "FAILED".
        payload.update(needs_setup=True, setup_steps=SETUP_STEPS,
                       notice="that address is a valid feed but holds no "
                              "events — most likely the wrong calendar")
    elif failures:
        payload["notice"] = (
            f"{len(failures)} of {len(feeds)} calendars could not be read — "
            f"the rest are up to date")

    this_week = sum(len(day["events"]) for day in payload["days"])
    print(f"[calendar] OK — {this_week} events this week, "
          f"{sum(len(v) for v in by_date.values())} across "
          f"{len(weeks)} weeks ({in_feed} in {len(summaries)} "
          f"calendar{'s' if len(summaries) != 1 else ''})")
    return payload


def _feeds_for_run(ics_text):
    """The feeds this run should read.

    `ics_text` short-circuits the network for the tests and the diagnostic:
    pass one .ics string, or a list of strings / (label, string) pairs.
    """
    if ics_text is None:
        return resolve_feeds()
    if isinstance(ics_text, str):
        return [Feed("", "", "(supplied)", text=ics_text)]
    feeds = []
    for entry in ics_text:
        if isinstance(entry, (tuple, list)):
            label, text = entry
        else:
            label, text = "", entry
        feeds.append(Feed("", str(label or ""), "(supplied)", text=text))
    return feeds


def _empty_week(start) -> dict:
    """One blank Sunday→Saturday block for the grid to fill."""
    return {
        "week_start": start.isoformat(),
        "week_end": (start + dt.timedelta(days=6)).isoformat(),
        "days": [{"date": (start + dt.timedelta(days=i)).isoformat(), "events": []}
                 for i in range(7)],
    }


def _spread_event(occurrence, first_day, last_day, tz=None, calendar_label=""):
    """One occurrence → (iso date, event dict) per day it covers inside the
    window. All-day events carry an exclusive DTEND, so a Fri→Mon event ends
    on the Sunday. Timed events are listed on the day they start."""
    start = _value(occurrence.get("DTSTART"))
    end = _value(occurrence.get("DTEND"))

    if start is None:
        return

    url, service = _meeting_link(occurrence)
    # Everything the detail popup shows. The grid only reads summary/start,
    # so the rest rides along untouched until someone clicks.
    details = {
        "calendar": calendar_label,
        "summary": _text(occurrence.get("SUMMARY")) or "(no title)",
        "location": _text(occurrence.get("LOCATION")),
        "description": _clean_description(_text(occurrence.get("DESCRIPTION"))),
        "organizer": _person(occurrence.get("ORGANIZER")),
        "attendees": _attendees(occurrence),
        "attendee_count": _attendee_count(occurrence),
        "meeting_url": url,
        "meeting_service": service,
        "status": _text(occurrence.get("STATUS")).upper(),
    }

    if isinstance(start, dt.datetime):
        start = _to_local(start, tz)
        end = _to_local(end, tz) if isinstance(end, dt.datetime) else None
        yield start.date().isoformat(), dict(
            details,
            start=start.strftime("%H:%M"),
            end=end.strftime("%H:%M") if end else None,
            all_day=False,
        )
        return

    # date (not datetime) → an all-day event
    last = start
    if isinstance(end, dt.date):
        last = max(start, end - dt.timedelta(days=1))
    day = max(start, first_day)
    while day <= min(last, last_day):
        yield day.isoformat(), dict(details, start=None, end=None, all_day=True)
        day += dt.timedelta(days=1)


def _meeting_link(occurrence):
    """(url, service) for the popup's join button.

    Only ever returns http(s): the widget hands this straight to the browser,
    and an invite is written by whoever sent it, not by us.
    """
    # Providers that name the link outright beat guessing from prose.
    for key in ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE",
                "CONFERENCE", "URL"):
        for candidate in _property_values(occurrence, key):
            url = _safe_url(candidate)
            if url:
                return url, _service_name(url)

    # Otherwise the first conferencing link in the text Outlook pastes in.
    text = "\n".join((_text(occurrence.get("LOCATION")),
                      _text(occurrence.get("DESCRIPTION"))))
    links = [u for u in (_safe_url(m) for m in _URL_RE.findall(text)) if u]
    for url in links:
        if _service_name(url):
            return url, _service_name(url)
    # A bare link with no recognisable host is still worth offering.
    return (links[0], "") if links else ("", "")


def _property_values(occurrence, key):
    """icalendar hands back a bare value for one instance of a property and a
    list for several — CONFERENCE legitimately repeats."""
    value = occurrence.get(key)
    if value is None:
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def _safe_url(candidate) -> str:
    """A URL safe to hand to the browser, or "". Anything that isn't plain
    http(s) is dropped: file:// and friends reach the local machine, and the
    feed is not a trusted source."""
    url = str(candidate or "").strip().strip("<>").rstrip(".,;:!)\u2019\"'")
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return url


def _service_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    for suffix, label in MEETING_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return label
    return ""


def _clean_description(text: str) -> str:
    """Outlook staples a join block onto the end of every invite. Its link is
    already on the button, so drop the block and keep what a person wrote —
    unless that was the whole body, in which case keep it rather than show
    an empty popup."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    trimmed = _JOIN_BLOCK_RE.sub("", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", (trimmed or text).strip())
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION].rstrip() + "…"
    return text


def _person(field) -> str:
    """'Dana Whitfield' from the CN parameter, else the bare address."""
    if field is None:
        return ""
    name = ""
    if hasattr(field, "params"):
        name = str(field.params.get("CN", "")).strip().strip('"')
    return name or re.sub(r"^mailto:", "", str(field), flags=re.I).strip()


def _attendee_count(occurrence) -> int:
    """The real total, so a capped list can still say "and 40 others"."""
    field = occurrence.get("ATTENDEE")
    if field is None:
        return 0
    return len(field) if isinstance(field, list) else 1


def _attendees(occurrence):
    """Invitee names for the popup, capped. A big distribution list would
    otherwise put hundreds of addresses into cache.json."""
    field = occurrence.get("ATTENDEE")
    if field is None:
        return []
    people = [_person(a) for a in (field if isinstance(field, list) else [field])]
    return [p for p in people if p][:MAX_ATTENDEES]


def _value(field):
    """icalendar wraps values in vDDDTypes/vText; `.dt` is the real thing."""
    return getattr(field, "dt", None) if field is not None else None


def _text(field):
    """First value as a string.

    A feed may carry the same property twice — icalendar hands back a list
    when it does, and str() on that list would land in the panel verbatim as
    "[vText(b'CANCELLED'), vText(b'CONFIRMED')]".
    """
    if isinstance(field, list):
        field = field[0] if field else None
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
    print("checking the calendar feeds\n")

    try:
        feeds = resolve_feeds()
    except CalendarSetupNeeded as e:
        print(f"  url ..... NOT SET ({e})\n")
        print("  no URL to test. Publish the calendar that owns the events:")
        for i, step in enumerate(SETUP_STEPS, 1):
            print(f"    {i}. {step}")
        return 1

    print(f"  found ... {len(feeds)} calendar{'s' if len(feeds) != 1 else ''}")
    fetched, failed = [], 0
    for feed in feeds:
        name = feed.label or _provider(feed.url)
        print(f"\n  [{name}] from {feed.source}")
        print(f"    url ..... {mask_url(feed.url)}")
        try:
            ics_text = fetch_ics(feed.url)
        except Exception as e:
            failed += 1
            print(f"    fetch ... FAILED — {e}")
            print("    " + _fetch_advice(str(e), feed.url).replace("\n  ", "\n    "))
            continue
        print(f"    fetch ... ok, {len(ics_text) / 1024:.1f} KB from "
              f"{_provider(feed.url)}")
        fetched.append((feed.label, ics_text))

    if not fetched:
        print("\n  Nothing could be fetched.")
        return 1
    print()

    payload = collect_calendar(ics_text=fetched)
    if payload.get("error"):
        print(f"  parse ... FAILED — {payload['error']}")
        return 1

    for summary in payload.get("feeds", []):
        print(f"  parse ... {summary['calendar'] or 'calendar'}: "
              f"{summary['events_in_feed']} events in the feed, "
              f"{summary['events_drawn']} drawn in the window")

    total = payload.get("events_in_feed", 0)
    this_week = sum(len(day["events"]) for day in payload["days"])
    print(f"            {total} events across all feeds, "
          f"{this_week} in the week of {payload['week_start']}")
    weeks = payload.get("weeks") or []
    if weeks:
        windowed = sum(len(d["events"]) for w in weeks for d in w["days"])
        print(f"            {windowed} across the {len(weeks)} weeks the panel "
              f"can page to, {weeks[0]['week_start']} → {weeks[-1]['week_end']}")
    print()

    for day in payload["days"]:
        events = day["events"] or []
        listed = ", ".join(
            _describe(e) for e in events) or "—"
        print(f"    {day['date']}  {listed}")

    if not total:
        print("\n  The feed is valid but contains no events at all — the address of "
              "an empty\n  or wrong calendar. An iCal address covers ONE calendar, "
              "never a whole\n  account, so it has to be the calendar the events "
              "are actually on:")
        for i, step in enumerate(SETUP_STEPS, 1):
            print(f"    {i}. {step}")
    elif not this_week:
        print("\n  The feeds parsed but have nothing in the current week. That's "
              "normal for a\n  quiet week — check a date above against the "
              "calendar to be sure it's the\n  right one.")
    elif all(e["summary"].lower() in ("busy", "(no title)")
             for day in payload["days"] for e in day["events"]):
        print("\n  Every event came through titled 'Busy' — the calendar was "
              "published as\n  availability only. Re-publish it with 'Can view "
              "all details' to get titles.")
    elif failed:
        print(f"\n  {failed} calendar(s) above failed; the rest are fine. The panel "
              f"shows what\n  could be read rather than going blank.")
    else:
        print("\n  Feeds are fine. If the panel still looks stale, the collector "
              "isn't running:\n  run `py collector.py` and check the last sync "
              "time in the dashboard.")

    if len(feeds) == 1:
        print("\n  Only one calendar is configured. A shared calendar is a "
              "SEPARATE calendar —\n  it shows in your Google sidebar but is not "
              "in this feed, and needs its own\n  address added alongside:")
        for i, step in enumerate(SHARED_SETUP_STEPS, 1):
            print(f"    {i}. {step}")
    return 0


def _describe(event) -> str:
    """'09:30 Standup [Family]' — the calendar tag only when there is one, so
    single-calendar output reads exactly as it did before."""
    text = (event["summary"] if event["all_day"]
            else f"{event['start']} {event['summary']}")
    return f"{text} [{event['calendar']}]" if event.get("calendar") else text


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

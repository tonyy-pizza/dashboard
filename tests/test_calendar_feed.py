"""Calendar shaping — parsing a real .ics without going near the network."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import calendar_feed

# Every fixture below is written in Vancouver time, and the panel renders in
# the machine's timezone — so the tests pin the render timezone rather than
# depending on where they happen to run.
VANCOUVER = ZoneInfo("America/Vancouver")

MONDAY = dt.date(2026, 8, 10)
SUNDAY = dt.date(2026, 8, 16)
THAT_WEEK = dt.date(2026, 8, 12)          # any day inside the week


def ics(*events):
    body = "\n".join(events)
    return ("BEGIN:VCALENDAR\n"
            "PRODID:-//Google Inc//Google Calendar 70.9054//EN\n"
            "VERSION:2.0\n"
            "CALSCALE:GREGORIAN\n"
            "X-WR-TIMEZONE:America/Vancouver\n"
            f"{body}\n"
            "END:VCALENDAR\n")


def timed(summary, start, end, extra=""):
    return (f"BEGIN:VEVENT\nDTSTART;TZID=America/Vancouver:{start}\n"
            f"DTEND;TZID=America/Vancouver:{end}\nSUMMARY:{summary}\n"
            f"{extra}END:VEVENT")


def all_day(summary, start, end, extra=""):
    return (f"BEGIN:VEVENT\nDTSTART;VALUE=DATE:{start}\n"
            f"DTEND;VALUE=DATE:{end}\nSUMMARY:{summary}\n{extra}END:VEVENT")


def days_of(payload):
    return {day["date"]: day["events"] for day in payload["days"]}


def collect(*events, today=THAT_WEEK, tz=VANCOUVER):
    return calendar_feed.collect_calendar(today=today, ics_text=ics(*events), tz=tz)


# ── week bounds ──────────────────────────────────────────────────────

@pytest.mark.parametrize("today,monday", [
    (dt.date(2026, 8, 16), dt.date(2026, 8, 10)),   # a Sunday
    (dt.date(2026, 8, 10), dt.date(2026, 8, 10)),   # the Monday itself
    (dt.date(2026, 1, 1), dt.date(2025, 12, 29)),   # week spanning new year
])
def test_week_runs_monday_to_sunday(today, monday):
    assert calendar_feed.week_bounds(today) == (monday, monday + dt.timedelta(days=6))


def test_payload_always_has_seven_days():
    payload = collect()
    assert len(payload["days"]) == 7
    assert payload["week_start"] == "2026-08-10"
    assert payload["week_end"] == "2026-08-16"


# ── parsing ──────────────────────────────────────────────────────────

def test_timed_event_lands_on_its_day_with_local_times():
    days = days_of(collect(timed("standup", "20260811T090000", "20260811T091500")))

    assert [e["summary"] for e in days["2026-08-11"]] == ["standup"]
    event = days["2026-08-11"][0]
    assert event["start"] == "09:00"
    assert event["end"] == "09:15"
    assert event["all_day"] is False


def test_utc_times_are_converted_to_the_render_timezone():
    body = ("BEGIN:VEVENT\nDTSTART:20260811T160000Z\nDTEND:20260811T163000Z\n"
            "SUMMARY:standup\nEND:VEVENT")
    days = days_of(collect(body))

    assert days["2026-08-11"][0]["start"] == "09:00"      # 16:00Z − 7h


def test_without_an_explicit_timezone_it_uses_the_machines():
    """What the collector actually does on joputer."""
    body = ("BEGIN:VEVENT\nDTSTART:20260811T160000Z\nDTEND:20260811T163000Z\n"
            "SUMMARY:standup\nEND:VEVENT")
    expected = dt.datetime(2026, 8, 11, 16, 0,
                           tzinfo=dt.timezone.utc).astimezone().strftime("%H:%M")

    payload = calendar_feed.collect_calendar(today=THAT_WEEK, ics_text=ics(body))

    starts = [e["start"] for day in payload["days"] for e in day["events"]]
    assert starts == [expected]


def test_all_day_event_uses_an_exclusive_end():
    days = days_of(collect(all_day("holiday", "20260812", "20260813")))

    assert [d for d, e in days.items() if e] == ["2026-08-12"]
    assert days["2026-08-12"][0]["all_day"] is True
    assert days["2026-08-12"][0]["start"] is None


def test_multi_day_all_day_event_covers_each_day():
    days = days_of(collect(all_day("conference", "20260813", "20260816")))

    assert [d for d, e in days.items() if e] == [
        "2026-08-13", "2026-08-14", "2026-08-15"]


def test_multi_day_event_is_clipped_to_the_week():
    days = days_of(collect(all_day("long trip", "20260805", "20260820")))

    filled = [d for d, e in days.items() if e]
    assert filled[0] == "2026-08-10" and filled[-1] == "2026-08-16"


def test_events_outside_the_week_are_dropped():
    days = days_of(collect(timed("last month", "20260701T090000", "20260701T100000"),
                           timed("next month", "20260901T090000", "20260901T100000")))

    assert all(not events for events in days.values())


def test_location_and_missing_title_are_handled():
    days = days_of(collect(
        timed("dentist", "20260811T143000", "20260811T153000",
              extra="LOCATION:Broadway\n"),
        "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260812\nDTEND;VALUE=DATE:20260813\n"
        "END:VEVENT"))

    assert days["2026-08-11"][0]["location"] == "Broadway"
    assert days["2026-08-12"][0]["summary"] == "(no title)"


def test_all_day_events_sort_above_timed_ones():
    days = days_of(collect(
        timed("late", "20260811T160000", "20260811T170000"),
        timed("early", "20260811T080000", "20260811T090000"),
        all_day("birthday", "20260811", "20260812")))

    assert [e["summary"] for e in days["2026-08-11"]] == ["birthday", "early", "late"]


# ── recurrence ───────────────────────────────────────────────────────

def test_weekly_recurrence_is_expanded_into_this_week():
    # Started back in June, still running.
    body = ("BEGIN:VEVENT\nDTSTART;TZID=America/Vancouver:20260602T090000\n"
            "DTEND;TZID=America/Vancouver:20260602T093000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU\nSUMMARY:standup\nEND:VEVENT")
    days = days_of(collect(body))

    assert [e["summary"] for e in days["2026-08-11"]] == ["standup"]


def test_daily_recurrence_fills_the_whole_week():
    body = ("BEGIN:VEVENT\nDTSTART;TZID=America/Vancouver:20260601T070000\n"
            "DTEND;TZID=America/Vancouver:20260601T073000\n"
            "RRULE:FREQ=DAILY\nSUMMARY:meds\nEND:VEVENT")
    days = days_of(collect(body))

    assert all(len(events) == 1 for events in days.values())


def test_cancelled_occurrences_are_skipped():
    body = ("BEGIN:VEVENT\nDTSTART;TZID=America/Vancouver:20260602T090000\n"
            "DTEND;TZID=America/Vancouver:20260602T093000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU\n"
            "EXDATE;TZID=America/Vancouver:20260811T090000\n"
            "SUMMARY:standup\nEND:VEVENT")
    days = days_of(collect(body))

    assert days["2026-08-11"] == []


def test_a_recurrence_that_ended_before_this_week_is_gone():
    body = ("BEGIN:VEVENT\nDTSTART;TZID=America/Vancouver:20260602T090000\n"
            "DTEND;TZID=America/Vancouver:20260602T093000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20260701T000000Z\n"
            "SUMMARY:old standup\nEND:VEVENT")
    days = days_of(collect(body))

    assert all(not events for events in days.values())


# ── URL resolution ───────────────────────────────────────────────────

def test_placeholder_url_reports_setup_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar_feed, "ICAL_URL", calendar_feed.PLACEHOLDER)
    monkeypatch.setattr(calendar_feed, "ICAL_URL_PATH", tmp_path / "calendar_url.txt")

    with pytest.raises(calendar_feed.CalendarSetupNeeded):
        calendar_feed.resolve_url()


def test_url_from_the_constant_wins(monkeypatch):
    monkeypatch.setattr(calendar_feed, "ICAL_URL",
                        "https://calendar.google.com/calendar/ical/x/basic.ics")
    assert calendar_feed.resolve_url().endswith("basic.ics")


def test_url_falls_back_to_the_sidecar_file(monkeypatch, tmp_path):
    path = tmp_path / "calendar_url.txt"
    path.write_text("# my secret feed\nhttps://example.com/private/basic.ics\n",
                    encoding="utf-8")
    monkeypatch.setattr(calendar_feed, "ICAL_URL", calendar_feed.PLACEHOLDER)
    monkeypatch.setattr(calendar_feed, "ICAL_URL_PATH", path)

    assert calendar_feed.resolve_url() == "https://example.com/private/basic.ics"


def test_webcal_urls_are_rewritten(monkeypatch):
    monkeypatch.setattr(calendar_feed, "ICAL_URL", "webcal://example.com/f.ics")
    assert calendar_feed.resolve_url() == "https://example.com/f.ics"


def test_a_url_that_isnt_a_link_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar_feed, "ICAL_URL", "my-calendar")
    monkeypatch.setattr(calendar_feed, "ICAL_URL_PATH", tmp_path / "nope.txt")

    with pytest.raises(calendar_feed.CalendarSetupNeeded, match="doesn't look like"):
        calendar_feed.resolve_url()


# ── graceful degradation ─────────────────────────────────────────────

def test_unconfigured_feed_renders_setup_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar_feed, "ICAL_URL", calendar_feed.PLACEHOLDER)
    monkeypatch.setattr(calendar_feed, "ICAL_URL_PATH", tmp_path / "calendar_url.txt")

    payload = calendar_feed.collect_calendar(today=THAT_WEEK)

    assert payload["needs_setup"] is True
    assert payload["setup_steps"]
    assert len(payload["days"]) == 7          # the grid still renders, empty


def test_missing_parser_reports_setup_needed(monkeypatch):
    monkeypatch.setattr(calendar_feed, "_ical_modules", lambda: (_ for _ in ()).throw(
        calendar_feed.CalendarSetupNeeded("no icalendar — pip install icalendar")))

    payload = calendar_feed.collect_calendar(today=THAT_WEEK)

    assert payload["needs_setup"] is True
    assert "pip install" in payload["error"]


def test_fetch_failure_is_reported_without_the_setup_banner(monkeypatch):
    monkeypatch.setattr(calendar_feed, "ICAL_URL", "https://example.com/f.ics")
    monkeypatch.setattr(calendar_feed, "fetch_ics",
                        lambda url: (_ for _ in ()).throw(RuntimeError("403 Forbidden")))

    payload = calendar_feed.collect_calendar(today=THAT_WEEK)

    assert "403 Forbidden" in payload["error"]
    assert payload.get("needs_setup") is not True


def test_a_non_calendar_response_is_rejected(monkeypatch):
    class Response:
        status_code = 200
        text = "<html>sign in</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(calendar_feed, "FETCH_ATTEMPTS", 1)
    monkeypatch.setattr(calendar_feed.requests, "get", lambda *a, **k: Response())

    with pytest.raises(RuntimeError, match="wasn't an iCal feed"):
        calendar_feed.fetch_ics("https://example.com/f.ics")


def test_garbled_feed_is_reported_not_raised(monkeypatch):
    payload = calendar_feed.collect_calendar(today=THAT_WEEK,
                                             ics_text="BEGIN:VCALENDAR\nnonsense")

    assert "error" in payload
    assert len(payload["days"]) == 7

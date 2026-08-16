"""Calendar shaping — the parts that don't need Google on the line."""

import datetime as dt

import pytest

import gcal


def spread(item, monday=dt.date(2026, 8, 10)):
    return dict(gcal._spread_event(item, monday, monday + dt.timedelta(days=6)))


# ── week bounds ──────────────────────────────────────────────────────

@pytest.mark.parametrize("today,monday", [
    (dt.date(2026, 8, 16), dt.date(2026, 8, 10)),   # a Sunday
    (dt.date(2026, 8, 10), dt.date(2026, 8, 10)),   # the Monday itself
    (dt.date(2026, 1, 1), dt.date(2025, 12, 29)),   # week spanning new year
])
def test_week_runs_monday_to_sunday(today, monday):
    assert gcal.week_bounds(today) == (monday, monday + dt.timedelta(days=6))


# ── event mapping ────────────────────────────────────────────────────

def test_timed_event_lands_on_its_day():
    days = spread({"summary": "standup",
                   "start": {"dateTime": "2026-08-11T09:00:00-07:00"},
                   "end": {"dateTime": "2026-08-11T09:15:00-07:00"}})

    assert list(days) == ["2026-08-11"]
    event = days["2026-08-11"]
    assert event["summary"] == "standup"
    assert event["all_day"] is False
    assert len(event["start"]) == 5 and ":" in event["start"]


def test_all_day_event_uses_an_exclusive_end():
    # Google marks a single all-day event as start 12th, end 13th.
    days = spread({"summary": "holiday",
                   "start": {"date": "2026-08-12"},
                   "end": {"date": "2026-08-13"}})

    assert list(days) == ["2026-08-12"]
    assert days["2026-08-12"]["all_day"] is True
    assert days["2026-08-12"]["start"] is None


def test_multi_day_event_covers_each_day():
    days = spread({"summary": "conference",
                   "start": {"date": "2026-08-13"},
                   "end": {"date": "2026-08-16"}})

    assert list(days) == ["2026-08-13", "2026-08-14", "2026-08-15"]


def test_multi_day_event_is_clipped_to_the_week():
    days = spread({"summary": "long trip",
                   "start": {"date": "2026-08-05"},
                   "end": {"date": "2026-08-20"}})

    assert min(days) == "2026-08-10"
    assert max(days) == "2026-08-16"


def test_event_without_a_start_is_dropped():
    assert spread({"summary": "malformed", "start": {}, "end": {}}) == {}


def test_missing_title_gets_a_placeholder():
    days = spread({"start": {"date": "2026-08-11"}, "end": {"date": "2026-08-12"}})
    assert days["2026-08-11"]["summary"] == "(no title)"


# ── graceful degradation ─────────────────────────────────────────────

def test_without_google_libraries_it_reports_setup_needed(monkeypatch):
    monkeypatch.setattr(gcal, "_google_modules",
                        lambda: (_ for _ in ()).throw(gcal.CalendarSetupNeeded("no libs")))

    payload = gcal.collect_calendar(dt.date(2026, 8, 16))

    assert payload["needs_setup"] is True
    assert payload["setup_steps"]
    assert len(payload["days"]) == 7          # the grid still renders, empty
    assert payload["week_start"] == "2026-08-10"


def test_api_failure_is_reported_without_the_setup_banner(monkeypatch):
    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("500 backend error")

    monkeypatch.setattr(gcal, "_google_modules", lambda: (None, None, None, Boom()))
    monkeypatch.setattr(gcal, "load_credentials", lambda interactive=False: object())

    payload = gcal.collect_calendar(dt.date(2026, 8, 16))

    assert "500 backend error" in payload["error"]
    assert payload.get("needs_setup") is not True

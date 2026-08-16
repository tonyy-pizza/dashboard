"""Translating between the two API dialects and the neutral SyncEvent.

Pure conversion only — no network, no credentials. This is where the
all-day-boundary and timezone mistakes would live.
"""

import datetime as dt

import pytest

from google_side import event_from_google, google_body
from outlook_side import (
    event_from_graph, graph_body, marker_properties, property_id,
)
from sync_model import (
    ATTENDEE_HEADER, FINGERPRINT_KEY, GOOGLE, ID_KEY, OUTLOOK, SOURCE_KEY,
    SyncEvent, fingerprint,
)


def google_item(**overrides):
    item = {
        "id": "g1",
        "summary": "standup",
        "description": "agenda",
        "location": "room 1",
        "start": {"dateTime": "2026-08-11T09:00:00-07:00", "timeZone": "America/Vancouver"},
        "end": {"dateTime": "2026-08-11T09:30:00-07:00", "timeZone": "America/Vancouver"},
        "updated": "2026-08-10T12:00:00.000Z",
    }
    item.update(overrides)
    return item


def graph_item(**overrides):
    item = {
        "id": "o1",
        "subject": "standup",
        "body": {"contentType": "text", "content": "agenda"},
        "location": {"displayName": "room 1"},
        "start": {"dateTime": "2026-08-11T16:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-08-11T16:30:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "lastModifiedDateTime": "2026-08-10T12:00:00.0000000Z",
    }
    item.update(overrides)
    return item


# ── Google → SyncEvent ───────────────────────────────────────────────

def test_google_timed_event_normalizes_to_utc():
    event = event_from_google(google_item())

    assert event.uid == "g1"
    assert event.summary == "standup"
    assert event.start == "2026-08-11T16:00:00Z"      # 09:00 −07:00
    assert event.end == "2026-08-11T16:30:00Z"
    assert event.all_day is False
    assert event.updated == dt.datetime(2026, 8, 10, 12, tzinfo=dt.timezone.utc)


def test_google_all_day_event_keeps_its_dates():
    event = event_from_google(google_item(
        start={"date": "2026-08-11"}, end={"date": "2026-08-12"}))

    assert event.all_day is True
    assert (event.start, event.end) == ("2026-08-11", "2026-08-12")


def test_google_markers_are_read():
    event = event_from_google(google_item(extendedProperties={"private": {
        SOURCE_KEY: OUTLOOK, ID_KEY: "o1", FINGERPRINT_KEY: "abc123"}}))

    assert event.is_copy is True
    assert (event.source, event.source_id) == (OUTLOOK, "o1")
    assert event.base_fingerprint == "abc123"


def test_a_plain_google_event_is_not_a_copy():
    assert event_from_google(google_item()).is_copy is False


def test_google_attendees_become_text():
    event = event_from_google(google_item(attendees=[
        {"email": "a@x.com", "displayName": "Alice"},
        {"email": "b@y.com"},
    ]))

    assert event.attendees == ("Alice <a@x.com>", "b@y.com")


def test_google_missing_fields_are_tolerated():
    event = event_from_google({"id": "g1"})
    assert (event.summary, event.description, event.location) == ("", "", "")
    assert event.start == ""


# ── SyncEvent → Google ───────────────────────────────────────────────

def test_google_body_carries_content_and_markers():
    event = SyncEvent(uid="o1", summary="standup", description="agenda",
                      location="room 1", start="2026-08-11T16:00:00Z",
                      end="2026-08-11T16:30:00Z")

    body = google_body(event, OUTLOOK, "o1", "fp1")

    assert body["summary"] == "standup"
    assert body["start"] == {"dateTime": "2026-08-11T16:00:00+00:00", "timeZone": "UTC"}
    assert body["extendedProperties"]["private"] == {
        SOURCE_KEY: OUTLOOK, ID_KEY: "o1", FINGERPRINT_KEY: "fp1"}


def test_google_body_never_sets_real_attendees():
    """Real attendees on a mirror would send a second set of invitations."""
    event = SyncEvent(uid="o1", summary="review", attendees=("Alice <a@x.com>",))

    body = google_body(event)

    assert "attendees" not in body
    assert ATTENDEE_HEADER in body["description"]
    assert "Alice <a@x.com>" in body["description"]


def test_google_all_day_body_uses_date_keys():
    event = SyncEvent(uid="o1", summary="holiday", start="2026-08-11",
                      end="2026-08-12", all_day=True)

    body = google_body(event)

    assert body["start"] == {"date": "2026-08-11"}
    assert body["end"] == {"date": "2026-08-12"}


def test_google_body_without_markers_omits_them():
    assert "extendedProperties" not in google_body(SyncEvent(uid="x"))


# ── Graph → SyncEvent ────────────────────────────────────────────────

def test_graph_timed_event_normalizes_to_utc():
    event = event_from_graph(graph_item())

    assert event.uid == "o1"
    assert event.start == "2026-08-11T16:00:00Z"
    assert event.end == "2026-08-11T16:30:00Z"
    assert event.all_day is False
    assert event.updated == dt.datetime(2026, 8, 10, 12, tzinfo=dt.timezone.utc)


def test_graph_all_day_event_reduces_to_dates():
    event = event_from_graph(graph_item(
        isAllDay=True,
        start={"dateTime": "2026-08-11T00:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-08-12T00:00:00.0000000", "timeZone": "UTC"}))

    assert event.all_day is True
    assert (event.start, event.end) == ("2026-08-11", "2026-08-12")


def test_graph_html_bodies_are_flattened():
    event = event_from_graph(graph_item(body={
        "contentType": "html",
        "content": "<html><body><p>agenda</p><p>second&nbsp;line</p></body></html>"}))

    assert event.description == "agenda\n\nsecond line"


def test_graph_markers_are_read():
    event = event_from_graph(graph_item(singleValueExtendedProperties=[
        {"id": property_id(SOURCE_KEY), "value": GOOGLE},
        {"id": property_id(ID_KEY), "value": "g1"},
        {"id": property_id(FINGERPRINT_KEY), "value": "abc123"},
    ]))

    assert event.is_copy is True
    assert (event.source, event.source_id) == (GOOGLE, "g1")
    assert event.base_fingerprint == "abc123"


def test_graph_attendees_become_text():
    event = event_from_graph(graph_item(attendees=[
        {"emailAddress": {"address": "a@x.com", "name": "Alice"}},
        {"emailAddress": {"address": "b@y.com", "name": "b@y.com"}},
    ]))

    assert event.attendees == ("Alice <a@x.com>", "b@y.com")


def test_graph_seven_digit_timestamps_parse():
    event = event_from_graph(graph_item(
        lastModifiedDateTime="2026-08-10T12:00:00.1234567Z"))

    assert event.updated.year == 2026


# ── SyncEvent → Graph ────────────────────────────────────────────────

def test_graph_body_carries_content_and_markers():
    event = SyncEvent(uid="g1", summary="standup", description="agenda",
                      location="room 1", start="2026-08-11T16:00:00Z",
                      end="2026-08-11T16:30:00Z")

    body = graph_body(event, GOOGLE, "g1", "fp1")

    assert body["subject"] == "standup"
    assert body["body"] == {"contentType": "text", "content": "agenda"}
    assert body["start"] == {"dateTime": "2026-08-11T16:00:00", "timeZone": "UTC"}
    assert body["singleValueExtendedProperties"] == marker_properties(
        GOOGLE, "g1", "fp1")


def test_graph_body_never_sets_real_attendees():
    event = SyncEvent(uid="g1", summary="review", attendees=("Alice <a@x.com>",))

    body = graph_body(event)

    assert "attendees" not in body
    assert ATTENDEE_HEADER in body["body"]["content"]


def test_graph_all_day_body_spans_midnight_to_midnight():
    event = SyncEvent(uid="g1", summary="holiday", start="2026-08-11",
                      end="2026-08-12", all_day=True)

    body = graph_body(event)

    assert body["isAllDay"] is True
    assert body["start"]["dateTime"].startswith("2026-08-11T00:00:00")
    assert body["end"]["dateTime"].startswith("2026-08-12T00:00:00")


# ── the two dialects agree ───────────────────────────────────────────

def test_the_same_meeting_fingerprints_identically_on_both_sides():
    """The whole sync rests on this: one meeting, two APIs, one fingerprint."""
    from_google = event_from_google(google_item())
    from_graph = event_from_graph(graph_item())

    assert fingerprint(from_google) == fingerprint(from_graph)


def test_the_same_all_day_event_fingerprints_identically():
    from_google = event_from_google(google_item(
        summary="holiday", description="", location="",
        start={"date": "2026-08-11"}, end={"date": "2026-08-12"}))
    from_graph = event_from_graph(graph_item(
        subject="holiday", body={"contentType": "text", "content": ""},
        location={"displayName": ""}, isAllDay=True,
        start={"dateTime": "2026-08-11T00:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-08-12T00:00:00.0000000", "timeZone": "UTC"}))

    assert fingerprint(from_google) == fingerprint(from_graph)


def test_a_meeting_with_attendees_round_trips_between_the_dialects():
    """Outlook original with real attendees → Google mirror carrying them as
    text → both fingerprint the same, so the pair stays quiet."""
    original = event_from_graph(graph_item(attendees=[
        {"emailAddress": {"address": "a@x.com", "name": "Alice"}}]))

    mirrored = google_body(original, OUTLOOK, original.uid, fingerprint(original))
    round_tripped = event_from_google(google_item(
        id="g1",
        summary=mirrored["summary"],
        description=mirrored["description"],
        location=mirrored["location"],
        start={"dateTime": "2026-08-11T16:00:00+00:00"},
        end={"dateTime": "2026-08-11T16:30:00+00:00"},
        extendedProperties=mirrored["extendedProperties"]))

    assert fingerprint(round_tripped) == fingerprint(original)


@pytest.mark.parametrize("side_body,reader,item_maker", [
    (graph_body, event_from_graph, graph_item),
    (google_body, event_from_google, google_item),
])
def test_writing_then_reading_keeps_the_fingerprint(side_body, reader, item_maker):
    original = reader(item_maker())
    assert fingerprint(original) == fingerprint(reader(item_maker()))

"""The sync engine: what it decides to write, and what it refuses to."""

import datetime as dt

import pytest

from sync_model import (
    ATTENDEE_HEADER, GOOGLE, OUTLOOK, SyncEvent, fingerprint, plan,
    render_description, split_attendee_block, sync_window,
)

NOON = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)


def event(uid, summary="standup", start="2026-08-11T16:00:00Z",
          end="2026-08-11T16:30:00Z", updated=NOON, **kwargs):
    return SyncEvent(uid=uid, summary=summary, start=start, end=end,
                     updated=updated, **kwargs)


def copy_of(original, uid, side, **kwargs):
    """The mirror the sync would have produced for `original`."""
    fields = dict(summary=original.summary, description=original.description,
                  location=original.location, start=original.start,
                  end=original.end, all_day=original.all_day,
                  attendees=original.attendees, updated=original.updated)
    fields.update(kwargs)
    return SyncEvent(uid=uid, source=side, source_id=original.uid,
                     base_fingerprint=fingerprint(original), **fields)


def kinds(actions):
    return [(a.kind, a.target) for a in actions]


# ── window ───────────────────────────────────────────────────────────

def test_window_is_rolling_around_now():
    start, end = sync_window(NOON)
    assert (NOON - start).days == 7
    assert (end - NOON).days == 120


def test_window_size_is_adjustable():
    start, end = sync_window(NOON, days_past=1, days_future=2)
    assert (NOON - start).days == 1 and (end - NOON).days == 2


# ── fingerprinting ───────────────────────────────────────────────────

def test_same_content_fingerprints_the_same():
    assert fingerprint(event("a")) == fingerprint(event("b"))


@pytest.mark.parametrize("change", [
    {"summary": "different"},
    {"start": "2026-08-11T17:00:00Z"},
    {"end": "2026-08-11T18:00:00Z"},
    {"location": "room 2"},
    {"description": "notes"},
])
def test_content_changes_move_the_fingerprint(change):
    assert fingerprint(event("a", **change)) != fingerprint(event("a"))


def test_the_uid_and_timestamp_are_not_content():
    assert fingerprint(event("a", updated=NOON)) == fingerprint(
        event("z", updated=NOON + dt.timedelta(days=3)))


def test_whitespace_noise_is_not_a_change():
    assert (fingerprint(event("a", summary="standup  ", description="hi\r\n"))
            == fingerprint(event("a", summary="standup", description="hi")))


# ── attendees travel as text, never as invitations ───────────────────

def test_attendees_render_into_the_description():
    rendered = render_description(event("a", description="agenda",
                                        attendees=("Alice <a@x.com>",)))
    assert "agenda" in rendered
    assert ATTENDEE_HEADER in rendered
    assert "Alice <a@x.com>" in rendered


def test_rendering_is_idempotent():
    once = render_description(event("a", description="agenda",
                                    attendees=("Alice <a@x.com>",)))
    twice = render_description(event("a", description=once,
                                     attendees=("Alice <a@x.com>",)))
    assert once == twice
    assert twice.count(ATTENDEE_HEADER) == 1


def test_the_block_reads_back_out():
    body, attendees = split_attendee_block(
        f"agenda\n\n{ATTENDEE_HEADER}\nAlice <a@x.com>\nBob <b@y.com>")
    assert body == "agenda"
    assert attendees == ("Alice <a@x.com>", "Bob <b@y.com>")


def test_an_original_and_its_text_only_mirror_match():
    """The original carries real attendees; the copy carries the text block
    and no attendees. They must still compare as in sync."""
    original = event("o", description="agenda", attendees=("Alice <a@x.com>",))
    mirror = event("m", description=render_description(original))

    assert fingerprint(original) == fingerprint(mirror)
    assert plan([mirror_as_copy(original, mirror)], [original]) == []


def mirror_as_copy(original, mirror):
    return SyncEvent(uid=mirror.uid, summary=mirror.summary,
                     description=mirror.description, start=mirror.start,
                     end=mirror.end, updated=mirror.updated,
                     source=OUTLOOK, source_id=original.uid,
                     base_fingerprint=fingerprint(original))


# ── first sync ───────────────────────────────────────────────────────

def test_an_original_with_no_mirror_is_created_on_the_other_side():
    actions = plan([], [event("o1")])

    assert kinds(actions) == [("create", GOOGLE)]
    assert actions[0].link_source == OUTLOOK
    assert actions[0].link_source_id == "o1"


def test_both_sides_create_into_each_other():
    actions = plan([event("g1", summary="gym")], [event("o1", summary="review")])

    assert sorted(kinds(actions)) == [("create", GOOGLE), ("create", OUTLOOK)]


def test_an_up_to_date_pair_produces_nothing():
    original = event("o1")
    assert plan([copy_of(original, "g1", OUTLOOK)], [original]) == []


# ── loop prevention ──────────────────────────────────────────────────

def test_a_mirror_is_never_mirrored_back():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)

    actions = plan([mirror], [original])

    assert actions == []
    assert not any(a.target == OUTLOOK for a in actions)


def test_a_mirror_whose_original_vanished_is_left_alone():
    """Deletions don't propagate, so an orphan copy just sits there."""
    orphan = copy_of(event("gone"), "g1", OUTLOOK)

    assert plan([orphan], []) == []


def test_a_marker_naming_its_own_side_is_ignored():
    """A hand-copied event carrying the wrong marker shouldn't be re-mirrored."""
    odd = copy_of(event("x"), "g1", GOOGLE)      # google event marked as google

    assert plan([odd], []) == []


# ── edits ────────────────────────────────────────────────────────────

def test_editing_the_original_updates_the_mirror():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)
    edited = event("o1", summary="standup (moved)",
                   updated=NOON + dt.timedelta(hours=1))

    actions = plan([mirror], [edited])

    assert kinds(actions) == [("update", GOOGLE)]
    assert actions[0].target_uid == "g1"
    assert actions[0].source_event.summary == "standup (moved)"
    assert actions[0].link_source == OUTLOOK      # markers refreshed with it


def test_editing_the_mirror_updates_the_original():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK, summary="standup (moved)",
                     updated=NOON + dt.timedelta(hours=1))

    actions = plan([mirror], [original])

    assert kinds(actions) == [("update", OUTLOOK), ("relink", GOOGLE)]
    assert actions[0].target_uid == "o1"
    assert actions[0].source_event.summary == "standup (moved)"
    # The base fingerprint lives on the copy, so it has to be refreshed too.
    assert actions[1].fingerprint == actions[0].fingerprint


def test_deletions_are_never_planned():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK, summary="edited",
                     updated=NOON + dt.timedelta(hours=1))

    actions = plan([mirror], [original]) + plan([], [original]) + plan([mirror], [])

    assert all(a.kind in ("create", "update", "relink", "link") for a in actions)


# ── conflicts ────────────────────────────────────────────────────────

def test_when_both_sides_changed_the_newer_edit_wins():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)
    # Both diverge from the shared base, Google most recently.
    outlook_edit = event("o1", summary="outlook version",
                         updated=NOON + dt.timedelta(hours=1))
    google_edit = SyncEvent(uid="g1", summary="google version",
                            start=mirror.start, end=mirror.end,
                            updated=NOON + dt.timedelta(hours=2),
                            source=OUTLOOK, source_id="o1",
                            base_fingerprint=mirror.base_fingerprint)

    actions = plan([google_edit], [outlook_edit])

    assert actions[0].target == OUTLOOK
    assert actions[0].source_event.summary == "google version"
    assert "newer" in actions[0].reason


def test_the_older_edit_loses_the_other_way_round():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)
    outlook_edit = event("o1", summary="outlook version",
                         updated=NOON + dt.timedelta(hours=5))
    google_edit = SyncEvent(uid="g1", summary="google version",
                            start=mirror.start, end=mirror.end,
                            updated=NOON + dt.timedelta(hours=2),
                            source=OUTLOOK, source_id="o1",
                            base_fingerprint=mirror.base_fingerprint)

    actions = plan([google_edit], [outlook_edit])

    assert actions[0].target == GOOGLE
    assert actions[0].source_event.summary == "outlook version"


def test_a_side_without_a_timestamp_does_not_win():
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)
    outlook_edit = event("o1", summary="outlook version", updated=None)
    google_edit = SyncEvent(uid="g1", summary="google version",
                            start=mirror.start, end=mirror.end, updated=NOON,
                            source=OUTLOOK, source_id="o1",
                            base_fingerprint=mirror.base_fingerprint)

    actions = plan([google_edit], [outlook_edit])

    assert actions[0].source_event.summary == "google version"


def test_writing_back_settles_on_the_next_run():
    """After a conflict is resolved, the pair must go quiet — otherwise the
    two calendars ping-pong forever."""
    original = event("o1")
    mirror = copy_of(original, "g1", OUTLOOK)
    google_edit = SyncEvent(uid="g1", summary="google version",
                            start=mirror.start, end=mirror.end,
                            updated=NOON + dt.timedelta(hours=2),
                            source=OUTLOOK, source_id="o1",
                            base_fingerprint=mirror.base_fingerprint)

    actions = plan([google_edit], [original])
    assert actions                                     # something to do now

    # Apply it: Outlook takes the new content, the copy's base is refreshed.
    settled_outlook = event("o1", summary="google version",
                            updated=NOON + dt.timedelta(hours=3))
    settled_google = SyncEvent(uid="g1", summary="google version",
                               start=mirror.start, end=mirror.end,
                               updated=google_edit.updated, source=OUTLOOK,
                               source_id="o1",
                               base_fingerprint=fingerprint(google_edit))

    assert plan([settled_google], [settled_outlook]) == []


# ── first-run reconciliation ─────────────────────────────────────────

def test_without_reconcile_a_first_run_duplicates_matching_events():
    same_on_both = [event("g1", summary="all hands")], [event("o1", summary="all hands")]

    actions = plan(*same_on_both)

    assert sorted(kinds(actions)) == [("create", GOOGLE), ("create", OUTLOOK)]


def test_reconcile_links_matching_events_instead_of_copying_them():
    actions = plan([event("g1", summary="all hands")],
                   [event("o1", summary="all hands")], reconcile=True)

    assert kinds(actions) == [("link", GOOGLE)]
    assert actions[0].target_uid == "g1"
    assert actions[0].link_source_id == "o1"


def test_reconcile_leaves_genuinely_different_events_to_be_created():
    actions = plan([event("g1", summary="gym", start="2026-08-12T01:00:00Z")],
                   [event("o1", summary="all hands")], reconcile=True)

    assert sorted(kinds(actions)) == [("create", GOOGLE), ("create", OUTLOOK)]


def test_reconcile_refuses_ambiguous_matches():
    """Two identical-looking candidates: linking either one is a guess."""
    actions = plan([event("g1", summary="all hands")],
                   [event("o1", summary="all hands"),
                    event("o2", summary="all hands")],
                   reconcile=True)

    assert not any(a.kind == "link" for a in actions)


def test_reconcile_matches_on_start_not_just_title():
    actions = plan([event("g1", summary="all hands")],
                   [event("o1", summary="all hands",
                          start="2026-08-12T16:00:00Z")], reconcile=True)

    assert not any(a.kind == "link" for a in actions)


def test_reconcile_ignores_case_and_padding_when_matching():
    actions = plan([event("g1", summary="  All Hands ")],
                   [event("o1", summary="all hands")], reconcile=True)

    # Matched despite the casing, so no duplicate is created…
    assert not any(a.kind == "create" for a in actions)
    assert kinds(actions)[0] == ("link", GOOGLE)
    # …and the two titles then converge on one spelling rather than staying
    # subtly different forever.
    assert kinds(actions)[1] == ("update", GOOGLE)
    assert actions[1].source_event.summary == "all hands"


def test_a_linked_pair_needs_no_further_writes():
    """The link action carries the base fingerprint, so the same run doesn't
    also queue an update for the pair it just linked."""
    actions = plan([event("g1", summary="all hands")],
                   [event("o1", summary="all hands")], reconcile=True)

    assert [a.kind for a in actions] == ["link"]


# ── all-day events ───────────────────────────────────────────────────

def test_all_day_and_timed_events_are_different_content():
    timed = event("a", start="2026-08-11T00:00:00Z", end="2026-08-12T00:00:00Z")
    whole_day = event("a", start="2026-08-11", end="2026-08-12", all_day=True)

    assert fingerprint(timed) != fingerprint(whole_day)


def test_an_all_day_pair_stays_in_sync():
    original = event("o1", start="2026-08-11", end="2026-08-12", all_day=True)

    assert plan([copy_of(original, "g1", OUTLOOK)], [original]) == []

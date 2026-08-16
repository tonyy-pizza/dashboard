"""The runner: dry runs, the status file the dashboard reads, and what
happens when one event or one whole calendar fails.
"""

import datetime as dt
import json

import pytest

import calendar_sync
from sync_model import GOOGLE, OUTLOOK, Action, SyncEvent, fingerprint

NOON = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)


class FakeSide:
    """Records writes instead of performing them."""

    def __init__(self, name, events=None, fail_on=None):
        self.name = name
        self.events = events or []
        self.fail_on = fail_on or set()
        self.created, self.updated, self.marked = [], [], []
        self.listed = None

    def list_events(self, start, end):
        if "list" in self.fail_on:
            raise RuntimeError(f"{self.name} is unreachable")
        self.listed = (start, end)
        return list(self.events)

    def create(self, event, source, source_id, fingerprint):
        if "create" in self.fail_on:
            raise RuntimeError("create refused")
        self.created.append((event, source, source_id, fingerprint))
        return f"new-{len(self.created)}"

    def update(self, uid, event):
        if "update" in self.fail_on:
            raise RuntimeError("update refused")
        self.updated.append((uid, event))

    def set_markers(self, uid, source, source_id, fingerprint):
        self.marked.append((uid, source, source_id, fingerprint))


def event(uid, summary="standup", updated=NOON):
    return SyncEvent(uid=uid, summary=summary, start="2026-08-11T16:00:00Z",
                     end="2026-08-11T16:30:00Z", updated=updated)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_sync, "SYNC_STATUS_PATH",
                        tmp_path / "calendar_sync_status.json")
    monkeypatch.setattr(calendar_sync, "SYNC_LOG_PATH", tmp_path / "sync.log")
    calendar_sync.configure_logging()
    return tmp_path


def status_of(tmp_path):
    return json.loads((tmp_path / "calendar_sync_status.json").read_text())


def sides(google_events=(), outlook_events=(), **kwargs):
    return {GOOGLE: FakeSide(GOOGLE, list(google_events), kwargs.get("google_fail")),
            OUTLOOK: FakeSide(OUTLOOK, list(outlook_events), kwargs.get("outlook_fail"))}


# ── applying actions ─────────────────────────────────────────────────

def test_create_actions_reach_the_target_side():
    both = sides()
    actions = [Action(kind="create", target=GOOGLE, source_event=event("o1"),
                      fingerprint="fp1", link_source=OUTLOOK, link_source_id="o1")]

    written = calendar_sync.apply_actions(actions, both)

    assert written == 1
    assert both[GOOGLE].created[0][1:] == (OUTLOOK, "o1", "fp1")
    assert both[OUTLOOK].created == []


def test_update_actions_also_refresh_the_markers_on_a_copy():
    both = sides()
    actions = [Action(kind="update", target=GOOGLE, source_event=event("o1"),
                      target_uid="g1", fingerprint="fp2",
                      link_source=OUTLOOK, link_source_id="o1")]

    calendar_sync.apply_actions(actions, both)

    assert both[GOOGLE].updated[0][0] == "g1"
    assert both[GOOGLE].marked == [("g1", OUTLOOK, "o1", "fp2")]


def test_updating_an_original_leaves_its_markers_alone():
    both = sides()
    actions = [Action(kind="update", target=OUTLOOK, source_event=event("g1"),
                      target_uid="o1", fingerprint="fp2")]

    calendar_sync.apply_actions(actions, both)

    assert both[OUTLOOK].updated[0][0] == "o1"
    assert both[OUTLOOK].marked == []      # originals never carry markers


def test_relink_only_touches_markers():
    both = sides()
    actions = [Action(kind="relink", target=GOOGLE, target_uid="g1",
                      fingerprint="fp3", link_source=OUTLOOK, link_source_id="o1")]

    calendar_sync.apply_actions(actions, both)

    assert both[GOOGLE].marked == [("g1", OUTLOOK, "o1", "fp3")]
    assert both[GOOGLE].updated == [] and both[GOOGLE].created == []


def test_one_failing_event_does_not_abandon_the_rest():
    both = sides()
    both[GOOGLE].fail_on = {"create"}
    actions = [
        Action(kind="create", target=GOOGLE, source_event=event("o1"),
               link_source=OUTLOOK, link_source_id="o1"),
        Action(kind="create", target=OUTLOOK, source_event=event("g1"),
               link_source=GOOGLE, link_source_id="g1"),
    ]

    written = calendar_sync.apply_actions(actions, both)

    assert written == 1
    assert len(both[OUTLOOK].created) == 1


# ── dry run ──────────────────────────────────────────────────────────

def test_a_dry_run_writes_absolutely_nothing():
    both = sides(outlook_events=[event("o1")])

    calendar_sync.run(dry_run=True, sides=both, now=NOON)

    assert both[GOOGLE].created == []
    assert both[GOOGLE].updated == []
    assert both[GOOGLE].marked == []


def test_a_dry_run_still_reports_what_it_would_do(isolated_paths):
    both = sides(outlook_events=[event("o1"), event("o2", summary="review")])

    calendar_sync.run(dry_run=True, sides=both, now=NOON)

    status = status_of(isolated_paths)
    assert status["dry_run"] is True
    assert status["events_planned_count"] == 2
    assert status["events_synced_count"] == 0


def test_a_dry_run_logs_each_planned_action(isolated_paths):
    both = sides(outlook_events=[event("o1", summary="quarterly review")])

    calendar_sync.run(dry_run=True, sides=both, now=NOON)

    logged = (isolated_paths / "sync.log").read_text(encoding="utf-8")
    assert "would create on google" in logged
    assert "quarterly review" in logged


# ── a real run ───────────────────────────────────────────────────────

def test_a_real_run_creates_the_missing_mirrors(isolated_paths):
    both = sides(outlook_events=[event("o1")])

    result = calendar_sync.run(sides=both, now=NOON)

    assert result == 0
    assert len(both[GOOGLE].created) == 1
    assert status_of(isolated_paths)["events_synced_count"] == 1


def test_the_sync_window_is_passed_to_both_sides():
    both = sides()

    calendar_sync.run(sides=both, now=NOON, days_past=7, days_future=120)

    for side in both.values():
        start, end = side.listed
        assert (NOON - start).days == 7
        assert (end - NOON).days == 120


def test_an_already_synced_pair_writes_nothing(isolated_paths):
    original = event("o1")
    mirror = SyncEvent(uid="g1", summary=original.summary, start=original.start,
                       end=original.end, updated=original.updated,
                       source=OUTLOOK, source_id="o1",
                       base_fingerprint=fingerprint(original))
    both = sides(google_events=[mirror], outlook_events=[original])

    calendar_sync.run(sides=both, now=NOON)

    assert both[GOOGLE].created == [] and both[OUTLOOK].created == []
    assert status_of(isolated_paths)["events_planned_count"] == 0


# ── status file ──────────────────────────────────────────────────────

def test_status_records_a_successful_run(isolated_paths):
    calendar_sync.run(sides=sides(), now=NOON)

    status = status_of(isolated_paths)
    assert status["last_error"] is None
    assert status["last_run_utc"] == status["last_success_utc"]
    assert "duration_seconds" in status


def test_status_keeps_the_last_success_after_a_failure(isolated_paths):
    calendar_sync.run(sides=sides(), now=NOON)
    good_run = status_of(isolated_paths)["last_success_utc"]

    calendar_sync.run(sides=sides(google_fail={"list"}), now=NOON)

    status = status_of(isolated_paths)
    assert status["last_error"] is not None
    assert "unreachable" in status["last_error"]
    assert status["last_success_utc"] == good_run     # the old one survives


def test_a_later_success_clears_the_error(isolated_paths):
    calendar_sync.run(sides=sides(google_fail={"list"}), now=NOON)
    assert status_of(isolated_paths)["last_error"] is not None

    calendar_sync.run(sides=sides(), now=NOON)

    status = status_of(isolated_paths)
    assert status["last_error"] is None
    assert status["last_success_utc"] == status["last_run_utc"]


def test_an_unreachable_calendar_fails_the_run_without_raising():
    result = calendar_sync.run(sides=sides(outlook_fail={"list"}), now=NOON)
    assert result == 1


def test_nothing_is_written_when_a_calendar_cannot_be_read():
    both = sides(google_fail={"list"}, outlook_events=[event("o1")])

    calendar_sync.run(sides=both, now=NOON)

    assert both[OUTLOOK].created == []      # no half-sync off a partial read


# ── repeated runs ────────────────────────────────────────────────────

class StatefulSide(FakeSide):
    """A fake that actually keeps what it's given, so a run can be repeated
    against the result of the previous one."""

    def __init__(self, name, events=None):
        super().__init__(name, events)
        self.clock = NOON
        self._next = 0

    def _tick(self):
        self.clock += dt.timedelta(minutes=1)
        return self.clock

    def create(self, event, source, source_id, fp):
        super().create(event, source, source_id, fp)
        self._next += 1
        self.events.append(SyncEvent(
            uid=f"{self.name}-{self._next}", summary=event.summary,
            description=event.description, location=event.location,
            start=event.start, end=event.end, all_day=event.all_day,
            updated=self._tick(), source=source, source_id=source_id,
            base_fingerprint=fp))
        return self.events[-1].uid

    def update(self, uid, event):
        super().update(uid, event)
        for i, existing in enumerate(self.events):
            if existing.uid == uid:
                self.events[i] = SyncEvent(
                    uid=uid, summary=event.summary, description=event.description,
                    location=event.location, start=event.start, end=event.end,
                    all_day=event.all_day, updated=self._tick(),
                    source=existing.source, source_id=existing.source_id,
                    base_fingerprint=existing.base_fingerprint)

    def set_markers(self, uid, source, source_id, fp):
        super().set_markers(uid, source, source_id, fp)
        for i, existing in enumerate(self.events):
            if existing.uid == uid:
                self.events[i] = SyncEvent(
                    uid=uid, summary=existing.summary,
                    description=existing.description, location=existing.location,
                    start=existing.start, end=existing.end,
                    all_day=existing.all_day, updated=existing.updated,
                    source=source, source_id=source_id, base_fingerprint=fp)


def stateful(google_events=(), outlook_events=()):
    return {GOOGLE: StatefulSide(GOOGLE, list(google_events)),
            OUTLOOK: StatefulSide(OUTLOOK, list(outlook_events))}


def test_a_second_run_over_the_same_calendars_changes_nothing():
    """The property that keeps this off a 10-minute duplication treadmill."""
    both = stateful(outlook_events=[event("o1")])

    calendar_sync.run(sides=both, now=NOON)
    assert len(both[GOOGLE].created) == 1

    calendar_sync.run(sides=both, now=NOON)

    assert len(both[GOOGLE].created) == 1          # no second copy
    assert both[GOOGLE].updated == []
    assert len(both[GOOGLE].events) == 1


def test_an_edit_propagates_once_and_then_settles():
    both = stateful(outlook_events=[event("o1")])
    calendar_sync.run(sides=both, now=NOON)

    # Someone renames the Outlook original.
    both[OUTLOOK].events[0] = event("o1", summary="standup (moved)",
                                    updated=NOON + dt.timedelta(hours=2))
    calendar_sync.run(sides=both, now=NOON)
    assert len(both[GOOGLE].updated) == 1
    assert both[GOOGLE].events[0].summary == "standup (moved)"

    calendar_sync.run(sides=both, now=NOON)

    assert len(both[GOOGLE].updated) == 1          # didn't bounce back
    assert both[OUTLOOK].updated == []


def test_an_edit_on_the_mirror_flows_back_and_settles():
    both = stateful(outlook_events=[event("o1")])
    calendar_sync.run(sides=both, now=NOON)

    mirror = both[GOOGLE].events[0]
    both[GOOGLE].events[0] = SyncEvent(
        uid=mirror.uid, summary="renamed in google", start=mirror.start,
        end=mirror.end, updated=NOON + dt.timedelta(hours=3),
        source=mirror.source, source_id=mirror.source_id,
        base_fingerprint=mirror.base_fingerprint)

    calendar_sync.run(sides=both, now=NOON)
    assert both[OUTLOOK].events[0].summary == "renamed in google"

    before = (len(both[GOOGLE].updated), len(both[OUTLOOK].updated))
    calendar_sync.run(sides=both, now=NOON)

    assert (len(both[GOOGLE].updated), len(both[OUTLOOK].updated)) == before


def test_reconcile_then_normal_runs_stay_quiet():
    both = stateful(google_events=[event("g1", summary="all hands")],
                    outlook_events=[event("o1", summary="all hands")])

    calendar_sync.run(sides=both, reconcile=True, now=NOON)

    assert both[GOOGLE].created == [] and both[OUTLOOK].created == []
    assert len(both[GOOGLE].events) == 1 and len(both[OUTLOOK].events) == 1

    calendar_sync.run(sides=both, now=NOON)

    assert both[GOOGLE].created == [] and both[OUTLOOK].created == []


# ── CLI ──────────────────────────────────────────────────────────────

def test_dry_run_flag_is_wired_up(monkeypatch):
    seen = {}
    monkeypatch.setattr(calendar_sync, "run",
                        lambda **kwargs: seen.update(kwargs) or 0)

    calendar_sync.main(["--dry-run"])

    assert seen["dry_run"] is True
    assert seen["reconcile"] is False


def test_reconcile_flag_is_wired_up(monkeypatch):
    seen = {}
    monkeypatch.setattr(calendar_sync, "run",
                        lambda **kwargs: seen.update(kwargs) or 0)

    calendar_sync.main(["--reconcile", "--days-past", "3"])

    assert seen["reconcile"] is True
    assert seen["days_past"] == 3


def test_defaults_match_the_spec(monkeypatch):
    seen = {}
    monkeypatch.setattr(calendar_sync, "run",
                        lambda **kwargs: seen.update(kwargs) or 0)

    calendar_sync.main([])

    assert (seen["days_past"], seen["days_future"]) == (7, 120)
    assert seen["dry_run"] is False

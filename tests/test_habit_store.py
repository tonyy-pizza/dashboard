import datetime as dt

import pytest

from habit_store import HabitStore, year_dates


@pytest.fixture
def store(tmp_path):
    return HabitStore(tmp_path / "habits.json")


def names(habits):
    return [h.name for h in habits]


def test_year_grid_is_sized_off_the_calendar():
    assert len(year_dates(2026)) == 365
    assert len(year_dates(2028)) == 366          # leap year
    assert year_dates(2026)[0] == dt.date(2026, 1, 1)
    assert year_dates(2026)[-1] == dt.date(2026, 12, 31)


def test_add_habit_with_goal(store):
    habits = store.add("lift", "4x / week")
    assert names(habits) == ["lift"]
    assert habits[0].goal == "4x / week"
    assert habits[0].archived is False


def test_toggle_marks_and_unmarks_a_day(store):
    habit = store.add("lift")[0]
    day = dt.date(2026, 8, 16)

    habits, state = store.toggle(habit.id, day)
    assert state is True
    assert habits[0].is_done(day) is True

    habits, state = store.toggle(habit.id, day)
    assert state is False
    assert habits[0].is_done(day) is False


def test_backfilling_a_past_day_works(store):
    habit = store.add("lift")[0]
    past = dt.date(2026, 2, 3)

    store.toggle(habit.id, past)
    assert store.load()[0].is_done(past) is True


def test_toggles_persist_across_instances(store):
    habit = store.add("lift")[0]
    store.toggle(habit.id, dt.date(2026, 8, 16))

    assert HabitStore(store.path).load()[0].is_done(dt.date(2026, 8, 16)) is True


def test_unmarked_days_are_not_kept_on_disk(store):
    habit = store.add("lift")[0]
    day = dt.date(2026, 8, 16)
    store.toggle(habit.id, day)
    store.toggle(habit.id, day)

    assert "2026-08-16" not in store.path.read_text(encoding="utf-8")


def test_done_count_is_scoped_to_the_year(store):
    habit = store.add("lift")[0]
    store.toggle(habit.id, dt.date(2026, 1, 2))
    store.toggle(habit.id, dt.date(2026, 3, 4))
    store.toggle(habit.id, dt.date(2025, 12, 31))

    habit = store.load()[0]
    assert habit.done_count(2026) == 2
    assert habit.done_count() == 3


def test_archiving_hides_but_keeps_the_data(store):
    habit = store.add("lift")[0]
    store.toggle(habit.id, dt.date(2026, 8, 16))

    store.set_archived(habit.id, True)
    assert store.active() == []
    assert names(store.archived()) == ["lift"]

    store.set_archived(habit.id, False)
    restored = store.active()[0]
    assert restored.name == "lift"
    assert restored.is_done(dt.date(2026, 8, 16)) is True   # history came back


def test_each_habit_toggles_independently(store):
    store.add("lift")
    store.add("read")
    lift, read = store.load()
    day = dt.date(2026, 8, 16)

    store.toggle(lift.id, day)

    by_name = {h.name: h for h in store.load()}
    assert by_name["lift"].is_done(day) is True
    assert by_name["read"].is_done(day) is False


def test_corrupt_file_does_not_raise(tmp_path):
    path = tmp_path / "habits.json"
    path.write_text("nonsense", encoding="utf-8")
    store = HabitStore(path)

    assert store.load() == []
    assert names(store.add("lift")) == ["lift"]

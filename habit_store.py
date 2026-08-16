"""habits.json — the contribution-graph habit tracker.

Dashboard-owned and written directly by the widget when a dot is clicked.
This deliberately bypasses the collector → cache.json → display flow: habit
toggles are user input, not gathered data, and routing them through
QFileSystemWatcher would mean the UI waited on a file event to show a click
it already knows about.

On-disk shape:

    {"habits": [
       {"id": "...", "name": "lift", "goal": "4x / week",
        "archived": false, "days": {"2026-01-04": true}}
    ]}

`days` only carries the dates that are done — un-toggling drops the key, so
the file stays small over a year. Reads tolerate explicit `false` values in
case the file was hand-edited.
"""

import calendar
import datetime as dt
import uuid
from pathlib import Path

from paths import HABITS_PATH
from storage import load_json, save_json


def year_dates(year: int) -> list:
    """Every date in `year`, Jan 1 → Dec 31. 365 or 366 entries — the grid is
    sized off this, never off a hardcoded 365."""
    days = 366 if calendar.isleap(year) else 365
    jan1 = dt.date(year, 1, 1)
    return [jan1 + dt.timedelta(days=i) for i in range(days)]


class Habit:
    def __init__(self, id, name, goal="", archived=False, days=None):
        self.id = id
        self.name = name
        self.goal = goal
        self.archived = archived
        self.days = days or {}

    def is_done(self, date) -> bool:
        return bool(self.days.get(_iso(date), False))

    def done_count(self, year: int = None) -> int:
        if year is None:
            return sum(1 for v in self.days.values() if v)
        prefix = f"{year:04d}-"
        return sum(1 for k, v in self.days.items() if v and k.startswith(prefix))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "archived": self.archived,
            "days": {k: True for k, v in sorted(self.days.items()) if v},
        }


def _iso(date) -> str:
    return date.isoformat() if isinstance(date, dt.date) else str(date)


class HabitStore:
    def __init__(self, path: Path = HABITS_PATH):
        self.path = Path(path)

    # ── read ─────────────────────────────────────────────────────────
    def load(self) -> list:
        raw = load_json(self.path, {})
        habits = []
        for rec in (raw.get("habits", []) if isinstance(raw, dict) else []):
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("name", "")).strip()
            if not name:
                continue
            days = rec.get("days") or {}
            if not isinstance(days, dict):
                days = {}
            habits.append(Habit(
                id=str(rec.get("id") or uuid.uuid4().hex),
                name=name,
                goal=str(rec.get("goal", "")),
                archived=bool(rec.get("archived", False)),
                days={str(k): bool(v) for k, v in days.items() if v},
            ))
        return habits

    def active(self) -> list:
        return [h for h in self.load() if not h.archived]

    def archived(self) -> list:
        return [h for h in self.load() if h.archived]

    # ── write ────────────────────────────────────────────────────────
    def save(self, habits: list) -> list:
        save_json(self.path, {"habits": [h.to_dict() for h in habits]})
        return habits

    def add(self, name: str, goal: str = "") -> list:
        name = str(name).strip()
        if not name:
            return self.load()
        habits = self.load()
        habits.append(Habit(id=uuid.uuid4().hex, name=name,
                            goal=str(goal).strip(), archived=False, days={}))
        return self.save(habits)

    def set_day(self, habit_id: str, date, done: bool) -> list:
        habits = self.load()
        key = _iso(date)
        for habit in habits:
            if habit.id == habit_id:
                if done:
                    habit.days[key] = True
                else:
                    habit.days.pop(key, None)
        return self.save(habits)

    def toggle(self, habit_id: str, date):
        """Flip one day. Works for any date in the grid, not just today —
        backfilling is the whole point of the year view. Returns
        (habits, new_state)."""
        key = _iso(date)
        current = False
        for habit in self.load():
            if habit.id == habit_id:
                current = habit.is_done(key)
        habits = self.set_day(habit_id, key, not current)
        return habits, not current

    def set_archived(self, habit_id: str, archived: bool) -> list:
        """Archive instead of delete: the habit leaves the main view but its
        day data stays in habits.json and comes back on unarchive."""
        habits = self.load()
        for habit in habits:
            if habit.id == habit_id:
                habit.archived = bool(archived)
        return self.save(habits)

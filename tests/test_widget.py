"""Headless (offscreen) tests for the widget's wiring.

These don't check pixels — they check that the panels build, that the
interactive panels actually reach their files, and that a cache.json renders
into the read-only panels without blowing up.
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF, QSize
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import QApplication

import dashboard_widget as dw
from habit_store import HabitStore
from journal_store import JournalStore
from todo_store import TodoStore


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def never_spawn_the_collector(monkeypatch):
    """The widget now kicks a refresh on startup and on wake. Nothing in the
    suite should actually launch `py collector.py`."""
    started = []
    monkeypatch.setattr(dw.CollectorRunner, "run_async",
                        lambda self: started.append(True) or True)
    return started


@pytest.fixture
def widget(qapp, tmp_path):
    window = dw.DashboardWidget()
    window.todos = TodoStore(tmp_path / "todo.json")
    window.habits = HabitStore(tmp_path / "habits.json")
    window.journal = JournalStore(tmp_path / "journal.org")
    window._reload_todos()
    window._reload_habits()
    window._reload_journal()
    yield window
    window.close()


def todo_rows(widget):
    return widget.findChildren(dw.TodoRow)


# ── construction ─────────────────────────────────────────────────────

def test_window_builds_with_every_panel(widget):
    titles = {label.text() for label in widget.findChildren(dw.QLabel)}
    for panel in ("to do", "habit tracker", "journal", "weather",
                  "calendar", "sector analysis", "rough notes"):
        assert panel in titles


def test_group_labels_are_present(widget):
    titles = {label.text() for label in widget.findChildren(dw.QLabel)}
    assert {"today's items", "at a glance", "freeform"} <= titles


def test_widening_the_window_scales_the_fonts_up(widget):
    widget.resizeEvent(QResizeEvent(QSize(700, 800), QSize(1040, 900)))
    small = widget.clock_label.font().pixelSize()

    widget.resizeEvent(QResizeEvent(QSize(1400, 900), QSize(700, 800)))

    assert widget.clock_label.font().pixelSize() > small


def test_font_scale_is_clamped_at_both_ends(widget):
    widget.scaler.set_width(120)
    assert widget.scaler.factor == pytest.approx(0.55)
    widget.scaler.set_width(9000)
    assert widget.scaler.factor == pytest.approx(1.6)


def test_compact_mode_shrinks_text(widget):
    widget.scaler.set_width(1000)
    comfortable = widget.clock_label.font().pixelSize()
    widget.scaler.set_compact(True)
    assert widget.clock_label.font().pixelSize() < comfortable
    widget.scaler.set_compact(False)


# ── to do ────────────────────────────────────────────────────────────

def test_typing_a_todo_adds_a_row_and_writes_the_file(widget):
    widget.todo_input.setText("buy milk")
    widget._add_todo()

    assert [r.item.text for r in todo_rows(widget)] == ["buy milk"]
    assert widget.todos.load()[0].text == "buy milk"
    assert widget.todo_input.text() == ""


def test_priority_button_cycles_and_applies(widget):
    for _ in range(3):
        widget._cycle_new_priority()
    assert widget.priority_btn.text() == "!!!"

    widget.todo_input.setText("urgent thing")
    widget._add_todo()
    assert widget.todos.load()[0].priority == 3


def test_completing_keeps_the_row_and_strikes_it_through(widget):
    widget.todo_input.setText("write spec")
    widget._add_todo()
    row = todo_rows(widget)[0]

    row.item.done = True
    row._apply_state()
    widget._set_todo_done(row.item.id, True)

    rows = todo_rows(widget)
    assert len(rows) == 1                       # still visible, not archived
    assert rows[0].label.font().strikeOut() is True
    assert widget.todos.load()[0].done is True


def test_delete_removes_it_immediately(widget):
    widget.todo_input.setText("temporary")
    widget._add_todo()
    widget._delete_todo(todo_rows(widget)[0].item.id)

    assert todo_rows(widget) == []
    assert widget.todos.load() == []


def test_tiers_render_highest_first(widget):
    widget.todos.add("plain")
    widget.todos.add("shouty", priority=3)
    widget._reload_todos()

    assert [r.item.text for r in todo_rows(widget)] == ["shouty", "plain"]
    tiers = [t.priority for t in widget.findChildren(dw.TierList)]
    assert tiers == [3, 0]


def test_drag_reorder_persists(widget):
    for text in ("a", "b", "c"):
        widget.todos.add(text)
    widget._reload_todos()
    ids = {r.item.text: r.item.id for r in todo_rows(widget)}

    widget._reorder_todos(0, [ids["c"], ids["a"], ids["b"]])

    assert [r.item.text for r in todo_rows(widget)] == ["c", "a", "b"]
    assert [i.text for i in widget.todos.load()] == ["c", "a", "b"]


def test_dropping_into_another_tier_moves_and_positions(widget):
    widget.todos.add("high one", priority=2)
    widget.todos.add("high two", priority=2)
    widget.todos.add("mover")
    widget._reload_todos()
    mover = next(r for r in todo_rows(widget) if r.item.text == "mover")

    widget._move_todo_tier(mover.item.id, 2, 0)

    tier = [i.text for i in widget.todos.load() if i.priority == 2]
    assert tier == ["mover", "high one", "high two"]


# ── habits ───────────────────────────────────────────────────────────

def test_adding_a_habit_shows_a_grid(widget):
    widget.habits.add("lift", "4x / week")
    widget._reload_habits()

    grids = widget.findChildren(dw.DotGrid)
    assert len(grids) == 1
    assert grids[0]._year == dt.date.today().year


def test_clicking_a_day_writes_habits_json(widget):
    widget.habits.add("lift")
    widget._reload_habits()
    habit_id = widget.habits.load()[0].id
    day = dt.date(dt.date.today().year, 3, 4)

    widget._toggle_habit_day(habit_id, day)
    assert widget.habits.load()[0].is_done(day) is True

    widget._toggle_habit_day(habit_id, day)
    assert widget.habits.load()[0].is_done(day) is False


def test_today_checkbox_logs_the_habit(widget):
    widget.habits.add("lift")
    widget._reload_habits()
    today = dt.date.today()

    check = widget.findChildren(dw.TodayCheck)[0]
    assert check.fill.isVisibleTo(check) is False
    check.clicked.emit()

    assert widget.habits.load()[0].is_done(today) is True
    # The panel rebuilt, so grab the new one and confirm it came back ticked.
    check = widget.findChildren(dw.TodayCheck)[0]
    assert check.fill.isVisibleTo(check) is True


def test_today_checkbox_unlogs_on_a_second_click(widget):
    widget.habits.add("lift")
    habit_id = widget.habits.load()[0].id
    widget.habits.set_day(habit_id, dt.date.today(), True)
    widget._reload_habits()

    widget.findChildren(dw.TodayCheck)[0].clicked.emit()

    assert widget.habits.load()[0].is_done(dt.date.today()) is False


def test_today_checkbox_and_grid_stay_in_sync(widget):
    widget.habits.add("lift")
    widget._reload_habits()
    habit_id = widget.habits.load()[0].id
    today = dt.date.today()

    # Log via the grid; the checkbox should show it after the rebuild.
    widget._toggle_habit_day(habit_id, today)

    check = widget.findChildren(dw.TodayCheck)[0]
    grid = widget.findChildren(dw.DotGrid)[0]
    assert check.fill.isVisibleTo(check) is True
    assert grid._values.get(today) == 1.0


def test_each_habit_gets_its_own_today_checkbox(widget):
    widget.habits.add("lift")
    widget.habits.add("read")
    widget._reload_habits()
    lift_id = widget.habits.load()[0].id

    checks = widget.findChildren(dw.TodayCheck)
    assert len(checks) == 2
    checks[0].clicked.emit()

    by_id = {h.id: h for h in widget.habits.load()}
    assert by_id[lift_id].is_done(dt.date.today()) is True
    assert sum(h.is_done(dt.date.today()) for h in widget.habits.load()) == 1


def test_archiving_removes_the_grid_but_keeps_the_habit(widget):
    widget.habits.add("lift")
    widget._reload_habits()
    habit_id = widget.habits.load()[0].id

    widget._archive_habit(habit_id)

    assert widget.findChildren(dw.DotGrid) == []
    assert [h.name for h in widget.habits.archived()] == ["lift"]


def test_archived_dialog_restores(widget, qapp):
    widget.habits.add("lift")
    habit_id = widget.habits.load()[0].id
    widget.habits.set_archived(habit_id, True)

    dialog = dw.ArchivedHabitsDialog(widget.habits, widget.scaler,
                                     widget.body_font, widget.title_font, widget)
    dialog._restore(habit_id)

    assert dialog.restored_any is True
    assert [h.name for h in widget.habits.active()] == ["lift"]
    dialog.close()


# ── dot grid geometry ────────────────────────────────────────────────

def test_dot_grid_maps_clicks_back_to_dates(qapp):
    grid = dw.DotGrid(2026)
    grid.resize(53 * 9, 80)
    cell = grid._cell
    top = grid._top()

    # Jan 1 2026 is a Thursday: column 0, row 3 (Mon-indexed).
    date = grid._date_at(QPointF(cell * 0.5, top + 3.5 * cell))
    assert date == dt.date(2026, 1, 1)

    # One column right, same row → a week later.
    date = grid._date_at(QPointF(cell * 1.5, top + 3.5 * cell))
    assert date == dt.date(2026, 1, 8)


def test_dot_grid_ignores_padding_days_outside_the_year(qapp):
    grid = dw.DotGrid(2026)
    grid.resize(53 * 9, 80)
    # Column 0 / row 0 is Mon 29 Dec 2025 — grid padding, not a 2026 day.
    assert grid._date_at(QPointF(4, grid._top() + 4)) is None


def test_dot_grid_sizes_itself_for_a_leap_year(qapp):
    assert dw.DotGrid(2028)._columns >= 53
    assert dw.DotGrid(2026)._dates[-1] == dt.date(2026, 12, 31)


def test_score_grid_shades_by_rating(qapp):
    grid = dw.ScoreDotGrid(2026)
    grid.set_ratings({dt.date(2026, 5, 1): 10.0, dt.date(2026, 5, 2): 1.0})

    assert grid._values[dt.date(2026, 5, 1)] == pytest.approx(1.0)
    assert grid._values[dt.date(2026, 5, 2)] == pytest.approx(0.28)
    assert grid.describe(dt.date(2026, 5, 1), 1.0) == "10.0"
    assert grid.describe(dt.date(2026, 6, 1), None) == "no entry"


# ── journal ──────────────────────────────────────────────────────────

def test_saving_a_journal_entry_writes_the_datetree(widget):
    widget.journal_body.setPlainText("a good day")
    widget.rating_input.setValue(7.3)

    widget._save_journal()

    entry = widget.journal.read_entry(dt.date.today())
    assert entry.rating == 7.3
    assert entry.body == "a good day"
    assert "saved" in widget.journal_status._full_text


def test_rating_is_required(widget):
    widget.journal_body.setPlainText("forgot to rate it")
    widget.rating_input.setValue(0.0)          # the "—" slot

    widget._save_journal()

    assert widget.journal.read_entry(dt.date.today()) is None
    assert "required" in widget.journal_status._full_text


def test_todays_entry_is_loaded_back_into_the_panel(widget):
    widget.journal.write_entry(dt.date.today(), 8.5, "written elsewhere")

    widget._reload_journal()

    assert widget.journal_body.toPlainText() == "written elsewhere"
    assert widget.rating_input.value() == pytest.approx(8.5)


def test_resaving_the_same_day_does_not_duplicate(widget):
    widget.rating_input.setValue(5.0)
    widget.journal_body.setPlainText("first")
    widget._save_journal()
    widget.rating_input.setValue(6.0)
    widget.journal_body.setPlainText("second")
    widget._save_journal()

    text = widget.journal.path.read_text(encoding="utf-8")
    assert text.count("**** Entry") == 1
    assert len(widget.journal.read_all()) == 1


def test_history_dialog_lists_the_months(widget, qapp):
    today = dt.date.today()
    widget.journal.write_entry(today, 7.0, "this month")

    dialog = dw.JournalHistoryDialog(widget.journal, widget.scaler,
                                     widget.body_font, widget.title_font, widget)

    labels = [dialog.month_combo.itemText(i) for i in range(dialog.month_combo.count())]
    assert any(str(today.year) in label for label in labels)
    assert dialog.month_combo.currentData() == (today.year, today.month)
    dialog.close()


# ── cache-fed panels ─────────────────────────────────────────────────

def write_cache(payload):
    dw.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    dw.CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")


def test_cache_renders_into_the_read_only_panels(widget):
    monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    write_cache({
        "generated_at": "2026-08-16T09:30:00",
        "weather": {"temp_c": 21.4, "weather_code": 2, "high_c": 24, "low_c": 14,
                    "wind_kph": 11, "hourly_temps": [14, 16, 19, 21, 22, 20]},
        "greed": {"score": 62, "rating": "Greed", "previous_close": 55},
        "sectors": {"sp500": [{"ticker": "XLK", "label": "Technology", "change_pct": 1.2},
                              {"ticker": "XLE", "label": "Energy", "change_pct": -0.8}],
                    "tsx": [{"ticker": "XIT.TO", "label": "Technology", "change_pct": None}]},
        "calendar": {"week_start": monday.isoformat(),
                     "week_end": (monday + dt.timedelta(days=6)).isoformat(),
                     "days": [{"date": (monday + dt.timedelta(days=i)).isoformat(),
                               "events": ([{"summary": "standup", "start": "09:00",
                                            "end": "09:15", "all_day": False,
                                            "location": ""}] if i == 0 else [])}
                              for i in range(7)]},
    })

    widget._load_cache()

    assert widget.temp_label.text() == "21°"
    assert widget.cond_label._full_text == "partly cloudy"
    assert "h 24°" in widget.hilo_label._full_text
    assert widget.greed_score.text() == "62"
    assert "+7 vs. yesterday" == widget.greed_delta._full_text
    assert "last sync" in widget.status_label._full_text
    assert widget.calendar_hint.isVisible() is False


def test_calendar_setup_notice_is_shown(widget):
    write_cache({"generated_at": "2026-08-16T09:30:00",
                 "calendar": {"error": "missing credentials.json", "needs_setup": True,
                              "setup_steps": ["make a project", "enable the api"]}})

    widget._load_cache()

    assert "one-time setup" in widget.calendar_hint.text()
    assert "enable the api" in widget.calendar_hint.text()


def test_failed_collector_fields_render_as_messages(widget):
    write_cache({"generated_at": "2026-08-16T09:30:00",
                 "weather": {"error": "HTTPSConnectionPool(host='api')"},
                 "greed": {"error": "timeout"},
                 "sectors": {}, "calendar": {}})

    widget._load_cache()

    assert widget.temp_label.text() == "--°"
    assert "unavailable" in widget.cond_label._full_text
    assert widget.greed_score.text() == "--"


def test_missing_cache_is_not_fatal(widget):
    if dw.CACHE_PATH.exists():
        dw.CACHE_PATH.unlink()
    widget._load_cache()
    assert "no cache file yet" in widget.status_label._full_text


# ── missing orgparse ─────────────────────────────────────────────────

def test_the_window_still_launches_without_orgparse(tmp_path):
    """The whole app used to die at import with ModuleNotFoundError. Now the
    journal panel disables itself and everything else comes up."""
    import os
    import subprocess
    import sys

    repo = str(Path(__file__).resolve().parent.parent)
    script = f'''
import sys
sys.modules["orgparse"] = None      # makes `import orgparse` raise ImportError
sys.path.insert(0, {repo!r})
from PyQt6.QtWidgets import QApplication
import dashboard_widget as dw
app = QApplication([])
window = dw.DashboardWidget()
assert window.journal_save_btn.isEnabled() is False, "save button should be off"
assert window.journal_history_btn.isEnabled() is False, "history should be off"
assert window.todo_input.isEnabled() is True, "the rest must still work"
print("STATUS:" + window.journal_status._full_text)
'''
    env = {**os.environ,
           "QT_QPA_PLATFORM": "offscreen",
           "DASHBOARD_DATA_DIR": str(tmp_path / "data"),
           "DASHBOARD_NOTES_DIR": str(tmp_path / "notes")}
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert "pip install orgparse" in result.stdout


# ── refreshing after a reboot or a sleep ─────────────────────────────

def fresh_cache(minutes_old=0):
    stamp = (dt.datetime.now() - dt.timedelta(minutes=minutes_old))
    write_cache({"generated_at": stamp.isoformat(timespec="seconds"),
                 "weather": {}, "greed": {}, "sectors": {}, "calendar": {}})


def test_a_stale_cache_refreshes_on_wake(widget, never_spawn_the_collector):
    fresh_cache(minutes_old=180)          # machine slept for three hours
    widget._load_cache()
    never_spawn_the_collector.clear()

    now = dt.datetime.now()
    widget._detect_wake(now - dt.timedelta(hours=3))   # seed the previous tick
    widget._detect_wake(now)

    assert len(never_spawn_the_collector) == 1
    assert "after wake" in widget.status_label._full_text


def test_a_normal_tick_refreshes_nothing(widget, never_spawn_the_collector):
    fresh_cache(minutes_old=180)
    widget._load_cache()
    never_spawn_the_collector.clear()

    now = dt.datetime.now()
    widget._detect_wake(now)
    widget._detect_wake(now + dt.timedelta(seconds=1))

    assert never_spawn_the_collector == []


def test_a_short_nap_with_fresh_data_refreshes_nothing(widget,
                                                       never_spawn_the_collector):
    fresh_cache(minutes_old=2)            # collector ran just before sleeping
    widget._load_cache()
    never_spawn_the_collector.clear()

    now = dt.datetime.now()
    widget._detect_wake(now - dt.timedelta(minutes=3))
    widget._detect_wake(now)

    assert never_spawn_the_collector == []


def test_wake_refresh_respects_the_auto_refresh_toggle(widget,
                                                       never_spawn_the_collector):
    fresh_cache(minutes_old=180)
    widget._load_cache()
    widget._toggle_auto_refresh(False)
    never_spawn_the_collector.clear()

    now = dt.datetime.now()
    widget._detect_wake(now - dt.timedelta(hours=3))
    widget._detect_wake(now)

    assert never_spawn_the_collector == []
    widget._toggle_auto_refresh(True)


def test_launching_with_an_old_cache_refreshes(qapp, never_spawn_the_collector):
    fresh_cache(minutes_old=600)          # machine was off overnight
    never_spawn_the_collector.clear()

    window = dw.DashboardWidget()

    assert len(never_spawn_the_collector) == 1
    assert "startup" in window.status_label._full_text
    window.close()


def test_launching_with_a_fresh_cache_does_not(qapp, never_spawn_the_collector):
    fresh_cache(minutes_old=1)            # just restarted the widget
    never_spawn_the_collector.clear()

    window = dw.DashboardWidget()

    assert never_spawn_the_collector == []
    window.close()


def test_launching_with_no_cache_at_all_refreshes(qapp, never_spawn_the_collector):
    if dw.CACHE_PATH.exists():
        dw.CACHE_PATH.unlink()
    never_spawn_the_collector.clear()

    window = dw.DashboardWidget()

    assert len(never_spawn_the_collector) == 1
    window.close()


def test_cache_age_is_reported_in_minutes(widget):
    fresh_cache(minutes_old=45)
    widget._load_cache()

    assert 44 < widget.cache_age_minutes() < 46


# ── startup failures must be visible ─────────────────────────────────

def test_a_startup_failure_is_written_to_the_crash_log(monkeypatch, tmp_path):
    shown = {}
    monkeypatch.setattr(dw.CRASH_LOG.__class__, "write_text",
                        lambda self, text, **kw: shown.update(logged=text))
    monkeypatch.setattr(dw.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.update(dialog=a[2])))

    code = dw.report_startup_failure(ImportError("no such name"), "traceback here")

    assert code == 1
    assert "traceback here" in shown["logged"]
    assert "no such name" in shown["dialog"]


def test_a_stale_sibling_module_reports_instead_of_dying_silently(tmp_path):
    """Under pythonw there's no console, so an import error used to mean the
    app simply never appeared with nothing to go on."""
    import os
    import shutil
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    staged = tmp_path / "app"
    staged.mkdir()
    for module in repo.glob("*.py"):
        shutil.copy(module, staged / module.name)
    # Roll paths.py back to a version that predates a constant the widget needs.
    stale = (staged / "paths.py").read_text(encoding="utf-8")
    (staged / "paths.py").write_text(
        stale.split("# ── Google ↔ Outlook two-way sync")[0], encoding="utf-8")

    script = '''
import sys
from PyQt6.QtWidgets import QMessageBox
QMessageBox.critical = staticmethod(lambda *a, **k: print("DIALOG:" + a[2]))
import dashboard_widget as dw
sys.exit(dw.main())
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=staged, capture_output=True, text=True,
        timeout=120, env={**os.environ, "QT_QPA_PLATFORM": "offscreen",
                          "DASHBOARD_DATA_DIR": str(tmp_path / "data"),
                          "DASHBOARD_NOTES_DIR": str(tmp_path / "notes")})

    assert result.returncode == 1
    assert "SYNC_STATUS_PATH" in result.stdout          # named in the dialog
    assert "different versions" in result.stdout        # and what to do
    assert (staged / "dashboard_crash.log").exists()


# ── the collector must not flash a console ───────────────────────────

def test_windows_gets_the_no_window_flag(monkeypatch):
    monkeypatch.setattr(dw.sys, "platform", "win32")
    assert dw.no_window_kwargs() == {"creationflags": 0x08000000}


def test_other_platforms_pass_nothing_extra(monkeypatch):
    monkeypatch.setattr(dw.sys, "platform", "linux")
    assert dw.no_window_kwargs() == {}


def test_the_collector_is_spawned_without_a_console(monkeypatch):
    """A refresh under pythonw would otherwise pop a terminal window."""
    seen = {}

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(dw.sys, "platform", "win32")
    monkeypatch.setattr(dw.subprocess, "run",
                        lambda *args, **kwargs: seen.update(kwargs) or Result())

    runner = dw.CollectorRunner()
    runner._run()

    assert seen.get("creationflags") == 0x08000000


# ── calendar sync status ─────────────────────────────────────────────

def write_sync_status(**fields):
    status = {"last_run_utc": None, "last_success_utc": None, "last_error": None,
              "events_synced_count": 0, "dry_run": False}
    status.update(fields)
    dw.SYNC_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    dw.SYNC_STATUS_PATH.write_text(json.dumps(status), encoding="utf-8")


def minutes_ago(count):
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=count)
    return moment.replace(microsecond=0).isoformat()


def test_a_recent_sync_shows_how_long_ago(widget):
    write_sync_status(last_run_utc=minutes_ago(4),
                      last_success_utc=minutes_ago(4), events_synced_count=3)

    widget._load_sync_status()

    assert widget.sync_label.text() == "synced 4m ago"
    assert "3 event" in widget.sync_label.toolTip()


def test_a_failed_sync_is_called_out(widget):
    write_sync_status(last_run_utc=minutes_ago(2),
                      last_success_utc=minutes_ago(90),
                      last_error="Graph GET 503: service unavailable")

    widget._load_sync_status()

    text = widget.sync_label.text()
    assert text.startswith("sync failed 2m ago")
    assert "last ok 1h ago" in text
    assert "503" in widget.sync_label.toolTip()


def test_a_dry_run_is_labelled_as_one(widget):
    write_sync_status(last_run_utc=minutes_ago(3),
                      last_success_utc=minutes_ago(3), dry_run=True)

    widget._load_sync_status()

    assert widget.sync_label.text() == "synced 3m ago (dry run)"


def test_no_status_file_means_no_sync_line(widget):
    if dw.SYNC_STATUS_PATH.exists():
        dw.SYNC_STATUS_PATH.unlink()

    widget._load_sync_status()

    assert widget.sync_label.text() == ""


def test_a_corrupt_status_file_is_ignored(widget):
    dw.SYNC_STATUS_PATH.write_text("{not json", encoding="utf-8")

    widget._load_sync_status()

    assert widget.sync_label.text() == ""


def test_the_status_file_is_watched_like_the_cache(widget):
    write_sync_status(last_run_utc=minutes_ago(3), last_success_utc=minutes_ago(3))

    widget._on_watched_change(str(dw.SYNC_STATUS_PATH))

    assert widget.sync_label.text() == "synced 3m ago"
    assert str(dw.SYNC_STATUS_PATH) in widget.watcher.files()


@pytest.mark.parametrize("seconds,expected", [
    (5, "5s ago"), (89, "89s ago"), (120, "2m ago"),
    (5399, "89m ago"), (7200, "2h ago"), (172801, "2d ago"),
])
def test_relative_times_read_naturally(seconds, expected):
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    assert dw._ago(moment.isoformat()) == expected


def test_a_clock_skewed_future_timestamp_does_not_read_as_negative():
    ahead = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    assert dw._ago(ahead) == "just now"


# ── rough notes ──────────────────────────────────────────────────────

def test_rough_notes_save_and_reload(widget):
    widget.notes_edit.setPlainText("remember the milk")
    widget._save_notes()

    assert dw.ROUGH_NOTES_PATH.read_text(encoding="utf-8") == "remember the milk"
    widget.notes_edit.setPlainText("")
    widget._load_notes()
    assert widget.notes_edit.toPlainText() == "remember the milk"


def test_typing_queues_a_debounced_save(widget):
    widget.notes_edit.setPlainText("typing…")
    assert widget._notes_timer.isActive() is True

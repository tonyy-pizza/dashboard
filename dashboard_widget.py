#!/usr/bin/env python3
"""
Dashboard Widget — joputer edition (standalone, frameless)

Frameless window with a custom title bar (drag-to-move via the header) and a
QSizeGrip in the corner for resizing. No OS chrome, but still shows up in the
taskbar like a normal app.

Two data flows, deliberately separate:

  read-only panels   collector.py → cache.json → QFileSystemWatcher → repaint
  (Weather, Greed,   No polling. The ↻ button spawns collector.py through
   Sectors, Calendar) CollectorRunner; the watcher does the rest.

  interactive panels  widget → todo.json / habits.json / journal.org
  (To Do, Habits,     Written straight from here. These never round-trip
   Journal, Notes)    through the collector or the watcher — a click has to
                      show up immediately, and cache.json stays
                      collector-owned.

The only timers are the title-bar clock, a debounce for the Rough Notes
autosave, and the background collector refresh (which re-runs the collector on
an interval so the calendar and market data stay fresh — it does not poll
cache.json).

Fonts: drop Inter and Fraunces .ttf files into a `fonts/` folder next to this
script (Google Fonts, OFL) and they load automatically; otherwise the widget
falls back to installed system fonts.

Setup:
    pip install PyQt6 orgparse

Launch (startup shortcut target):
    pythonw.exe dashboard_widget.py
"""

import datetime as dt
import json
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import (
    QEvent, QFileSystemWatcher, QMimeData, QObject, QPoint, QPointF, QRect,
    QRectF, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QDrag, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizeGrip, QSizePolicy, QVBoxLayout, QWidget,
)

CRASH_LOG = Path(__file__).resolve().parent / "dashboard_crash.log"

# The sibling modules. If one of them is missing or out of date relative to
# this file, the import blows up before anything can be drawn — and under
# pythonw there's no console for the traceback to land in, so the app just
# silently never appears. Catch it and report it in main() instead.
try:
    import theme
    from habit_store import HabitStore, year_dates
    from journal_store import JournalStore, JournalUnavailable
    from paths import (
        CACHE_PATH, COLLECTOR_SCRIPT, ROUGH_NOTES_PATH, SYNC_STATUS_PATH,
    )
    from storage import atomic_write_text, load_json
    from theme import (
        BG, BORDER, CHROME, CREAM, CREAM_DIM, DONE_TEXT, DOT_EMPTY, FAINT,
        HOVER, PANEL_BG, WHITE,
    )
    from todo_store import (
        MAX_PRIORITY, PRIORITY_MARKS, PRIORITY_NAMES, TodoStore,
    )
except Exception as import_error:          # reported by main(), see below
    STARTUP_ERROR = (import_error, traceback.format_exc())
else:
    STARTUP_ERROR = None

PYTHON_EXE = "py"
BASE_W = 1000            # design reference width for proportional scaling
VERSION = "v3.0"
AUTO_REFRESH_MINUTES = 20
NOTES_SAVE_DELAY_MS = 800
TODO_MIME = "application/x-dionysus-todo"

DAYS_SHORT = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS_SHORT = ["jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]

# WMO weather codes (Open-Meteo's `weather_code` field) → readable sky state
WMO_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def soft_wrap(text):
    """Exception strings contain long unbreakable runs like
    HTTPSConnectionPool(host=... — zero-width spaces after punctuation let
    word-wrap break them on narrow windows."""
    return re.sub(r"([./,()'=:])", "\\1" + "​", str(text))


def make_check_box(size=13):
    """The dashboard's own check indicator — a hard-cornered square that
    fills cream when set. Returns (frame, fill); pass both to set_check_box.
    Native QCheckBox brings its own platform chrome, which fights the skin."""
    box = QFrame()
    box.setFixedSize(size, size)
    box.setCursor(Qt.CursorShape.PointingHandCursor)
    layout = QHBoxLayout(box)
    layout.setContentsMargins(3, 3, 3, 3)
    fill = QFrame()
    layout.addWidget(fill)
    return box, fill


def set_check_box(box, fill, checked):
    fill.setVisible(checked)
    fill.setStyleSheet(f"background: {CREAM}; border: none;")
    box.setStyleSheet(f"border: 1px solid {CHROME if checked else BORDER}; "
                      f"background: transparent;")


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            # Detach before deleteLater — a widget that's merely removed from
            # the layout keeps painting at its old position until the event
            # loop processes the deferred delete.
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def _ago(iso_str):
    """'2026-08-16T18:40:00+00:00' → '4m ago'. None when unparseable."""
    if not iso_str:
        return None
    try:
        when = dt.datetime.fromisoformat(str(iso_str))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    seconds = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    if seconds < 0:
        return "just now"
    for limit, size, unit in ((90, 1, "s"), (5400, 60, "m"),
                              (172800, 3600, "h")):
        if seconds < limit:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds // 86400)}d ago"


def _sync_stamp(iso_str):
    """Collector timestamp → '09:42' today, '14 aug 09:42' before that."""
    if not iso_str:
        return "unknown"
    try:
        when = dt.datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return str(iso_str)
    if when.date() == dt.date.today():
        return when.strftime("%H:%M")
    return f"{when.day:02d} {MONTHS_SHORT[when.month - 1]} {when.strftime('%H:%M')}"


class FontScaler:
    """Pixel-sized fonts that follow the window width.

    Widgets registered here get a fresh font on every resize; unregistered
    fonts (dialogs, painted text) just take the current factor once.
    """

    def __init__(self):
        self._registry = []      # (widget, family, px, spacing, tabular)
        self.factor = 1.0
        self.compact = False

    def set_width(self, width):
        self.factor = max(0.55, min(width / BASE_W, 1.6))
        self.reapply()

    def set_compact(self, compact):
        self.compact = compact
        self.reapply()

    def px(self, base):
        base = max(6, base - 1) if self.compact else base
        return max(6, round(base * self.factor))

    def font(self, family, px, spacing=0.0, tabular_nums=False, register=None):
        font = self._build(family, px, spacing, tabular_nums)
        if register is not None:
            self._registry.append((register, family, px, spacing, tabular_nums))
            register.setFont(font)
        return font

    def reapply(self):
        alive = []
        for entry in self._registry:
            widget, family, px, spacing, tabular_nums = entry
            try:
                widget.setFont(self._build(family, px, spacing, tabular_nums))
                alive.append(entry)
            except RuntimeError:
                pass  # widget was rebuilt and deleted underneath us
        self._registry = alive

    def _build(self, family, px, spacing, tabular_nums):
        font = QFont(family)
        font.setPixelSize(self.px(px))
        if spacing:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing,
                                  spacing * self.factor)
        if tabular_nums:
            theme.tabular(font)
        return font


# ─────────────────────────────────────────────────────────────────────────
# Background collector runner
# ─────────────────────────────────────────────────────────────────────────

def no_window_kwargs() -> dict:
    """Keep a spawned console program from flashing a window.

    `py` is the console launcher, so Windows gives it a console of its own
    even though our output is piped and the parent is `pythonw` with no
    console to inherit. CREATE_NO_WINDOW suppresses that; on anything else
    there's nothing to suppress.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


class CollectorRunner(QObject):
    finished = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self._running = False

    @property
    def running(self):
        return self._running

    def run_async(self):
        if self._running:
            return False
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            result = subprocess.run(
                [PYTHON_EXE, str(COLLECTOR_SCRIPT)],
                capture_output=True, text=True, timeout=300,
                **no_window_kwargs(),
            )
            if result.returncode == 0:
                self.finished.emit(True, "refreshed.")
            else:
                self.finished.emit(False, f"collector error: {result.stderr[-200:]}")
        except Exception as e:
            self.finished.emit(False, f"failed to run collector: {e}")
        finally:
            self._running = False


# ─────────────────────────────────────────────────────────────────────────
# Elided label — QLabel refuses to shrink below its full text width by
# default. This subclass reports the full text width as its sizeHint (so it
# takes its natural width when there's room) but a near-zero
# minimumSizeHint (so the layout may compress it), re-truncating with "…" on
# every resize. Both hints are computed from _full_text, never from the
# currently displayed elided text — otherwise the hint shrinks along with the
# label and the column can never grow back when the window widens.
# ─────────────────────────────────────────────────────────────────────────

class ElidedLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)
        super().setText(text)

    def setText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.updateGeometry()
        self._update_elided()

    def sizeHint(self):
        # +4px headroom: contentsRect is slightly narrower than the widget,
        # and a hint of exactly the text advance still elides the last char.
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full_text) + 4)
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance("…"))
        return hint

    def resizeEvent(self, event):
        self._update_elided()
        super().resizeEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.FontChange:
            self.updateGeometry()
            self._update_elided()
        super().changeEvent(event)

    def _update_elided(self):
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )
        super().setText(elided)


# ─────────────────────────────────────────────────────────────────────────
# Custom-painted pieces
# ─────────────────────────────────────────────────────────────────────────

class Sparkline(QWidget):
    """Today's hourly temperature curve under the Weather readout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = []
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values):
        self._values = [float(v) for v in (values or []) if isinstance(v, (int, float))]
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawLine(0, height - 1, width, height - 1)
        if len(self._values) < 2:
            return
        low, high = min(self._values), max(self._values)
        span = (high - low) or 1.0
        top, bottom = 3, height - 5
        points = [
            QPointF(i * (width - 1) / (len(self._values) - 1),
                    bottom - (value - low) / span * (bottom - top))
            for i, value in enumerate(self._values)
        ]
        pen = QPen(QColor(CREAM), 1.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF(points))


class GreedMeter(QWidget):
    """Horizontal 0–100 fear/greed scale with a marker at the current score."""

    def __init__(self, scaler, body_family, parent=None):
        super().__init__(parent)
        self._score = None
        self._scaler = scaler
        self._body = body_family
        # Bar + marker + the 0/50/100 tick row underneath.
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_score(self, score):
        self._score = score
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        bar_y, bar_h = 6, 6

        painter.fillRect(0, bar_y, width, bar_h, QColor(DOT_EMPTY))
        if self._score is not None:
            filled = round(width * max(0, min(100, self._score)) / 100)
            painter.fillRect(0, bar_y, filled, bar_h, QColor(CREAM))
            marker = QPolygonF([
                QPointF(filled - 4, bar_y + bar_h + 2),
                QPointF(filled + 4, bar_y + bar_h + 2),
                QPointF(filled, bar_y + bar_h + 8),
            ])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(WHITE))
            painter.drawPolygon(marker)

        font = self._scaler.font(self._body, theme.CAPTION_PX, tabular_nums=True)
        painter.setFont(font)
        painter.setPen(QColor(FAINT))
        label_y = bar_y + bar_h + 9
        for value, align in ((0, Qt.AlignmentFlag.AlignLeft),
                             (50, Qt.AlignmentFlag.AlignHCenter),
                             (100, Qt.AlignmentFlag.AlignRight)):
            painter.drawText(QRect(0, label_y, width, 14),
                             align | Qt.AlignmentFlag.AlignVCenter, str(value))


class DivergingBar(QWidget):
    """Sector bar diverging from a center axis: gains right, losses left."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct = None
        self._max_abs = 1.0
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, pct, max_abs):
        self._pct = pct
        self._max_abs = max_abs or 1.0
        self.update()

    def color(self):
        # The palette has no red/green, so direction is carried by which side
        # of the axis the bar sits on; brightness only separates up from down.
        if self._pct is None or abs(self._pct) <= 0.1:
            return FAINT
        return CREAM if self._pct > 0 else CHROME

    def paintEvent(self, _event):
        painter = QPainter(self)
        width, height = self.width(), self.height()
        center = width // 2
        painter.fillRect(center, 0, 1, height, QColor(BORDER))
        if self._pct is None or abs(self._pct) <= 0.005:
            return
        half = max(center - 3, 1)
        length = round(min(abs(self._pct), self._max_abs) / self._max_abs * half)
        color = QColor(self.color())
        if self._pct > 0:
            painter.fillRect(center + 2, 1, length, height - 2, color)
        else:
            painter.fillRect(center - 1 - length, 1, length, height - 2, color)


class DotGrid(QWidget):
    """GitHub-contribution-style grid for one calendar year.

    Columns are ISO weeks, rows are Mon→Sun. Values are date → intensity:
    the habit tracker passes 1.0 for done (binary, single cream accent), the
    journal history passes a 0–1 scale of the day's rating.
    """

    dayClicked = pyqtSignal(object)

    # Small and compact: the habit panel sits in a half-width column now, so
    # a full year of dots has to fit in roughly 450px.
    MIN_CELL = 4
    MAX_CELL = 8
    GAP = 2
    LABEL_H = 11

    def __init__(self, year, interactive=True, show_months=True, parent=None):
        super().__init__(parent)
        self._year = year
        self._values = {}
        self._interactive = interactive
        self._show_months = show_months
        self._cell = 9
        self._label_font = QFont()
        self._color_cache = {}
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rebuild_dates()

    # ── data ─────────────────────────────────────────────────────────
    def set_year(self, year):
        if year != self._year:
            self._year = year
            self._rebuild_dates()
            self.update()

    def set_values(self, values):
        """date → True/False, or date → 0..1 intensity."""
        self._values = {
            date: (1.0 if value is True else float(value))
            for date, value in (values or {}).items()
            if value
        }
        self.update()

    def set_label_font(self, font):
        self._label_font = font
        self.update()

    def _rebuild_dates(self):
        # calendar.isleap decides 365 vs 366 — nothing here assumes 365.
        self._dates = year_dates(self._year)
        first = self._dates[0]
        self._grid_start = first - dt.timedelta(days=first.weekday())
        span = (self._dates[-1] - self._grid_start).days
        self._columns = span // 7 + 1
        self._resize_cells(self.width())

    # ── geometry ─────────────────────────────────────────────────────
    def _top(self):
        return self.LABEL_H if self._show_months and self._cell >= 7 else 0

    def _resize_cells(self, width):
        usable = max(width, self._columns * self.MIN_CELL)
        cell = usable // self._columns if self._columns else self.MAX_CELL
        self._cell = max(self.MIN_CELL, min(self.MAX_CELL, cell))
        self.setFixedHeight(self._top() + 7 * self._cell)

    def resizeEvent(self, event):
        self._resize_cells(event.size().width())
        super().resizeEvent(event)

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(self._columns * 9)
        hint.setHeight(self._top() + 7 * self._cell)
        return hint

    def _date_at(self, pos):
        top = self._top()
        if pos.y() < top or self._cell <= 0:
            return None
        column = int(pos.x()) // self._cell
        row = (int(pos.y()) - top) // self._cell
        if not (0 <= row < 7) or column < 0 or column >= self._columns:
            return None
        date = self._grid_start + dt.timedelta(days=column * 7 + row)
        return date if date.year == self._year else None

    # ── interaction ──────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if not self._interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        # Backfilling past days is the point of the year view, so any date in
        # the grid is clickable, not just today.
        date = self._date_at(event.position())
        if date is not None:
            self.dayClicked.emit(date)

    def mouseMoveEvent(self, event):
        date = self._date_at(event.position())
        if date is None:
            self.setToolTip("")
            return
        value = self._values.get(date)
        state = self.describe(date, value)
        self.setToolTip(f"{date.isoformat()} · {state}")

    def describe(self, _date, value):
        return "done" if value else "not done"

    # ── painting ─────────────────────────────────────────────────────
    def _fill_color(self, value):
        """Cream at full intensity, shaded toward the empty dot below that.
        Quantized to 20 steps so a year of dots reuses a handful of QColors."""
        step = round(max(0.0, min(1.0, value)) * 20)
        if step not in self._color_cache:
            self._color_cache[step] = QColor(theme.mix(DOT_EMPTY, CREAM, step / 20))
        return self._color_cache[step]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell, top = self._cell, self._top()
        size = max(2, cell - self.GAP)
        today = dt.date.today()
        empty = QColor(DOT_EMPTY)

        painter.setPen(Qt.PenStyle.NoPen)
        for date in self._dates:
            offset = (date - self._grid_start).days
            x = (offset // 7) * cell
            y = top + (offset % 7) * cell
            value = self._values.get(date)
            painter.setBrush(self._fill_color(value) if value else empty)
            painter.drawEllipse(QRectF(x, y, size, size))

        if self._dates[0] <= today <= self._dates[-1]:
            offset = (today - self._grid_start).days
            painter.setPen(QPen(QColor(CHROME), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF((offset // 7) * cell - 1,
                                       top + (offset % 7) * cell - 1,
                                       size + 2, size + 2))

        if top:
            painter.setFont(self._label_font)
            painter.setPen(QColor(FAINT))
            for month in range(1, 13):
                first = dt.date(self._year, month, 1)
                x = ((first - self._grid_start).days // 7) * cell
                painter.drawText(QRect(x, 0, cell * 5, self.LABEL_H),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 MONTHS_SHORT[month - 1])


class ScoreDotGrid(DotGrid):
    """Journal ratings as a year grid — same language as the habit tracker,
    but shaded by score instead of binary on/off."""

    def __init__(self, year, parent=None):
        super().__init__(year, interactive=False, show_months=True, parent=parent)
        self._ratings = {}

    def set_ratings(self, ratings):
        self._ratings = dict(ratings or {})
        # 1.0 → faintest still-visible dot, 10.0 → full cream.
        self.set_values({date: 0.28 + 0.72 * (rating - 1) / 9
                         for date, rating in self._ratings.items()
                         if rating is not None})

    def describe(self, date, _value):
        rating = self._ratings.get(date)
        return f"{rating:.1f}" if rating is not None else "no entry"


# ─────────────────────────────────────────────────────────────────────────
# To Do
# ─────────────────────────────────────────────────────────────────────────

class TodoRow(QWidget):
    """One to-do: click the box to complete (strikethrough, stays visible),
    ✕ to delete for good, drag to reorder within its tier."""

    toggled = pyqtSignal(str, bool)
    deleted = pyqtSignal(str)
    priorityChanged = pyqtSignal(str, int)

    def __init__(self, item, scaler, body_family, parent=None):
        super().__init__(parent)
        self.item = item
        self._scaler = scaler
        self._press_pos = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.box, self.fill = make_check_box()
        layout.addWidget(self.box, 0, Qt.AlignmentFlag.AlignTop)

        self.label = QLabel(item.text)
        self.label.setWordWrap(True)
        # Window resizes replace the label's font (proportional scaling),
        # which would clear the strikeout — re-assert it on every font change.
        self.label.installEventFilter(self)
        scaler.font(body_family, theme.BODY_PX, register=self.label)
        layout.addWidget(self.label, 1)

        self.delete_btn = QPushButton("✕")
        self.delete_btn.setFixedSize(16, 16)
        self.delete_btn.setToolTip("delete — immediate, no undo")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {FAINT};
                border: none; font-size: 11px; }}
            QPushButton:hover {{ color: {WHITE}; }}
        """)
        self.delete_btn.clicked.connect(lambda: self.deleted.emit(self.item.id))
        layout.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._apply_state()

    # ── completion ───────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self.label and event.type() == QEvent.Type.FontChange:
            font = self.label.font()
            if font.strikeOut() != self.item.done:
                font.setStrikeOut(self.item.done)
                self.label.setFont(font)
        return super().eventFilter(obj, event)

    def _apply_state(self):
        done = self.item.done
        set_check_box(self.box, self.fill, done)
        font = self.label.font()
        font.setStrikeOut(done)
        self.label.setFont(font)
        self.label.setStyleSheet(
            f"color: {DONE_TEXT if done else CREAM}; background: transparent;")

    # ── mouse: toggle, drag, context menu ────────────────────────────
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._press_pos = event.position().toPoint()
        if self.box.geometry().adjusted(-4, -4, 4, 4).contains(self._press_pos):
            self.item.done = not self.item.done
            self._apply_state()
            self.toggled.emit(self.item.id, self.item.done)
            self._press_pos = None

    def mouseMoveEvent(self, event):
        if self._press_pos is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        distance = (event.position().toPoint() - self._press_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            return
        mime = QMimeData()
        mime.setData(TODO_MIME, self.item.id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(self._press_pos)
        self._press_pos = None
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, _event):
        self._press_pos = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(theme.menu_style())
        for priority in range(MAX_PRIORITY, -1, -1):
            action = menu.addAction(PRIORITY_NAMES[priority])
            action.setCheckable(True)
            action.setChecked(priority == self.item.priority)
            action.triggered.connect(
                lambda _checked, p=priority: self.priorityChanged.emit(self.item.id, p))
        menu.addSeparator()
        menu.addAction("delete", lambda: self.deleted.emit(self.item.id))
        menu.exec(event.globalPos())


class TodayCheck(QWidget):
    """`[✓] today` — the one-click way to log a habit for the current day.

    Same thing as clicking today's dot in the year grid, without having to
    find it. Both stay in sync because a toggle rebuilds the whole panel.
    """

    clicked = pyqtSignal()

    def __init__(self, scaler, body_family, date, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"log this habit for today ({date.isoformat()})")
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.box, self.fill = make_check_box()
        layout.addWidget(self.box)

        self.label = QLabel("today")
        scaler.font(body_family, theme.CAPTION_PX, register=self.label)
        layout.addWidget(self.label)
        self.set_checked(False)

    def set_checked(self, checked):
        set_check_box(self.box, self.fill, checked)
        self.label.setStyleSheet(
            f"color: {CREAM if checked else FAINT}; background: transparent;")

    def mousePressEvent(self, event):
        # The label counts as part of the target — a 13px box is a small
        # thing to ask someone to hit every morning.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class TierList(QWidget):
    """Drop target for one priority tier: rows reorder by dragging inside it,
    and a drop from another tier moves the item into this one."""

    reordered = pyqtSignal(int, list)
    movedHere = pyqtSignal(str, int, int)

    def __init__(self, priority, parent=None):
        super().__init__(parent)
        self.priority = priority
        self._drop_row = None
        self.setAcceptDrops(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(7)

    def add_row(self, row):
        self._layout.addWidget(row)

    def rows(self):
        return [self._layout.itemAt(i).widget()
                for i in range(self._layout.count())
                if isinstance(self._layout.itemAt(i).widget(), TodoRow)]

    def _insert_index(self, y):
        rows = self.rows()
        for index, row in enumerate(rows):
            if y < row.geometry().center().y():
                return index
        return len(rows)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(TODO_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat(TODO_MIME):
            return
        self._drop_row = self._insert_index(event.position().y())
        self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, _event):
        self._drop_row = None
        self.update()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(TODO_MIME):
            return
        item_id = bytes(event.mimeData().data(TODO_MIME)).decode("utf-8")
        index = self._insert_index(event.position().y())
        self._drop_row = None
        self.update()
        event.acceptProposedAction()

        # Emit after the drag's own event loop has unwound. Rebuilding the
        # rows here would delete the widget that is still inside
        # QDrag.exec() further down the stack.
        ids = [row.item.id for row in self.rows()]
        if item_id not in ids:
            QTimer.singleShot(
                0, lambda: self.movedHere.emit(item_id, self.priority, index))
            return
        ids.remove(item_id)
        ids.insert(min(index, len(ids)), item_id)
        QTimer.singleShot(0, lambda: self.reordered.emit(self.priority, ids))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_row is None:
            return
        rows = self.rows()
        if not rows:
            y = 0
        elif self._drop_row >= len(rows):
            y = rows[-1].geometry().bottom() + 3
        else:
            y = rows[self._drop_row].geometry().top() - 3
        painter = QPainter(self)
        painter.fillRect(0, max(0, y), self.width(), 2, QColor(WHITE))


# ─────────────────────────────────────────────────────────────────────────
# Dialogs
# ─────────────────────────────────────────────────────────────────────────

class AddHabitDialog(QDialog):
    def __init__(self, scaler, body_family, title_family, parent=None):
        super().__init__(parent)
        self.setWindowTitle("new habit")
        self.setStyleSheet(theme.dialog_style(body_family))
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        heading = QLabel("new habit")
        scaler.font(title_family, theme.TITLE_PX, register=heading)
        layout.addWidget(heading)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("name (e.g. lift)")
        scaler.font(body_family, theme.BODY_PX, register=self.name_input)
        layout.addWidget(self.name_input)

        self.goal_input = QLineEdit()
        self.goal_input.setPlaceholderText("goal — optional (e.g. 4x / week)")
        scaler.font(body_family, theme.BODY_PX, register=self.goal_input)
        layout.addWidget(self.goal_input)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        add = QPushButton("add")
        add.setDefault(True)
        add.clicked.connect(self.accept)
        buttons.addWidget(add)
        layout.addLayout(buttons)

        self.name_input.returnPressed.connect(self.accept)
        self.goal_input.returnPressed.connect(self.accept)

    def values(self):
        return self.name_input.text().strip(), self.goal_input.text().strip()


class ArchivedHabitsDialog(QDialog):
    """The unhide view — archived habits keep their data and come back whole."""

    def __init__(self, store, scaler, body_family, title_family, parent=None):
        super().__init__(parent)
        self.store = store
        self._scaler = scaler
        self._body = body_family
        self.restored_any = False
        self.setWindowTitle("archived habits")
        self.setStyleSheet(theme.dialog_style(body_family))
        self.setMinimumSize(360, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        heading = QLabel("archived habits")
        scaler.font(title_family, theme.TITLE_PX, register=heading)
        layout.addWidget(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(theme.scrollbar_style())
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        close = QPushButton("close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)

        self._reload()

    def _reload(self):
        clear_layout(self.list_layout)
        archived = self.store.archived()
        if not archived:
            empty = QLabel("nothing archived.")
            empty.setStyleSheet(f"color: {FAINT};")
            self._scaler.font(self._body, theme.BODY_PX, register=empty)
            self.list_layout.addWidget(empty)
        for habit in archived:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            name = ElidedLabel(habit.name)
            self._scaler.font(self._body, theme.BODY_PX, register=name)
            row_layout.addWidget(name, 1)

            count = QLabel(f"{habit.done_count()} days kept")
            count.setStyleSheet(f"color: {FAINT};")
            self._scaler.font(self._body, theme.CAPTION_PX, tabular_nums=True, register=count)
            row_layout.addWidget(count)

            restore = QPushButton("unhide")
            restore.setStyleSheet(theme.button_style(self._body))
            restore.clicked.connect(lambda _checked, h=habit: self._restore(h.id))
            row_layout.addWidget(restore)

            self.list_layout.addWidget(row)
        self.list_layout.addStretch()

    def _restore(self, habit_id):
        self.store.set_archived(habit_id, False)
        self.restored_any = True
        self._reload()


class JournalHistoryDialog(QDialog):
    """Month dropdown over past entries, with the year's scores as a shaded
    dot grid above it."""

    def __init__(self, store, scaler, body_family, title_family, parent=None):
        super().__init__(parent)
        self.store = store
        self._scaler = scaler
        self._body = body_family
        self._title = title_family
        self.setWindowTitle("journal history")
        self.setStyleSheet(theme.dialog_style(body_family))
        self.setMinimumSize(460, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        heading = QLabel("journal history")
        scaler.font(title_family, theme.TITLE_PX, register=heading)
        layout.addWidget(heading)

        year = dt.date.today().year
        year_row = QHBoxLayout()
        year_label = QLabel(str(year))
        year_label.setStyleSheet(f"color: {FAINT};")
        scaler.font(body_family, theme.CAPTION_PX, tabular_nums=True, register=year_label)
        year_row.addWidget(year_label)
        year_row.addStretch()
        legend = QLabel("dim 1.0 → cream 10.0")
        legend.setStyleSheet(f"color: {FAINT};")
        scaler.font(body_family, theme.CAPTION_PX, register=legend)
        year_row.addWidget(legend)
        layout.addLayout(year_row)

        self.grid = ScoreDotGrid(year)
        self.grid.set_label_font(scaler.font(body_family, 8))
        self.grid.set_ratings(store.ratings_for_year(year))
        layout.addWidget(self.grid)

        picker = QHBoxLayout()
        picker.setSpacing(8)
        month_label = QLabel("month")
        month_label.setStyleSheet(f"color: {FAINT};")
        scaler.font(body_family, theme.SMALL_PX, register=month_label)
        picker.addWidget(month_label)
        self.month_combo = QComboBox()
        scaler.font(body_family, theme.BODY_PX, register=self.month_combo)
        self.month_combo.currentIndexChanged.connect(self._show_month)
        picker.addWidget(self.month_combo, 1)
        layout.addLayout(picker)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(theme.scrollbar_style())
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self.entries_layout = QVBoxLayout(holder)
        self.entries_layout.setContentsMargins(0, 0, 0, 0)
        self.entries_layout.setSpacing(12)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        close = QPushButton("close")
        close.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(close)
        layout.addLayout(close_row)

        self._populate_months()

    def _populate_months(self):
        months = self.store.months_with_entries()
        today = dt.date.today()
        if (today.year, today.month) not in months:
            months.insert(0, (today.year, today.month))
        self.month_combo.blockSignals(True)
        for year, month in months:
            self.month_combo.addItem(f"{MONTHS_LONG[month - 1]} {year}", (year, month))
        self.month_combo.blockSignals(False)
        if months:
            self.month_combo.setCurrentIndex(0)
            self._show_month(0)

    def _show_month(self, _index):
        clear_layout(self.entries_layout)
        selection = self.month_combo.currentData()
        if not selection:
            return
        entries = self.store.read_month(*selection)
        if not entries:
            empty = QLabel("no entries this month.")
            empty.setStyleSheet(f"color: {FAINT};")
            self._scaler.font(self._body, theme.BODY_PX, register=empty)
            self.entries_layout.addWidget(empty)
        for entry in entries:
            self.entries_layout.addWidget(self._entry_block(entry))
        self.entries_layout.addStretch()

    def _entry_block(self, entry):
        block = QWidget()
        block.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        header = QHBoxLayout()
        date_label = QLabel(f"{entry.date.day:02d} {DAYS_SHORT[entry.date.weekday()]}")
        self._scaler.font(self._title, theme.BODY_PX, register=date_label)
        header.addWidget(date_label)
        header.addStretch()
        score = QLabel("—" if entry.rating is None else f"{entry.rating:.1f}")
        score.setStyleSheet(f"color: {CHROME};")
        self._scaler.font(self._body, theme.BODY_PX, tabular_nums=True, register=score)
        header.addWidget(score)
        layout.addLayout(header)

        body = QLabel(entry.body or "—")
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {CREAM_DIM};")
        self._scaler.font(self._body, theme.SMALL_PX, register=body)
        layout.addWidget(body)
        return block


# ─────────────────────────────────────────────────────────────────────────
# Calendar week grid
# ─────────────────────────────────────────────────────────────────────────

class WeekGrid(QWidget):
    """Read-only mini grid of the current week — one column per day."""

    MAX_EVENTS = 7

    def __init__(self, scaler, body_family, title_family, parent=None):
        super().__init__(parent)
        self._scaler = scaler
        self._body = body_family
        self._title = title_family
        # The calendar is the anchor of the top row, so the columns get real
        # height whether or not there is anything in them this week.
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: transparent;")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)

    def render(self, data):
        clear_layout(self._layout)
        data = data or {}
        days = data.get("days") or []
        if not days:
            monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
            days = [{"date": (monday + dt.timedelta(days=i)).isoformat(), "events": []}
                    for i in range(7)]
        for day in days:
            self._layout.addWidget(self._day_column(day), 1)

    def _day_column(self, day):
        try:
            date = dt.date.fromisoformat(day.get("date", ""))
        except ValueError:
            date = dt.date.today()
        is_today = date == dt.date.today()

        column = QFrame()
        column.setObjectName("day")
        column.setStyleSheet(
            f"QFrame#day {{ background: {BG if is_today else 'transparent'}; "
            f"border: 1px solid {CHROME if is_today else BORDER}; }}")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(6, 5, 6, 6)
        layout.setSpacing(4)

        header = QLabel(f"{DAYS_SHORT[date.weekday()]} {date.day:02d}")
        header.setStyleSheet(f"color: {WHITE if is_today else FAINT}; background: transparent;")
        self._scaler.font(self._body, theme.CAPTION_PX, spacing=1,
                          tabular_nums=True, register=header)
        layout.addWidget(header)

        events = day.get("events") or []
        for event in events[:self.MAX_EVENTS]:
            layout.addWidget(self._event_label(event))
        if len(events) > self.MAX_EVENTS:
            more = QLabel(f"+{len(events) - self.MAX_EVENTS} more")
            more.setStyleSheet(f"color: {FAINT}; background: transparent;")
            self._scaler.font(self._body, theme.CAPTION_PX, register=more)
            layout.addWidget(more)
        if not events:
            blank = QLabel("—")
            blank.setStyleSheet(f"color: {BORDER}; background: transparent;")
            self._scaler.font(self._body, theme.CAPTION_PX, register=blank)
            layout.addWidget(blank)
        layout.addStretch()
        return column

    def _event_label(self, event):
        summary = event.get("summary", "(no title)")
        start = event.get("start")
        text = summary if event.get("all_day") or not start else f"{start} {summary}"
        label = ElidedLabel(text)
        # Event titles come from the calendar feed, not from us. QLabel's
        # AutoText would render anything that looks like HTML — including
        # <img src="http://…">, which would phone home on every refresh.
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setToolTip(self._tooltip(event))
        label.setStyleSheet(f"color: {CREAM}; background: transparent;")
        self._scaler.font(self._body, theme.CAPTION_PX, tabular_nums=True, register=label)
        return label

    @staticmethod
    def _tooltip(event):
        parts = [event.get("summary", "")]
        if event.get("all_day"):
            parts.append("all day")
        elif event.get("start"):
            span = event["start"]
            if event.get("end"):
                span += f"–{event['end']}"
            parts.append(span)
        if event.get("location"):
            parts.append(event["location"])
        return " · ".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────
# Custom title bar — drag handle since there's no OS chrome
# ─────────────────────────────────────────────────────────────────────────

class TitleBar(QWidget):
    def __init__(self, parent_window):
        super().__init__()
        self._win = parent_window
        self._drag_pos = None
        self.setFixedHeight(40)
        # Plain-QWidget subclasses don't paint stylesheet backgrounds unless
        # WA_StyledBackground is set.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {PANEL_BG}; border-bottom: 1px solid {BORDER};")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (event.globalPosition().toPoint()
                              - self._win.frameGeometry().topLeft())

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, _event):
        self._drag_pos = None


# ─────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────

class DashboardWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Dashboard")
        self.setMinimumSize(620, 520)
        self.resize(1040, 900)

        self.scaler = FontScaler()
        self._always_on_top = False
        self._auto_refresh = True
        self.title_font = theme.pick_font(theme.TITLE_FAMILIES)
        self.body_font = theme.pick_font(theme.BODY_FAMILIES)

        self.todos = TodoStore()
        self.habits = HabitStore()
        self.journal = JournalStore()
        self._new_todo_priority = 0
        self._journal_saved_at = None

        self.runner = CollectorRunner()
        self.runner.finished.connect(self._on_refresh_done)

        self._build_ui()
        self._setup_watcher()
        self._load_cache()
        self._load_sync_status()
        self._reload_todos()
        self._reload_habits()
        self._reload_journal()
        self._load_notes()
        self.scaler.set_width(self.width())

        # Title-bar wall clock. Not data polling — it never reads cache.json.
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

        # Background data refresh. This re-runs the collector on an interval
        # (calendar + market data go stale otherwise); the display still only
        # updates when QFileSystemWatcher sees the new cache.json.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._auto_refresh_tick)
        self._refresh_timer.start(AUTO_REFRESH_MINUTES * 60 * 1000)

        # Rough Notes autosave debounce.
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.timeout.connect(self._save_notes)

    # ── UI construction ──────────────────────────────────────────────
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 10, 16, 6)
        body_layout.setSpacing(8)

        # Elided: collector error messages can be hundreds of characters and
        # would otherwise set the window's minimum width (tooltip has it all).
        self.status_label = ElidedLabel("")
        self.status_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.CAPTION_PX, register=self.status_label)
        body_layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(theme.scrollbar_style())
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        grid = QVBoxLayout(content)
        grid.setContentsMargins(0, 0, 6, 0)
        grid.setSpacing(10)
        scroll.setWidget(content)
        body_layout.addWidget(scroll, 1)
        outer.addWidget(body, 1)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 4)
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(grip)
        outer.addLayout(grip_row)

        # ── group 1: today's items ──
        grid.addWidget(self._group_label("today's items"))
        row = QHBoxLayout()
        row.setSpacing(10)
        # Calendar takes ~70% of the row: it carries the most information and
        # the weather readout needs far less room than it was getting.
        row.addWidget(self._build_weather_panel(), 75)
        row.addWidget(self._build_calendar_panel(), 175)
        grid.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._build_todo_panel(), 100)
        row.addWidget(self._build_habits_panel(), 100)
        grid.addLayout(row)
        grid.addWidget(self._build_journal_panel())

        # ── group 2: at a glance ──
        grid.addWidget(self._group_label("at a glance"))
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._build_greed_panel(), 100)
        row.addWidget(self._build_sector_panel(), 175)
        grid.addLayout(row)

        # ── group 3: freeform ──
        grid.addWidget(self._group_label("freeform"))
        grid.addWidget(self._build_notes_panel())
        grid.addStretch()

    def _build_title_bar(self):
        bar = TitleBar(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(10)

        mark = QFrame()
        mark.setFixedSize(7, 7)
        mark.setStyleSheet(f"background: {CREAM}; border: none;")
        layout.addWidget(mark)

        title = QLabel("dionysus")
        title.setStyleSheet(f"color: {CREAM}; background: transparent; border: none;")
        self.scaler.font(self.title_font, 18, spacing=0.5, register=title)
        layout.addWidget(title)
        layout.addStretch()

        self.clock_label = QLabel("--:--")
        self.clock_label.setStyleSheet(f"color: {WHITE}; background: transparent; border: none;")
        self.scaler.font(self.body_font, 15, tabular_nums=True, register=self.clock_label)
        layout.addWidget(self.clock_label)

        self.date_label = QLabel("")
        self.date_label.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
        self.scaler.font(self.body_font, theme.SMALL_PX, tabular_nums=True,
                         register=self.date_label)
        layout.addWidget(self.date_label)
        layout.addSpacing(6)

        self.refresh_btn = self._icon_button("↻", "refresh now (re-runs the collector)",
                                             self._manual_refresh)
        layout.addWidget(self.refresh_btn)
        self.gear_btn = self._icon_button("⚙", "settings", self._show_settings_menu)
        layout.addWidget(self.gear_btn)
        layout.addWidget(self._icon_button("—", "minimize", self.showMinimized))
        layout.addWidget(self._icon_button("✕", "close", self.close))
        return bar

    def _icon_button(self, text, tooltip, slot):
        button = QPushButton(text)
        button.setFixedSize(24, 24)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {CHROME};
                border: 1px solid {BORDER}; font-size: 11px; }}
            QPushButton:hover {{ background: {HOVER}; color: {WHITE};
                border-color: {CHROME}; }}
            QPushButton:disabled {{ color: {BORDER}; }}
        """)
        button.clicked.connect(slot)
        return button

    def _group_label(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.GROUP_PX, spacing=2, register=label)
        return label

    def _panel(self, title, caption=None):
        """Bordered panel with a lowercase Fraunces title. Returns
        (frame, content_layout, caption_label, header_layout)."""
        box = QFrame()
        box.setObjectName("panel")
        box.setStyleSheet(theme.panel_style())
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 11, 14, 12)
        layout.setSpacing(9)

        header = QHBoxLayout()
        header.setSpacing(8)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {CREAM}; background: transparent; border: none;")
        self.scaler.font(self.title_font, theme.TITLE_PX, spacing=0.4, register=title_label)
        header.addWidget(title_label)
        caption_label = None
        if caption is not None:
            caption_label = QLabel(caption)
            caption_label.setStyleSheet(
                f"color: {FAINT}; background: transparent; border: none;")
            self.scaler.font(self.body_font, theme.CAPTION_PX, tabular_nums=True,
                             register=caption_label)
            header.addWidget(caption_label)
        header.addStretch()
        layout.addLayout(header)
        return box, layout, caption_label, header

    def _small_button(self, text, slot, tooltip=None):
        button = QPushButton(text)
        button.setStyleSheet(theme.button_style(self.body_font))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button

    def _hint(self, text, color=None, size=None):
        # Resolved here rather than as a default argument: defaults are
        # evaluated while the class is being defined, which would undo the
        # guarded imports above.
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {color or FAINT}; background: transparent;")
        self.scaler.font(self.body_font, size or theme.BODY_PX, register=label)
        return label

    # ── panel: to do ─────────────────────────────────────────────────
    def _build_todo_panel(self):
        box, layout, caption, _ = self._panel("to do", "")
        self.todo_caption = caption

        entry_row = QHBoxLayout()
        entry_row.setSpacing(6)
        self.todo_input = QLineEdit()
        self.todo_input.setPlaceholderText("add a to-do…")
        self.todo_input.setStyleSheet(theme.input_style(self.body_font))
        self.scaler.font(self.body_font, theme.BODY_PX, register=self.todo_input)
        self.todo_input.returnPressed.connect(self._add_todo)
        entry_row.addWidget(self.todo_input, 1)

        self.priority_btn = self._small_button(
            "—", self._cycle_new_priority, "priority for the next item")
        self.priority_btn.setFixedWidth(38)
        entry_row.addWidget(self.priority_btn)
        entry_row.addWidget(self._small_button("add", self._add_todo))
        layout.addLayout(entry_row)

        self.todo_layout = QVBoxLayout()
        self.todo_layout.setSpacing(10)
        layout.addLayout(self.todo_layout)
        layout.addStretch()
        return box

    def _reload_todos(self):
        clear_layout(self.todo_layout)
        groups = self.todos.grouped()
        total = sum(len(items) for _, items in groups)
        done = sum(1 for _, items in groups for item in items if item.done)

        for priority, items in groups:
            header = QLabel(PRIORITY_MARKS[priority] or "no priority")
            header.setStyleSheet(
                f"color: {CREAM if priority else FAINT}; background: transparent;")
            self.scaler.font(self.body_font, theme.CAPTION_PX, spacing=1, register=header)
            self.todo_layout.addWidget(header)

            tier = TierList(priority)
            tier.reordered.connect(self._reorder_todos)
            tier.movedHere.connect(self._move_todo_tier)
            for item in items:
                row = TodoRow(item, self.scaler, self.body_font)
                row.toggled.connect(self._set_todo_done)
                row.deleted.connect(self._delete_todo)
                row.priorityChanged.connect(self._move_todo_tier)
                tier.add_row(row)
            self.todo_layout.addWidget(tier)

        if not total:
            self.todo_layout.addWidget(self._hint("nothing on the list."))
        if self.todo_caption is not None:
            self.todo_caption.setText(f"{done}/{total} done" if total else "idle")
        self.scaler.reapply()

    def _cycle_new_priority(self):
        self._new_todo_priority = (self._new_todo_priority + 1) % (MAX_PRIORITY + 1)
        self.priority_btn.setText(PRIORITY_MARKS[self._new_todo_priority] or "—")

    def _add_todo(self):
        text = self.todo_input.text().strip()
        if not text:
            return
        self.todos.add(text, self._new_todo_priority)
        self.todo_input.clear()
        self._reload_todos()

    def _set_todo_done(self, item_id, done):
        # Completion is only a strikethrough — the item stays on the list
        # until it's explicitly deleted.
        self.todos.set_done(item_id, done)
        self._reload_todos()

    def _delete_todo(self, item_id):
        self.todos.delete(item_id)
        self._reload_todos()

    def _move_todo_tier(self, item_id, priority, index=None):
        items = self.todos.set_priority(item_id, priority)
        if index is not None:
            # Dropped into a tier at a specific spot rather than picked from
            # the context menu, so honour where it landed.
            tier_ids = [i.id for i in items if i.priority == priority]
            if item_id in tier_ids:
                tier_ids.remove(item_id)
                tier_ids.insert(min(index, len(tier_ids)), item_id)
                self.todos.reorder(priority, tier_ids)
        self._reload_todos()

    def _reorder_todos(self, priority, ordered_ids):
        self.todos.reorder(priority, ordered_ids)
        self._reload_todos()

    # ── panel: habit tracker ─────────────────────────────────────────
    def _build_habits_panel(self):
        box, layout, caption, header = self._panel(
            "habit tracker", str(dt.date.today().year))
        self.habits_caption = caption
        header.addWidget(self._small_button("add habit", self._add_habit))
        header.addWidget(self._small_button("archived", self._show_archived_habits))

        self.habits_layout = QVBoxLayout()
        self.habits_layout.setSpacing(14)
        layout.addLayout(self.habits_layout)
        return box

    def _reload_habits(self):
        clear_layout(self.habits_layout)
        year = dt.date.today().year
        active = self.habits.active()
        if not active:
            self.habits_layout.addWidget(
                self._hint("no habits yet — add one to start a grid."))
        for habit in active:
            self.habits_layout.addWidget(self._habit_block(habit, year))
        if self.habits_caption is not None:
            self.habits_caption.setText(str(year))
        self.scaler.reapply()

    def _habit_block(self, habit, year):
        block = QWidget()
        block.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        header = QHBoxLayout()
        header.setSpacing(8)
        name = QLabel(habit.name)
        name.setStyleSheet(f"color: {CREAM}; background: transparent;")
        self.scaler.font(self.title_font, theme.BODY_PX, register=name)
        header.addWidget(name)
        if habit.goal:
            goal = ElidedLabel(habit.goal)
            goal.setStyleSheet(f"color: {FAINT}; background: transparent;")
            self.scaler.font(self.body_font, theme.SMALL_PX, register=goal)
            header.addWidget(goal, 1)
        header.addStretch()

        today = dt.date.today()
        check = TodayCheck(self.scaler, self.body_font, today)
        check.set_checked(habit.is_done(today))
        check.clicked.connect(
            lambda h=habit, d=today: self._toggle_habit_day(h.id, d))
        header.addWidget(check)

        count = QLabel(f"{habit.done_count(year)} days")
        count.setStyleSheet(f"color: {CHROME}; background: transparent;")
        self.scaler.font(self.body_font, theme.CAPTION_PX, tabular_nums=True, register=count)
        header.addWidget(count)

        archive = self._small_button(
            "archive", lambda _checked=False, h=habit: self._archive_habit(h.id),
            "hide this habit — its data is kept")
        header.addWidget(archive)
        layout.addLayout(header)

        grid = DotGrid(year)
        grid.set_label_font(self.scaler.font(self.body_font, 8))
        grid.set_values({date: True for date in year_dates(year) if habit.is_done(date)})
        grid.dayClicked.connect(lambda date, h=habit: self._toggle_habit_day(h.id, date))
        layout.addWidget(grid)
        return block

    def _toggle_habit_day(self, habit_id, date):
        # Straight to habits.json — no collector, no watcher round trip.
        self.habits.toggle(habit_id, date)
        self._reload_habits()

    def _add_habit(self):
        dialog = AddHabitDialog(self.scaler, self.body_font, self.title_font, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name, goal = dialog.values()
            if name:
                self.habits.add(name, goal)
                self._reload_habits()

    def _archive_habit(self, habit_id):
        self.habits.set_archived(habit_id, True)
        self._reload_habits()

    def _show_archived_habits(self):
        dialog = ArchivedHabitsDialog(self.habits, self.scaler, self.body_font,
                                      self.title_font, self)
        dialog.exec()
        if dialog.restored_any:
            self._reload_habits()

    # ── panel: journal ───────────────────────────────────────────────
    def _build_journal_panel(self):
        today = dt.date.today()
        box, layout, caption, header = self._panel(
            "journal", f"{DAYS_SHORT[today.weekday()]} {today.isoformat()}")
        self.journal_caption = caption
        self.journal_history_btn = self._small_button("history", self._show_journal_history)
        header.addWidget(self.journal_history_btn)

        self.journal_body = QPlainTextEdit()
        self.journal_body.setPlaceholderText("how did today go?")
        self.journal_body.setStyleSheet(theme.input_style(self.body_font))
        self.journal_body.setMinimumHeight(120)
        # Preferred, not the QPlainTextEdit default of Expanding — otherwise
        # the journal soaks up every spare pixel in the scroll area and shoves
        # Rough Notes off the bottom.
        self.journal_body.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Preferred)
        self.scaler.font(self.body_font, theme.BODY_PX, register=self.journal_body)
        layout.addWidget(self.journal_body)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        rating_label = QLabel("rating")
        rating_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.SMALL_PX, register=rating_label)
        controls.addWidget(rating_label)

        self.rating_input = QDoubleSpinBox()
        self.rating_input.setDecimals(1)
        self.rating_input.setSingleStep(0.1)
        # 0.0 is the "not set yet" slot: a rating is required, so saving with
        # the spinner still showing "—" is refused rather than defaulted.
        self.rating_input.setRange(0.0, 10.0)
        self.rating_input.setSpecialValueText("—")
        self.rating_input.setStyleSheet(theme.input_style(self.body_font))
        self.rating_input.setFixedWidth(72)
        self.scaler.font(self.body_font, theme.BODY_PX, tabular_nums=True,
                         register=self.rating_input)
        controls.addWidget(self.rating_input)

        scale_hint = QLabel("/ 10")
        scale_hint.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.CAPTION_PX, register=scale_hint)
        controls.addWidget(scale_hint)

        self.journal_status = ElidedLabel("")
        self.journal_status.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.CAPTION_PX, register=self.journal_status)
        controls.addWidget(self.journal_status, 1)

        self.journal_save_btn = self._small_button("save entry", self._save_journal)
        controls.addWidget(self.journal_save_btn)
        layout.addLayout(controls)
        return box

    def _reload_journal(self):
        today = dt.date.today()
        try:
            entry = self.journal.read_entry(today)
        except JournalUnavailable as e:
            # Missing orgparse: the panel says so and the rest of the
            # dashboard carries on rather than the app refusing to start.
            self._disable_journal(str(e))
            return
        except Exception as e:
            self.journal_status.setText(f"could not read journal.org: {soft_wrap(e)}")
            return
        if entry is None:
            self.journal_body.setPlainText("")
            self.rating_input.setValue(0.0)
            self.journal_status.setText("no entry yet today.")
            return
        self.journal_body.setPlainText(entry.body)
        self.rating_input.setValue(entry.rating or 0.0)
        self.journal_status.setText("today's entry loaded.")

    def _disable_journal(self, message):
        """Grey the panel out with a reason instead of leaving controls that
        can only fail."""
        self.journal_body.setEnabled(False)
        self.rating_input.setEnabled(False)
        self.journal_save_btn.setEnabled(False)
        self.journal_history_btn.setEnabled(False)
        self.journal_status.setText(message)

    def _save_journal(self):
        rating = self.rating_input.value()
        if rating < 1.0:
            self.journal_status.setText("a rating of 1.0–10.0 is required.")
            return
        try:
            self.journal.write_entry(dt.date.today(), rating,
                                     self.journal_body.toPlainText())
        except JournalUnavailable as e:
            self._disable_journal(str(e))
            return
        except Exception as e:
            self.journal_status.setText(f"save failed: {soft_wrap(e)}")
            return
        self.journal_status.setText(f"saved {dt.datetime.now().strftime('%H:%M')}")

    def _show_journal_history(self):
        # An exception escaping a slot aborts the whole Qt app, so the dialog
        # gets built inside the guard, not just the store call.
        try:
            dialog = JournalHistoryDialog(self.journal, self.scaler,
                                          self.body_font, self.title_font, self)
        except JournalUnavailable as e:
            self._disable_journal(str(e))
            return
        except Exception as e:
            self.journal_status.setText(f"could not open history: {soft_wrap(e)}")
            return
        dialog.exec()

    # ── panel: weather ───────────────────────────────────────────────
    def _build_weather_panel(self):
        box, layout, _, _ = self._panel("weather", "vancouver")
        row = QHBoxLayout()
        row.setSpacing(12)

        self.temp_label = QLabel("--°")
        self.temp_label.setStyleSheet(f"color: {CREAM}; background: transparent;")
        self.scaler.font(self.body_font, theme.BIG_PX, tabular_nums=True,
                         register=self.temp_label)
        row.addWidget(self.temp_label)

        column = QVBoxLayout()
        column.setSpacing(2)
        self.cond_label = ElidedLabel("—")
        self.cond_label.setStyleSheet(f"color: {CHROME}; background: transparent;")
        self.scaler.font(self.body_font, theme.BODY_PX, register=self.cond_label)
        column.addWidget(self.cond_label)
        self.hilo_label = ElidedLabel("")
        self.hilo_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.SMALL_PX, tabular_nums=True,
                         register=self.hilo_label)
        column.addWidget(self.hilo_label)
        row.addLayout(column, 1)
        layout.addLayout(row)

        # The calendar next door is taller, so the slack lands here: keep the
        # readout under the title and let the sparkline sit on the baseline.
        layout.addStretch()
        self.sparkline = Sparkline()
        layout.addWidget(self.sparkline)
        return box

    def _render_weather(self, weather):
        weather = weather or {}
        if weather.get("error"):
            self.temp_label.setText("--°")
            self.cond_label.setText(f"unavailable: {soft_wrap(weather['error'])}")
            self.hilo_label.setText("")
            self.sparkline.set_values([])
            return
        temp = weather.get("temp_c")
        self.temp_label.setText(f"{round(temp)}°" if isinstance(temp, (int, float)) else "--°")
        self.cond_label.setText(WMO_CODES.get(weather.get("weather_code"), "—").lower())
        high, low, wind = weather.get("high_c"), weather.get("low_c"), weather.get("wind_kph")
        high_s = f"{round(high)}°" if isinstance(high, (int, float)) else "—"
        low_s = f"{round(low)}°" if isinstance(low, (int, float)) else "—"
        wind_s = f" · {round(wind)} km/h" if isinstance(wind, (int, float)) else ""
        self.hilo_label.setText(f"h {high_s} · l {low_s}{wind_s}")
        self.sparkline.set_values(weather.get("hourly_temps") or [])

    # ── panel: calendar ──────────────────────────────────────────────
    def _build_calendar_panel(self):
        box, layout, caption, header = self._panel("calendar", "")
        self.calendar_caption = caption
        # Two-way sync status, written by calendar_sync.py on every run.
        self.sync_label = QLabel("")
        self.sync_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.CAPTION_PX, register=self.sync_label)
        header.addWidget(self.sync_label)

        self.calendar_hint = self._hint("", size=theme.SMALL_PX)
        self.calendar_hint.hide()
        layout.addWidget(self.calendar_hint)
        self.week_grid = WeekGrid(self.scaler, self.body_font, self.title_font)
        layout.addWidget(self.week_grid, 1)
        return box

    def _load_sync_status(self):
        """'synced 4m ago' from calendar_sync_status.json. Absent file means
        the sync isn't set up, and the line simply stays empty."""
        if not SYNC_STATUS_PATH.exists():
            self.sync_label.setText("")
            return
        status = load_json(SYNC_STATUS_PATH, {})
        if not isinstance(status, dict) or not status:
            self.sync_label.setText("")
            return

        succeeded = _ago(status.get("last_success_utc"))
        error = status.get("last_error")
        if error:
            attempted = _ago(status.get("last_run_utc")) or "just now"
            text = f"sync failed {attempted}"
            if succeeded:
                text += f" · last ok {succeeded}"
            self.sync_label.setStyleSheet(f"color: {CHROME}; background: transparent;")
            self.sync_label.setToolTip(str(error))
        elif succeeded:
            text = f"synced {succeeded}"
            if status.get("dry_run"):
                text += " (dry run)"
            self.sync_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
            self.sync_label.setToolTip(
                f"{status.get('events_synced_count', 0)} event(s) written last run")
        else:
            text = "sync pending"
            self.sync_label.setToolTip("")
        self.sync_label.setText(text)

    def _render_calendar(self, data):
        data = data or {}
        self.week_grid.render(data)
        start, end = data.get("week_start"), data.get("week_end")
        if start and end:
            try:
                first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
                self.calendar_caption.setText(
                    f"{MONTHS_SHORT[first.month - 1]} {first.day} – "
                    f"{MONTHS_SHORT[last.month - 1]} {last.day}")
            except ValueError:
                self.calendar_caption.setText("")

        if data.get("needs_setup"):
            steps = data.get("setup_steps") or []
            self.calendar_hint.setText(
                "google calendar isn't connected yet — one-time setup:  "
                + "  ·  ".join(f"{i}. {step}" for i, step in enumerate(steps, 1)))
            self.calendar_hint.show()
        elif data.get("error"):
            self.calendar_hint.setText(f"unavailable: {soft_wrap(data['error'])}")
            self.calendar_hint.show()
        else:
            self.calendar_hint.hide()

    # ── panel: greed index ───────────────────────────────────────────
    def _build_greed_panel(self):
        box, layout, _, _ = self._panel("greed index", "cnn fear & greed")
        row = QHBoxLayout()
        row.setSpacing(12)
        self.greed_score = QLabel("--")
        self.greed_score.setStyleSheet(f"color: {CREAM}; background: transparent;")
        self.scaler.font(self.body_font, theme.BIG_PX, tabular_nums=True,
                         register=self.greed_score)
        row.addWidget(self.greed_score)

        column = QVBoxLayout()
        column.setSpacing(2)
        self.greed_rating = ElidedLabel("—")
        self.greed_rating.setStyleSheet(f"color: {CHROME}; background: transparent;")
        self.scaler.font(self.body_font, theme.BODY_PX, register=self.greed_rating)
        column.addWidget(self.greed_rating)
        self.greed_delta = ElidedLabel("")
        self.greed_delta.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self.scaler.font(self.body_font, theme.SMALL_PX, tabular_nums=True,
                         register=self.greed_delta)
        column.addWidget(self.greed_delta)
        row.addLayout(column, 1)
        layout.addLayout(row)

        self.greed_meter = GreedMeter(self.scaler, self.body_font)
        layout.addWidget(self.greed_meter)
        layout.addStretch()
        return box

    def _render_greed(self, greed):
        greed = greed or {}
        if greed.get("error") or greed.get("score") is None:
            self.greed_score.setText("--")
            self.greed_meter.set_score(None)
            self.greed_rating.setText("unavailable")
            self.greed_delta.setText(soft_wrap(greed.get("error", "no data"))[:80])
            return
        score = greed["score"]
        self.greed_score.setText(str(score))
        self.greed_meter.set_score(score)
        self.greed_rating.setText((greed.get("rating") or "—").lower())
        previous = greed.get("previous_close")
        self.greed_delta.setText(
            f"{int(round(score - previous)):+d} vs. yesterday"
            if previous is not None else "")

    # ── panel: sector analysis ───────────────────────────────────────
    def _build_sector_panel(self):
        box, layout, _, _ = self._panel("sector analysis", "% 1d")
        self.sector_layout = QVBoxLayout()
        self.sector_layout.setSpacing(6)
        layout.addLayout(self.sector_layout)
        layout.addStretch()
        return box

    def _render_sectors(self, sectors):
        clear_layout(self.sector_layout)
        sectors = sectors or {}
        for key, section in (("sp500", "s&p 500"), ("tsx", "tsx")):
            rows = sectors.get(key) or []
            if not rows:
                continue
            # Sort here too (not just relying on collector.py) so display is
            # always correct even against stale/older cache data.
            rows = sorted(rows, key=lambda r: (r.get("change_pct") is None,
                                               -(r.get("change_pct") or 0)))
            header = QLabel(section)
            header.setStyleSheet(f"color: {FAINT}; background: transparent;")
            self.scaler.font(self.body_font, theme.CAPTION_PX, spacing=1, register=header)
            self.sector_layout.addWidget(header)

            max_abs = max((abs(r["change_pct"]) for r in rows
                           if r.get("change_pct") is not None), default=1) or 1
            for row in rows:
                self.sector_layout.addWidget(self._sector_row(row, max_abs))
        if not sectors:
            self.sector_layout.addWidget(self._hint("no market data yet."))
        self.scaler.reapply()

    def _sector_row(self, record, max_abs):
        pct = record.get("change_pct")
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        name = ElidedLabel(str(record.get("label", record.get("ticker", ""))).lower())
        name.setFixedWidth(round(130 * self.scaler.factor))
        name.setToolTip(f"{record.get('ticker', '')} — {record.get('label', '')}")
        name.setStyleSheet(f"color: {CHROME}; background: transparent;")
        self.scaler.font(self.body_font, theme.SMALL_PX, register=name)
        layout.addWidget(name)

        bar = DivergingBar()
        bar.set_value(pct, max_abs)
        layout.addWidget(bar, 1)

        value = QLabel("n/a" if pct is None else f"{pct:+.1f}")
        value.setFixedWidth(round(48 * self.scaler.factor))
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value.setStyleSheet(f"color: {bar.color()}; background: transparent;")
        self.scaler.font(self.body_font, theme.SMALL_PX, tabular_nums=True, register=value)
        layout.addWidget(value)
        return row

    # ── panel: rough notes ───────────────────────────────────────────
    def _build_notes_panel(self):
        box, layout, caption, _ = self._panel("rough notes", "autosaved")
        self.notes_caption = caption
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("scratch space…")
        self.notes_edit.setStyleSheet(theme.input_style(self.body_font))
        self.notes_edit.setMinimumHeight(110)
        self.scaler.font(self.body_font, theme.BODY_PX, register=self.notes_edit)
        self.notes_edit.textChanged.connect(self._queue_notes_save)
        layout.addWidget(self.notes_edit)
        return box

    def _load_notes(self):
        try:
            text = Path(ROUGH_NOTES_PATH).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        self.notes_edit.blockSignals(True)
        self.notes_edit.setPlainText(text)
        self.notes_edit.blockSignals(False)

    def _queue_notes_save(self):
        # Debounce, not a poll: the timer is armed by typing and fires once.
        self._notes_timer.start(NOTES_SAVE_DELAY_MS)

    def _save_notes(self):
        try:
            atomic_write_text(ROUGH_NOTES_PATH, self.notes_edit.toPlainText())
            self.notes_caption.setText(f"saved {dt.datetime.now().strftime('%H:%M')}")
        except OSError as e:
            self.notes_caption.setText(f"save failed: {e}")

    # ── clock ────────────────────────────────────────────────────────
    def _tick_clock(self):
        now = dt.datetime.now()
        self.clock_label.setText(now.strftime("%H:%M"))
        self.date_label.setText(
            f"{DAYS_SHORT[now.weekday()]} {now.day:02d} {MONTHS_SHORT[now.month - 1]}")

    # ── window events ────────────────────────────────────────────────
    def resizeEvent(self, event):
        self.scaler.set_width(event.size().width())
        super().resizeEvent(event)

    def paintEvent(self, _event):
        QPainter(self).fillRect(self.rect(), QColor(BG))

    def closeEvent(self, event):
        if self._notes_timer.isActive():
            self._notes_timer.stop()
            self._save_notes()
        super().closeEvent(event)

    # ── file watching (no polling) ───────────────────────────────────
    def _setup_watcher(self):
        # Two watched files, same zero-polling pattern: cache.json from the
        # collector, and the status file calendar_sync.py writes each run.
        self.watcher = QFileSystemWatcher()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.watcher.addPath(str(CACHE_PATH.parent))
        for path in (CACHE_PATH, SYNC_STATUS_PATH):
            if path.exists():
                self.watcher.addPath(str(path))
        self.watcher.fileChanged.connect(self._on_watched_change)
        self.watcher.directoryChanged.connect(self._on_watched_change)

    def _on_watched_change(self, _path):
        # An atomic replace drops the old inode, so re-arm the watch on any
        # path that came back.
        watched = set(self.watcher.files())
        for path in (CACHE_PATH, SYNC_STATUS_PATH):
            if path.exists() and str(path) not in watched:
                self.watcher.addPath(str(path))
        self._load_cache()
        self._load_sync_status()

    # ── cache loading (read-only panels) ─────────────────────────────
    def _load_cache(self):
        if not CACHE_PATH.exists():
            self.status_label.setText("no cache file yet — click ↻ to run the collector.")
            return
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        self.status_label.setText(f"last sync {_sync_stamp(data.get('generated_at'))}")
        self._render_weather(data.get("weather"))
        self._render_greed(data.get("greed"))
        self._render_sectors(data.get("sectors"))
        self._render_calendar(data.get("calendar"))
        self.scaler.reapply()

    # ── refresh ──────────────────────────────────────────────────────
    def _manual_refresh(self):
        # Widget-owned files have no watcher (nothing else writes them), so
        # ↻ is also the "re-read what Emacs may have changed" button.
        self._reload_todos()
        self._reload_habits()
        self._reload_journal()
        if not self.runner.run_async():
            return
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("…")
        self.status_label.setText("refreshing…")

    def _auto_refresh_tick(self):
        if self._auto_refresh and not self.runner.running:
            self.runner.run_async()

    def _on_refresh_done(self, success, message):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻")
        if not success:
            self.status_label.setText(message)

    # ── settings menu ────────────────────────────────────────────────
    def _show_settings_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(theme.menu_style())

        comfortable = menu.addAction("layout: comfortable")
        comfortable.setCheckable(True)
        comfortable.setChecked(not self.scaler.compact)
        comfortable.triggered.connect(lambda: self.scaler.set_compact(False))

        compact = menu.addAction("layout: compact")
        compact.setCheckable(True)
        compact.setChecked(self.scaler.compact)
        compact.triggered.connect(lambda: self.scaler.set_compact(True))
        menu.addSeparator()

        on_top = menu.addAction("keep window on top")
        on_top.setCheckable(True)
        on_top.setChecked(self._always_on_top)
        on_top.toggled.connect(self._toggle_always_on_top)

        auto = menu.addAction(f"auto-refresh every {AUTO_REFRESH_MINUTES} min")
        auto.setCheckable(True)
        auto.setChecked(self._auto_refresh)
        auto.toggled.connect(self._toggle_auto_refresh)

        menu.addSeparator()
        menu.addAction("quit", self.close)
        menu.exec(self.gear_btn.mapToGlobal(QPoint(0, self.gear_btn.height())))

    def _toggle_auto_refresh(self, checked):
        self._auto_refresh = checked

    def _toggle_always_on_top(self, checked):
        self._always_on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()  # re-applying flags requires re-showing the window


def report_startup_failure(error, details) -> int:
    """Say why the window never appeared, on screen and on disk.

    Launched with pythonw there is no console, so without this the app just
    doesn't open and there is nothing to go on.
    """
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    try:
        CRASH_LOG.write_text(f"{stamp}\n{details}", encoding="utf-8")
        written = f"\n\nFull traceback: {CRASH_LOG}"
    except OSError:
        written = ""
    print(details, file=sys.stderr)
    QMessageBox.critical(
        None, "Dashboard failed to start",
        f"{type(error).__name__}: {error}\n\n"
        "This usually means the files next to dashboard_widget.py are from "
        "different versions — copy the whole set across, not just the one "
        "that changed." + written)
    return 1


def main() -> int:
    app = QApplication(sys.argv)
    if STARTUP_ERROR is not None:
        return report_startup_failure(*STARTUP_ERROR)

    theme.apply_app_style(app)
    theme.load_local_fonts()
    title = theme.pick_font(theme.TITLE_FAMILIES)
    body = theme.pick_font(theme.BODY_FAMILIES)
    print(f"Title font: {title} | Body font: {body}")
    app.setFont(QFont(body, 9))
    try:
        window = DashboardWidget()
    except Exception as e:
        return report_startup_failure(e, traceback.format_exc())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

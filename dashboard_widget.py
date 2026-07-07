#!/usr/bin/env python3
"""
Dashboard Widget — joputer edition (standalone, frameless, retro CRT theme)

Frameless window with a custom title bar (drag-to-move via the header,
same pattern as the old Electron `frame: false` + drag-region setup) and
a QSizeGrip in the corner for resizing. No OS chrome, but still shows up
in the taskbar like a normal app.

Visuals match the Claude Design handoff (Retro_Dashboard.html): deep-navy
CRT look, glowing cyan VT323 headers, IBM Plex Mono body text, scanline
overlay, seven-segment greed readout, diverging sector bars.

Data flow unchanged: collector.py writes cache.json, this widget watches
it via QFileSystemWatcher (no polling) and repaints on change. Manual
refresh via the ↻ button spawns collector.py in the background.
(The only timer in this file drives the CLOCK panel's wall-clock display —
it never touches cache.json or any data source.)

Fonts: drop VT323-Regular.ttf and IBMPlexMono-*.ttf into a `fonts/` folder
next to this script (Google Fonts, OFL) and they're loaded automatically;
otherwise the widget falls back to installed system fonts.

Setup:
    pip install PyQt6

Launch (startup shortcut target):
    pythonw.exe dashboard_widget.py
"""

import datetime as dt
import html
import json
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, QEvent, QFileSystemWatcher, QPoint, QPointF, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QMenu, QSizePolicy, QSizeGrip, QGraphicsDropShadowEffect,
)

CACHE_PATH = Path(r"C:\Users\joey\dashboard-project-files\cache.json")
COLLECTOR_SCRIPT = Path(r"C:\Users\joey\dashboard-project-files\collector.py")
PYTHON_EXE = "py"
FONT_DIR = Path(__file__).resolve().parent / "fonts"

BASE_W = 960  # design reference width for proportional scaling
VERSION = "v2.6"

# ── Retro CRT palette (from Retro_Dashboard.html) ─────────────────────────
BG        = "#060c16"   # page background
BG_TITLE  = "#04070d"
PANEL_BG  = "rgba(9, 17, 30, 0.55)"
BORDER    = "#143253"
CYAN      = "#5cc8ff"
CYAN_DIM  = "#2f6d99"
TEXT      = "#cfe6f5"
MUTED     = "#9fc0d6"
FAINT     = "#6f93ad"
AMBER     = "#ffb84d"
RED       = "#ff6b6b"

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

DISPLAY_FALLBACKS = [  # big glowing headers / numerals — VT323 if available
    "VT323",
    "JetBrainsMono NFM", "JetBrainsMono NF", "JetBrains Mono",
    "Cascadia Code", "Cascadia Mono", "Consolas",
]
BODY_FALLBACKS = [  # body text — IBM Plex Mono if available
    "IBM Plex Mono",
    "JetBrainsMono NFM", "JetBrainsMono NF", "JetBrainsMono NFP",
    "JetBrainsMonoNL NFM", "JetBrainsMonoNL NF",
    "JetBrains Mono NL", "JetBrains Mono", "JetBrainsMono",
    "Cascadia Code", "Cascadia Mono", "Consolas",
]

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def load_local_fonts():
    if FONT_DIR.is_dir():
        for p in list(FONT_DIR.glob("*.ttf")) + list(FONT_DIR.glob("*.otf")):
            QFontDatabase.addApplicationFont(str(p))


def pick_font(candidates):
    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return "Consolas"


def zone_color(score):
    """CNN Fear & Greed zone → panel accent color."""
    if score is None:
        return FAINT
    if score >= 75:
        return RED       # extreme greed
    if score >= 55:
        return AMBER     # greed
    if score > 45:
        return MUTED     # neutral
    return CYAN          # fear / extreme fear


def hhmm(iso_str):
    """'2026-07-07T09:42' → '09:42' (best effort)."""
    if not iso_str or "T" not in iso_str:
        return None
    t = iso_str.split("T", 1)[1]
    return t[:5] if len(t) >= 5 else None


def soft_wrap(text):
    """Exception strings contain long unbreakable runs like
    HTTPSConnectionPool(host=... — zero-width spaces after punctuation let
    word-wrap break them on narrow windows."""
    return re.sub(r"([./,()'=:])", "\\1" + "\u200b", str(text))


def add_glow(widget, color, radius=14):
    eff = QGraphicsDropShadowEffect(widget)
    eff.setOffset(0, 0)
    eff.setBlurRadius(radius)
    eff.setColor(QColor(color))
    widget.setGraphicsEffect(eff)


# ─────────────────────────────────────────────────────────────────────────
# Background collector runner
# ─────────────────────────────────────────────────────────────────────────

class CollectorRunner(QObject):
    finished = pyqtSignal(bool, str)

    def run_async(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = subprocess.run(
                [PYTHON_EXE, str(COLLECTOR_SCRIPT)],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                self.finished.emit(True, "Refreshed.")
            else:
                self.finished.emit(False, f"Collector error: {result.stderr[-200:]}")
        except Exception as e:
            self.finished.emit(False, f"Failed to run collector: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Elided label — QLabel refuses to shrink below its full text width by
# default. This subclass reports the full text width as its sizeHint (so
# it takes its natural width when there's room) but a near-zero
# minimumSizeHint (so the layout may compress it), re-truncating with "…"
# on every resize. Both hints are computed from _full_text, never from
# the currently displayed elided text — otherwise the hint shrinks along
# with the label and the column can never grow back when the window widens.
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
# Custom-painted retro widgets
# ─────────────────────────────────────────────────────────────────────────

class ScanlineOverlay(QWidget):
    """Transparent full-window overlay painting CRT scanlines. Sits above
    every other widget; ignores the mouse entirely."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        p = QPainter(self)
        line = QColor(0, 0, 0, 40)
        for y in range(0, self.height(), 3):
            p.fillRect(0, y, self.width(), 1, line)


class Sparkline(QWidget):
    """Glowing cyan polyline over a faint baseline (Weather panel)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = []
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values):
        self._values = [float(v) for v in (values or []) if isinstance(v, (int, float))]
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawLine(0, h - 1, w, h - 1)
        if len(self._values) < 2:
            return
        lo, hi = min(self._values), max(self._values)
        span = (hi - lo) or 1.0
        top, bottom = 3, h - 6
        pts = [
            QPointF(i * (w - 1) / (len(self._values) - 1),
                    bottom - (v - lo) / span * (bottom - top))
            for i, v in enumerate(self._values)
        ]
        poly = QPolygonF(pts)
        glow_pen = QPen(QColor(92, 200, 255, 70), 4)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(glow_pen)
        p.drawPolyline(poly)
        pen = QPen(QColor(CYAN), 1.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(poly)


class SevenSegment(QWidget):
    """Squared seven-segment numeric readout (Greed Index panel)."""

    SEGMENTS = {
        "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
        "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abfgcd",
        "-": "g",
    }
    # Segment boxes in a 49x88 design cell (x, y, w, h, is_horizontal)
    GEOM = {
        "a": (8, 2, 32, 9, True),
        "b": (39, 10, 9, 30, False),
        "c": (39, 48, 9, 30, False),
        "d": (8, 77, 32, 9, True),
        "e": (1, 48, 9, 30, False),
        "f": (1, 10, 9, 30, False),
        "g": (8, 39, 32, 9, True),
    }
    CELL_W, CELL_H, GAP = 49, 88, 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = "--"
        self._color = QColor(AMBER)
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_value(self, value, color):
        self._text = "--" if value is None else str(int(value))
        self._color = QColor(color)
        self.update()

    @staticmethod
    def _seg_polygon(x, y, w, h, horizontal):
        if horizontal:
            return QPolygonF([
                QPointF(x + 3, y), QPointF(x + w - 3, y), QPointF(x + w, y + h / 2),
                QPointF(x + w - 3, y + h), QPointF(x + 3, y + h), QPointF(x, y + h / 2),
            ])
        return QPolygonF([
            QPointF(x, y + 3), QPointF(x + w / 2, y), QPointF(x + w, y + 3),
            QPointF(x + w, y + h - 3), QPointF(x + w / 2, y + h), QPointF(x, y + h - 3),
        ])

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self._text)
        if not n:
            return
        s = min(self.height() / self.CELL_H,
                self.width() / (n * self.CELL_W + (n - 1) * self.GAP))
        total_w = (n * self.CELL_W + (n - 1) * self.GAP) * s
        ox = (self.width() - total_w) / 2
        oy = (self.height() - self.CELL_H * s) / 2

        halo = QColor(self._color)
        halo.setAlpha(80)
        for i, ch in enumerate(self._text):
            cx = ox + i * (self.CELL_W + self.GAP) * s
            for seg in self.SEGMENTS.get(ch, ""):
                x, y, w, h, horiz = self.GEOM[seg]
                poly = self._seg_polygon(cx + x * s, oy + y * s, w * s, h * s, horiz)
                p.setPen(QPen(halo, 3 * s))
                p.setBrush(self._color)
                p.drawPolygon(poly)


class GreedGauge(QWidget):
    """Vertical 0–100 fear/greed scale: number ticks, gradient bar with a
    marker arrow at the current score, and zone labels."""

    def __init__(self, body_family, display_family, parent=None):
        super().__init__(parent)
        self._score = None
        self._body = body_family
        self._display = display_family
        self.setFixedWidth(128)
        self.setMinimumHeight(110)

    def set_score(self, score):
        self._score = score
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        pad = 6
        top, bottom = pad, h - pad
        span = bottom - top

        tick_font = QFont(self._body)
        tick_font.setPixelSize(9)
        p.setFont(tick_font)
        p.setPen(QColor(FAINT))
        for val in (100, 75, 50, 25, 0):
            y = bottom - span * val / 100
            p.drawText(0, int(y - 6), 20, 12,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(val))

        # gradient bar (bottom → top: deep blue, cyan, amber, red)
        bar_x, bar_w = 26, 7
        grad = QLinearGradient(0, bottom, 0, top)
        grad.setColorAt(0.0, QColor(CYAN_DIM))
        grad.setColorAt(0.28, QColor(CYAN))
        grad.setColorAt(0.62, QColor(AMBER))
        grad.setColorAt(0.88, QColor(RED))
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(grad)
        p.drawRect(bar_x, top, bar_w, span)

        # marker arrow + score value
        if self._score is not None:
            score = max(0, min(100, self._score))
            y = bottom - span * score / 100
            color = QColor(zone_color(self._score))
            arrow = QPolygonF([QPointF(bar_x + bar_w + 4, y - 5),
                               QPointF(bar_x + bar_w + 4, y + 5),
                               QPointF(bar_x + bar_w + 11, y)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPolygon(arrow)
            val_font = QFont(self._display)
            val_font.setPixelSize(14)
            p.setFont(val_font)
            p.setPen(color)
            p.drawText(bar_x + bar_w + 14, int(y - 8), 30, 16,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       str(int(self._score)))

        # zone labels down the right edge
        zone_font = QFont(self._display)
        zone_font.setPixelSize(12)
        zone_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1)
        p.setFont(zone_font)
        for label, color, align_y in (("EXTREME", RED, top + 6),
                                      ("GREED", AMBER, (top + bottom) / 2),
                                      ("FEAR", CYAN, bottom - 6)):
            p.setPen(QColor(color))
            p.drawText(64, int(align_y - 8), self.width() - 64, 16,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)


class DivergingBar(QWidget):
    """Sector bar diverging from a center axis: gains grow right (cyan),
    losses grow left (amber/red)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct = None
        self._max_abs = 1.0
        self.setFixedHeight(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, pct, max_abs):
        self._pct = pct
        self._max_abs = max_abs or 1.0
        self.update()

    def color(self):
        if self._pct is None or abs(self._pct) <= 0.1:
            return FAINT
        if self._pct > 0:
            return CYAN
        return RED if self._pct <= -1.25 else AMBER

    def paintEvent(self, _event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        cx = w // 2
        p.fillRect(cx - 1, 0, 2, h, QColor(CYAN_DIM))
        if self._pct is None or abs(self._pct) <= 0.005:
            return
        half = max(cx - 4, 1)
        length = round(min(abs(self._pct), self._max_abs) / self._max_abs * half)
        color = QColor(self.color())
        halo = QColor(color)
        halo.setAlpha(60)
        y, bh = 1, h - 2
        if self._pct > 0:
            p.fillRect(cx + 2, y - 1, length, bh + 2, halo)
            p.fillRect(cx + 2, y, length, bh, color)
        else:
            p.fillRect(cx - 2 - length, y - 1, length, bh + 2, halo)
            p.fillRect(cx - 2 - length, y, length, bh, color)


class GlowDot(QWidget):
    """Small glowing cyan orb (title bar dot / weather icon)."""

    def __init__(self, diameter, parent=None):
        super().__init__(parent)
        self._d = diameter
        self.setFixedSize(diameter + 8, diameter + 8)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._d / 2
        halo = QRadialGradient(cx, cy, r + 4)
        halo.setColorAt(0.0, QColor(92, 200, 255, 160))
        halo.setColorAt(1.0, QColor(92, 200, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(cx, cy), r + 4, r + 4)
        core = QRadialGradient(cx - r * 0.2, cy - r * 0.24, r * 1.4)
        core.setColorAt(0.0, QColor("#bfe6ff"))
        core.setColorAt(0.7, QColor(CYAN))
        core.setColorAt(1.0, QColor(CYAN))
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), r, r)


class TodoRow(QWidget):
    """Clickable to-do row: retro checkbox + label, strikethrough when done.
    Done-state lives in the widget only (cache.json stays collector-owned)."""

    toggled = pyqtSignal()

    def __init__(self, text, done, parent=None):
        super().__init__(parent)
        self._done = done
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self._box = QFrame()
        self._box.setFixedSize(14, 14)
        self._box.setStyleSheet(f"border: 1px solid {CYAN}; background: transparent;")
        box_lay = QHBoxLayout(self._box)
        box_lay.setContentsMargins(3, 3, 3, 3)
        self._fill = QFrame()
        self._fill.setStyleSheet(f"background: {CYAN}; border: none;")
        box_lay.addWidget(self._fill)
        add_glow(self._box, CYAN, 8)
        lay.addWidget(self._box, 0, Qt.AlignmentFlag.AlignTop)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        # Window resizes replace the label's font (proportional scaling),
        # which would clear the strikeout — re-assert it on every font change.
        self.label.installEventFilter(self)
        lay.addWidget(self.label, 1)
        self._apply()

    def eventFilter(self, obj, event):
        if obj is self.label and event.type() == QEvent.Type.FontChange:
            f = self.label.font()
            if f.strikeOut() != self._done:
                f.setStrikeOut(self._done)
                self.label.setFont(f)
        return super().eventFilter(obj, event)

    def is_done(self):
        return self._done

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._done = not self._done
            self._apply()
            self.toggled.emit()

    def _apply(self):
        self._fill.setVisible(self._done)
        f = self.label.font()
        f.setStrikeOut(self._done)
        self.label.setFont(f)
        color = "rgba(207, 230, 245, 0.45)" if self._done else TEXT
        self.label.setStyleSheet(f"color: {color}; background: transparent;")


class NewsRow(QWidget):
    """Clickable news row — opens the story link in the browser."""

    def __init__(self, link, parent=None):
        super().__init__(parent)
        self._link = link
        if link:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(link)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._link:
            webbrowser.open(self._link)


# ─────────────────────────────────────────────────────────────────────────
# Custom title bar — drag handle since there's no OS chrome
# ─────────────────────────────────────────────────────────────────────────

class TitleBar(QWidget):
    def __init__(self, parent_window):
        super().__init__()
        self._win = parent_window
        self._drag_pos = None
        self.setFixedHeight(38)
        # Plain-QWidget subclasses don't paint stylesheet backgrounds unless
        # WA_StyledBackground is set.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {BG_TITLE}; border-bottom: 1px solid {BORDER};")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self._win.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ─────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────

class DashboardWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Dashboard")
        self.setMinimumSize(560, 480)
        self.resize(990, 840)

        self._density = "comfortable"  # or "compact"
        self._always_on_top = False
        self._display = pick_font(DISPLAY_FALLBACKS)  # VT323-style
        self._body = pick_font(BODY_FALLBACKS)        # IBM Plex Mono-style
        self._todo_done = {}   # (text, source_file) → bool, survives reloads
        self._font_registry = []  # (widget, family, base_px, letter_spacing)

        self.runner = CollectorRunner()
        self.runner.finished.connect(self._on_refresh_done)

        self._build_ui()
        self._setup_watcher()
        self._load_cache()
        self._apply_scale(self.width())

        # CLOCK panel only — wall-clock repaint, not data polling.
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

    # ── Font helpers ─────────────────────────────────────────────────
    def _scale_factor(self, width=None):
        k = (width or self.width()) / BASE_W
        return max(0.55, min(k, 1.6))

    def _font(self, family, px, spacing=0.0, register=None):
        """Build a pixel-sized font scaled to the current window width.
        Pass register=<widget> to keep it rescaling on window resize."""
        k = self._scale_factor()
        if self._density == "compact":
            px = max(6, px - 1)
        f = QFont(family)
        f.setPixelSize(max(6, round(px * k)))
        if spacing:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing * k)
        if register is not None:
            self._font_registry.append((register, family, px, spacing))
            register.setFont(f)
        return f

    def _apply_scale(self, width):
        k = max(0.55, min(width / BASE_W, 1.6))
        alive = []
        for widget, family, px, spacing in self._font_registry:
            base = max(6, px - 1) if self._density == "compact" else px
            f = QFont(family)
            f.setPixelSize(max(6, round(base * k)))
            if spacing:
                f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing * k)
            try:
                widget.setFont(f)
                alive.append((widget, family, px, spacing))
            except RuntimeError:
                pass  # row widget was rebuilt and deleted
        self._font_registry = alive

    # ── UI construction ──────────────────────────────────────────────
    def _build_ui(self):
        # Background is painted in paintEvent (solid navy + radial glow) —
        # no stylesheet background here, it would paint over the gradient.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Title bar (matches the design's top bar) ──
        title_bar = TitleBar(self)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(16, 0, 8, 0)
        tb.setSpacing(9)

        tb.addWidget(GlowDot(8))

        self.title_label = QLabel("DASHBOARD")
        self.title_label.setStyleSheet(f"color: {CYAN}; background: transparent; border: none;")
        self._font(self._display, 17, spacing=3, register=self.title_label)
        add_glow(self.title_label, CYAN, 16)
        tb.addWidget(self.title_label)
        tb.addStretch()

        self.sys_label = QLabel(f"SYS ONLINE · {VERSION}")
        self.sys_label.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
        self._font(self._body, 10, spacing=2, register=self.sys_label)
        tb.addWidget(self.sys_label)
        tb.addSpacing(6)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedSize(26, 26)
        self.refresh_btn.setToolTip("Refresh now (re-runs the collector)")
        self.refresh_btn.setStyleSheet(self._icon_btn_style())
        self.refresh_btn.clicked.connect(self._manual_refresh)
        tb.addWidget(self.refresh_btn)

        self.gear_btn = QPushButton("⚙")
        self.gear_btn.setFixedSize(26, 26)
        self.gear_btn.setStyleSheet(self._icon_btn_style())
        self.gear_btn.clicked.connect(self._show_settings_menu)
        tb.addWidget(self.gear_btn)

        min_btn = QPushButton("—")
        min_btn.setFixedSize(26, 26)
        min_btn.setStyleSheet(self._icon_btn_style())
        min_btn.clicked.connect(self.showMinimized)
        tb.addWidget(min_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setStyleSheet(self._icon_btn_style(hover_bg=RED))
        close_btn.clicked.connect(self.close)
        tb.addWidget(close_btn)

        outer.addWidget(title_bar)

        # ── Body ──
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 8, 16, 8)
        body_layout.setSpacing(8)

        # Elided: collector error messages can be hundreds of characters and
        # would otherwise set the window's minimum width (tooltip has it all).
        self.status_label = ElidedLabel("")
        self.status_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self._font(self._body, 10, spacing=1, register=self.status_label)
        body_layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        grid = QVBoxLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        scroll.setWidget(content)
        body_layout.addWidget(scroll)

        outer.addWidget(body)

        # ── Bottom row: resize grip, bottom-right ──
        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(grip)
        outer.addLayout(grip_row)

        # ── Panel rows (stretch ratios from the design's grid columns) ──
        row1 = QHBoxLayout(); row1.setSpacing(12)
        row1.addWidget(self._build_clock_panel(), 135)
        row1.addWidget(self._build_weather_panel(), 100)
        grid.addLayout(row1)

        row2 = QHBoxLayout(); row2.setSpacing(12)
        row2.addWidget(self._build_briefing_panel(), 120)
        row2.addWidget(self._build_todo_panel(), 100)
        grid.addLayout(row2)

        grid.addWidget(self._build_project_panel())

        row4 = QHBoxLayout(); row4.setSpacing(12)
        row4.addWidget(self._build_greed_panel(), 100)
        row4.addWidget(self._build_sector_panel(), 135)
        grid.addLayout(row4)

        grid.addWidget(self._build_news_panel())
        grid.addStretch()

        # CRT scanline overlay on top of everything
        self._scanlines = ScanlineOverlay(self)
        self._scanlines.setGeometry(self.rect())
        self._scanlines.raise_()

    def _icon_btn_style(self, hover_bg=None):
        hover_bg = hover_bg or BORDER
        return f"""
            QPushButton {{ background: transparent; color: {FAINT};
                border: 1px solid {BORDER}; font-family: '{self._body}'; font-size: 11px; }}
            QPushButton:hover {{ background: {hover_bg}; color: {TEXT}; }}
            QPushButton:disabled {{ color: {BORDER}; }}
        """

    # ── Panel scaffolding ────────────────────────────────────────────
    def _panel(self, title, caption=None, caption_color=FAINT, dim_header=False):
        """Bordered translucent panel with a header row. Returns
        (frame, content_layout, caption_label)."""
        box = QFrame()
        box.setObjectName("panel")
        box.setStyleSheet(
            f"QFrame#panel {{ background: {PANEL_BG}; border: 1px solid {BORDER}; }}"
        )
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 11, 14, 11)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        t = QLabel(title)
        t.setStyleSheet(f"color: {FAINT if dim_header else CYAN}; background: transparent; border: none;")
        self._font(self._display, 14 if dim_header else 16, spacing=3, register=t)
        if not dim_header:
            add_glow(t, CYAN, 14)
        head.addWidget(t)
        head.addStretch()
        cap = None
        if caption is not None:
            cap = QLabel(caption)
            cap.setStyleSheet(f"color: {caption_color}; background: transparent; border: none;")
            self._font(self._body, 10, spacing=2, register=cap)
            head.addWidget(cap)
        v.addLayout(head)
        return box, v, cap

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                # Detach before deleteLater — a widget that's merely removed
                # from the layout keeps painting at its old position until
                # the event loop processes the deferred delete.
                w.setParent(None)
                w.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    # ── Individual panels ────────────────────────────────────────────
    def _build_clock_panel(self):
        box, v, _ = self._panel("CLOCK", dim_header=True)
        self.time_label = QLabel("--:--")
        self.time_label.setStyleSheet(f"color: {CYAN}; background: transparent; border: none;")
        self._font(self._display, 50, register=self.time_label)
        add_glow(self.time_label, CYAN, 22)
        v.addWidget(self.time_label)
        self.date_label = QLabel("")
        self.date_label.setStyleSheet(f"color: {MUTED}; background: transparent; border: none;")
        self._font(self._body, 12, spacing=2, register=self.date_label)
        v.addWidget(self.date_label)
        v.addStretch()
        return box

    def _build_weather_panel(self):
        box, v, _ = self._panel("WEATHER", caption="LOCAL STATION", dim_header=True)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.weather_orb = GlowDot(30)
        row.addWidget(self.weather_orb)
        self.temp_label = QLabel("--°")
        self.temp_label.setStyleSheet(f"color: {TEXT}; background: transparent; border: none;")
        self._font(self._display, 38, register=self.temp_label)
        add_glow(self.temp_label, CYAN, 10)
        row.addWidget(self.temp_label)
        cond_col = QVBoxLayout()
        cond_col.setSpacing(2)
        self.cond_label = ElidedLabel("—")
        self.cond_label.setStyleSheet(f"color: {MUTED}; background: transparent;")
        self._font(self._body, 11, spacing=1, register=self.cond_label)
        cond_col.addWidget(self.cond_label)
        self.hilo_label = ElidedLabel("")
        self.hilo_label.setStyleSheet(f"color: {FAINT}; background: transparent;")
        self._font(self._body, 11, register=self.hilo_label)
        cond_col.addWidget(self.hilo_label)
        row.addLayout(cond_col, 1)
        v.addLayout(row)
        self.sparkline = Sparkline()
        v.addWidget(self.sparkline)
        return box

    def _build_briefing_panel(self):
        box, v, _ = self._panel("DAILY BRIEFING", caption="OLLAMA DIGEST")
        self.briefing_label = QLabel("—")
        self.briefing_label.setTextFormat(Qt.TextFormat.RichText)
        self.briefing_label.setWordWrap(True)
        self.briefing_label.setStyleSheet(f"color: {TEXT}; background: transparent; border: none;")
        self._font(self._body, 12, register=self.briefing_label)
        v.addWidget(self.briefing_label)
        v.addStretch()
        return box

    def _build_todo_panel(self):
        box, v, cap = self._panel("TO DO", caption="")
        self.todo_caption = cap
        self.todo_layout = QVBoxLayout()
        self.todo_layout.setSpacing(9)
        v.addLayout(self.todo_layout)
        v.addStretch()
        return box

    def _build_project_panel(self):
        box, v, _ = self._panel("PROJECT STATUS", caption="LAST UPDATED")
        self.project_layout = QVBoxLayout()
        self.project_layout.setSpacing(9)
        v.addLayout(self.project_layout)
        return box

    def _build_greed_panel(self):
        box, v, _ = self._panel("GREED INDEX")
        row = QHBoxLayout()
        row.setSpacing(14)
        self.greed_gauge = GreedGauge(self._body, self._display)
        row.addWidget(self.greed_gauge)
        self.greed_segments = SevenSegment()
        self.greed_segments.setMinimumHeight(96)
        row.addWidget(self.greed_segments, 1)
        v.addLayout(row)
        self.greed_delta = QLabel("")
        self.greed_delta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.greed_delta.setStyleSheet(f"color: {MUTED}; background: transparent; border: none;")
        self._font(self._body, 11, spacing=1, register=self.greed_delta)
        v.addWidget(self.greed_delta)
        return box

    def _build_sector_panel(self):
        box, v, _ = self._panel("SECTOR ANALYSIS", caption="% 1D")
        self.sector_layout = QVBoxLayout()
        self.sector_layout.setSpacing(7)
        v.addLayout(self.sector_layout)
        v.addStretch()
        return box

    def _build_news_panel(self):
        box, v, _ = self._panel("NEWS", caption="LIVE", caption_color=RED)
        self.news_layout = QVBoxLayout()
        self.news_layout.setSpacing(9)
        v.addLayout(self.news_layout)
        return box

    # ── Clock ────────────────────────────────────────────────────────
    def _tick_clock(self):
        now = dt.datetime.now()
        self.time_label.setText(now.strftime("%H:%M"))
        self.date_label.setText(
            f"{DAYS[now.weekday()]} · {now.day:02d} {MONS[now.month - 1]} {now.year}"
        )

    # ── Window events ────────────────────────────────────────────────
    def resizeEvent(self, event):
        self._apply_scale(event.size().width())
        if hasattr(self, "_scanlines"):
            self._scanlines.setGeometry(self.rect())
            self._scanlines.raise_()
        super().resizeEvent(event)

    def paintEvent(self, _event):
        # Deep-navy backdrop with the design's faint blue radial glow up top.
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG))
        glow = QRadialGradient(self.width() * 0.5, -self.height() * 0.04,
                               self.width() * 0.8)
        glow.setColorAt(0.0, QColor(40, 110, 180, 41))
        glow.setColorAt(1.0, QColor(40, 110, 180, 0))
        p.fillRect(self.rect(), glow)

    # ── File watching (no polling) ─────────────────────────────────────
    def _setup_watcher(self):
        self.watcher = QFileSystemWatcher()
        if CACHE_PATH.exists():
            self.watcher.addPath(str(CACHE_PATH))
        else:
            self.watcher.addPath(str(CACHE_PATH.parent))
        self.watcher.fileChanged.connect(self._on_cache_changed)
        self.watcher.directoryChanged.connect(self._on_cache_changed)

    def _on_cache_changed(self, _path):
        if CACHE_PATH.exists() and str(CACHE_PATH) not in self.watcher.files():
            self.watcher.addPath(str(CACHE_PATH))
        self._load_cache()

    # ── Cache loading / rendering ───────────────────────────────────────
    def _load_cache(self):
        if not CACHE_PATH.exists():
            self.status_label.setText("NO CACHE FILE YET — CLICK ↻ TO RUN THE COLLECTOR.")
            return
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return

        generated = data.get("generated_at", "unknown")
        self.status_label.setText(f"LAST SYNC {generated}")

        self._render_briefing(data.get("briefing"))
        self._render_todos(data.get("todos", []))
        self._render_projects(data.get("projects", []))
        self._render_weather(data.get("weather", {}))
        self._render_greed(data.get("greed", {}))
        self._render_sectors(data.get("sectors", {}))
        self._render_news(data.get("news", []))

        self._apply_scale(self.width())

    def _render_briefing(self, briefing):
        if not briefing:
            self.briefing_label.setText("—")
            return
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", briefing) if s.strip()]
        lines = [
            f"<span style='color:{CYAN};'>&gt;</span> {html.escape(s)}"
            for s in sentences
        ]
        self.briefing_label.setText("<br>".join(lines))

    def _render_todos(self, todos):
        self._clear_layout(self.todo_layout)
        self._todo_rows = []
        seen_keys = set()
        for t in todos:
            key = (t.get("text", ""), t.get("source_file", ""))
            seen_keys.add(key)
            row = TodoRow(t.get("text", ""), self._todo_done.get(key, False))
            self._font(self._body, 12, register=row.label)
            row.toggled.connect(self._on_todo_toggled)
            row._key = key
            self.todo_layout.addWidget(row)
            self._todo_rows.append(row)
        # Drop done-state for todos that no longer exist
        self._todo_done = {k: v for k, v in self._todo_done.items() if k in seen_keys}
        if not todos:
            lbl = QLabel("NOTHING OUTSTANDING.")
            lbl.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
            self._font(self._body, 12, register=lbl)
            self.todo_layout.addWidget(lbl)
        self._update_todo_caption()

    def _on_todo_toggled(self):
        row = self.sender()
        if isinstance(row, TodoRow):
            self._todo_done[row._key] = row.is_done()
        self._update_todo_caption()

    def _update_todo_caption(self):
        rows = getattr(self, "_todo_rows", [])
        if self.todo_caption is not None:
            done = sum(1 for r in rows if r.is_done())
            self.todo_caption.setText(f"{done}/{len(rows)} DONE" if rows else "IDLE")

    def _render_projects(self, projects):
        self._clear_layout(self.project_layout)
        projects = sorted(projects, key=lambda p: p.get("updated") or "", reverse=True)
        if not projects:
            lbl = QLabel("NO ACTIVE PROJECT NOTES.")
            lbl.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
            self._font(self._body, 12, register=lbl)
            self.project_layout.addWidget(lbl)
            return
        k = self._scale_factor()
        for i, proj in enumerate(projects):
            accent = AMBER if i == 0 else CYAN
            name_color = AMBER if i == 0 else MUTED
            time_color = AMBER if i == 0 else FAINT

            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)

            check = QLabel("✓")
            check.setFixedWidth(round(20 * k))
            check.setStyleSheet(f"color: {accent}; background: transparent; border: none;")
            self._font(self._body, 14, register=check)
            if i == 0:
                add_glow(check, AMBER, 8)
            h.addWidget(check)

            name = ElidedLabel(Path(proj.get("file", "")).stem.upper())
            name.setFixedWidth(round(150 * k))
            name.setStyleSheet(f"color: {name_color}; background: transparent;")
            self._font(self._body, 11, spacing=1, register=name)
            h.addWidget(name)

            status = ElidedLabel(proj.get("status", ""))
            status.setStyleSheet(f"color: {TEXT}; background: transparent;")
            self._font(self._body, 12, register=status)
            h.addWidget(status, 1)

            when = QLabel(hhmm(proj.get("updated")) or "--:--")
            when.setFixedWidth(round(52 * k))
            when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            when.setStyleSheet(f"color: {time_color}; background: transparent; border: none;")
            self._font(self._display, 14, register=when)
            h.addWidget(when)

            self.project_layout.addWidget(row)

    def _render_weather(self, w):
        if w.get("error"):
            self.temp_label.setText("--°")
            self.cond_label.setText(f"UNAVAILABLE: {soft_wrap(w['error'])}")
            self.hilo_label.setText("")
            self.sparkline.set_values([])
            return
        temp = w.get("temp_c")
        self.temp_label.setText(f"{round(temp)}°" if isinstance(temp, (int, float)) else "--°")
        desc = WMO_CODES.get(w.get("weather_code"), "—")
        self.cond_label.setText(desc.upper())
        hi, lo = w.get("high_c"), w.get("low_c")
        wind = w.get("wind_kph")
        hi_s = f"{round(hi)}°" if isinstance(hi, (int, float)) else "—"
        lo_s = f"{round(lo)}°" if isinstance(lo, (int, float)) else "—"
        wind_s = f" · {round(wind)} KM/H" if isinstance(wind, (int, float)) else ""
        self.hilo_label.setText(f"H {hi_s} · L {lo_s}{wind_s}")
        self.sparkline.set_values(w.get("hourly_temps") or [])

    def _render_greed(self, greed):
        if not greed or greed.get("error"):
            self.greed_gauge.set_score(None)
            self.greed_segments.set_value(None, FAINT)
            err = greed.get("error", "no data") if greed else "no data"
            self.greed_delta.setText(f"UNAVAILABLE: {soft_wrap(err)[:80]}")
            return
        score = greed.get("score")
        color = zone_color(score)
        self.greed_gauge.set_score(score)
        self.greed_segments.set_value(score, color)
        prev = greed.get("previous_close")
        if score is not None and prev is not None:
            delta = int(round(score - prev))
            self.greed_delta.setText(f"{delta:+d} VS. YESTERDAY")
        else:
            rating = (greed.get("rating") or "").upper()
            self.greed_delta.setText(rating)

    def _render_sectors(self, sectors):
        self._clear_layout(self.sector_layout)
        k = self._scale_factor()
        for key, section_label in (("sp500", "S&P 500"), ("tsx", "TSX")):
            rows = sectors.get(key, [])
            if not rows:
                continue
            # Sort here too (not just relying on collector.py) so display is
            # always correct even against stale/older cache data.
            rows = sorted(rows, key=lambda r: (r.get("change_pct") is None,
                                               -(r.get("change_pct") or 0)))

            header = QLabel(section_label)
            header.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
            self._font(self._body, 10, spacing=2, register=header)
            self.sector_layout.addWidget(header)

            max_abs = max((abs(r["change_pct"]) for r in rows
                           if r.get("change_pct") is not None), default=1) or 1
            for r in rows:
                self.sector_layout.addWidget(
                    self._sector_row(r, max_abs, k))

    def _sector_row(self, r, max_abs, k):
        pct = r.get("change_pct")
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        name = ElidedLabel(str(r.get("label", r.get("ticker", ""))).upper())
        name.setFixedWidth(round(140 * k))
        name.setToolTip(f"{r.get('ticker', '')} — {r.get('label', '')}")
        name.setStyleSheet(f"color: {MUTED}; background: transparent;")
        self._font(self._body, 11, spacing=1, register=name)
        h.addWidget(name)

        bar = DivergingBar()
        bar.set_value(pct, max_abs)
        h.addWidget(bar, 1)

        pct_lbl = QLabel("N/A" if pct is None else f"{pct:+.1f}")
        pct_lbl.setFixedWidth(round(46 * k))
        pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pct_lbl.setStyleSheet(f"color: {bar.color()}; background: transparent; border: none;")
        self._font(self._display, 14, register=pct_lbl)
        h.addWidget(pct_lbl)
        return row

    def _render_news(self, news):
        self._clear_layout(self.news_layout)
        k = self._scale_factor()
        for n in news:
            row = NewsRow(n.get("link", ""))
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)

            when = QLabel(hhmm(n.get("published")) or "--:--")
            when.setFixedWidth(round(46 * k))
            when.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
            self._font(self._body, 12, register=when)
            h.addWidget(when, 0, Qt.AlignmentFlag.AlignTop)

            source = ElidedLabel(str(n.get("source", "")).upper())
            source.setFixedWidth(round(110 * k))
            source.setStyleSheet(f"color: {CYAN}; background: transparent;")
            self._font(self._display, 13, spacing=1, register=source)
            h.addWidget(source, 0, Qt.AlignmentFlag.AlignTop)

            title = QLabel(n.get("title", ""))
            title.setWordWrap(True)
            title.setStyleSheet(f"color: {TEXT}; background: transparent; border: none;")
            self._font(self._body, 12, register=title)
            h.addWidget(title, 1)

            self.news_layout.addWidget(row)
        if not news:
            lbl = QLabel("NO HEADLINES.")
            lbl.setStyleSheet(f"color: {FAINT}; background: transparent; border: none;")
            self._font(self._body, 12, register=lbl)
            self.news_layout.addWidget(lbl)

    # ── Manual refresh ───────────────────────────────────────────────────
    def _manual_refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("…")
        self.status_label.setText("REFRESHING — THIS CAN TAKE A MINUTE (OLLAMA CALLS)...")
        self.runner.run_async()

    def _on_refresh_done(self, success, message):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻")
        if not success:
            self.status_label.setText(message)

    # ── Settings menu: density + always-on-top ────────────────────────
    def _show_settings_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background: {BG_TITLE}; color: {TEXT}; border: 1px solid {BORDER}; }}
            QMenu::item:selected {{ background: {BORDER}; }}
        """)

        def set_density(d):
            self._density = d
            self._apply_scale(self.width())

        menu.addAction("Layout: Comfortable", lambda: set_density("comfortable"))
        menu.addAction("Layout: Compact", lambda: set_density("compact"))
        menu.addSeparator()

        top_action = menu.addAction("Keep window on top")
        top_action.setCheckable(True)
        top_action.setChecked(self._always_on_top)
        top_action.toggled.connect(self._toggle_always_on_top)

        menu.addSeparator()
        menu.addAction("Quit", self.close)
        menu.exec(self.gear_btn.mapToGlobal(QPoint(0, self.gear_btn.height())))

    def _toggle_always_on_top(self, checked):
        self._always_on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()  # re-apply flags requires re-showing the window


def main():
    app = QApplication(sys.argv)
    load_local_fonts()
    display = pick_font(DISPLAY_FALLBACKS)
    body = pick_font(BODY_FALLBACKS)
    print(f"Display font: {display} | Body font: {body}")
    app.setFont(QFont(body, 9))
    w = DashboardWidget()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

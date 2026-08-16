"""Palette, fonts and small Qt helpers for the dashboard skin.

Replaces the retro-CRT/Nord neon look wholesale: deep navy ground, hard
corners (no border-radius anywhere), lowercase panel titles, cream text,
Fraunces for titles and Inter for everything else.
"""

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette

# ── Palette ──────────────────────────────────────────────────────────
BG = "#0B1120"          # window background
PANEL_BG = "#141B2E"    # panel background
BORDER = "#232E47"      # panel border
WHITE = "#FFFFFF"       # highlight / accent
CHROME = "#C9CDD6"      # secondary chrome
CREAM = "#F0E6D2"       # text

# Derived tones — not in the spec's five, but needed for hierarchy. All are
# mixes of the above against the panel background, nothing new hue-wise.
FAINT = "#7C879E"       # captions, axis labels, disabled text
DOT_EMPTY = "#1D2740"   # an un-filled habit/journal dot
HOVER = "#1B2540"       # hover fill for buttons and dots


def mix(color_hex: str, other_hex: str, ratio: float) -> str:
    """Blend two #rrggbb colors — ratio 0.0 is all `color_hex`."""
    a = [int(color_hex[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other_hex[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * ratio):02x}" for x, y in zip(a, b))


DONE_TEXT = mix(CREAM, PANEL_BG, 0.55)   # completed to-do, still readable
CREAM_DIM = mix(CREAM, PANEL_BG, 0.35)

# ── Fonts ────────────────────────────────────────────────────────────
FONT_DIR = Path(__file__).resolve().parent / "fonts"

TITLE_FAMILIES = [  # panel titles — Fraunces
    "Fraunces", "Fraunces 9pt", "Fraunces 72pt",
    "Georgia", "Cambria", "Times New Roman", "Serif",
]
BODY_FAMILIES = [  # everything else — Inter
    "Inter", "Inter Display", "Inter Tight",
    "Segoe UI Variable Text", "Segoe UI", "Arial", "Helvetica",
]
MONO_FAMILIES = [  # kept for raw code/log display only; nothing uses it today
    "JetBrains Mono", "JetBrainsMono NF", "Cascadia Mono", "Consolas",
]

# Base pixel sizes, scaled at runtime by the window's width factor. Roughly
# 8–11pt at 96dpi: Fraunces' low x-height reads thin below ~13px, so titles sit
# at the top of the spec's 6–10pt range rather than the bottom.
TITLE_PX = 13
GROUP_PX = 11
BODY_PX = 12
SMALL_PX = 11
CAPTION_PX = 10
BIG_PX = 34


def load_local_fonts() -> None:
    """Register any .ttf/.otf dropped into ./fonts (Inter + Fraunces from
    Google Fonts) so the skin works without installing them system-wide."""
    if FONT_DIR.is_dir():
        for path in sorted(list(FONT_DIR.glob("*.ttf")) + list(FONT_DIR.glob("*.otf"))):
            QFontDatabase.addApplicationFont(str(path))


def apply_app_style(app) -> None:
    """Fusion + a dark palette, applied before any widget is built.

    Windows' native style paints complex widgets (QComboBox, QDoubleSpinBox)
    with its own light chrome and only partly honours stylesheets on them, so
    the rating spinner and the month dropdown come out white-on-white. Fusion
    respects the stylesheet everywhere, and the palette covers the bits no
    stylesheet reaches (tooltips, the text cursor, disabled text).
    """
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(CREAM))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(CREAM))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(FAINT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(CREAM))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(BORDER))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(WHITE))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(CREAM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
                     QColor(FAINT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
                     QColor(BORDER))
    app.setPalette(palette)


def pick_font(candidates: list) -> str:
    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def tabular(font: QFont) -> QFont:
    """Turn on tabular numerals so columns of numbers line up. Inter ships the
    `tnum` feature; QFont.setFeature needs Qt 6.7+, hence the guard."""
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass
    return font


def rgba(color_hex: str, alpha: float) -> str:
    r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


# ── Shared stylesheet fragments ──────────────────────────────────────

def panel_style(name: str = "panel") -> str:
    return (f"QFrame#{name} {{ background: {PANEL_BG}; border: 1px solid {BORDER}; }}")


def button_style(body_family: str, accent: str = CREAM, hover_bg: str = None) -> str:
    hover_bg = hover_bg or HOVER
    return f"""
        QPushButton {{ background: transparent; color: {accent};
            border: 1px solid {BORDER}; padding: 3px 8px;
            font-family: '{body_family}'; font-size: 11px; }}
        QPushButton:hover {{ background: {hover_bg}; color: {WHITE};
            border-color: {CHROME}; }}
        QPushButton:disabled {{ color: {BORDER}; border-color: {BORDER}; }}
    """


def input_style(body_family: str) -> str:
    return f"""
        QLineEdit, QPlainTextEdit, QTextEdit, QDoubleSpinBox, QComboBox {{
            background: {BG}; color: {CREAM}; border: 1px solid {BORDER};
            padding: 3px 6px; selection-background-color: {BORDER};
            selection-color: {WHITE}; font-family: '{body_family}'; }}
        QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus,
        QComboBox:focus {{ border-color: {CHROME}; }}
        QComboBox QAbstractItemView {{ background: {PANEL_BG}; color: {CREAM};
            border: 1px solid {BORDER}; selection-background-color: {BORDER};
            selection-color: {WHITE}; outline: none; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 12px;
            background: transparent; border-left: 1px solid {BORDER}; }}
    """


def scrollbar_style() -> str:
    return f"""
        QScrollArea {{ background: transparent; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {BORDER}; min-height: 24px; }}
        QScrollBar::handle:vertical:hover {{ background: {FAINT}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


def menu_style() -> str:
    return f"""
        QMenu {{ background: {PANEL_BG}; color: {CREAM};
            border: 1px solid {BORDER}; }}
        QMenu::item {{ padding: 4px 18px 4px 12px; }}
        QMenu::item:selected {{ background: {BORDER}; color: {WHITE}; }}
        QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 0; }}
    """


def dialog_style(body_family: str) -> str:
    return f"""
        QDialog {{ background: {BG}; color: {CREAM}; }}
        QLabel {{ color: {CREAM}; background: transparent;
            font-family: '{body_family}'; }}
    """ + input_style(body_family) + button_style(body_family) + scrollbar_style()

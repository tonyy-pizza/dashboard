"""Display conventions, matched to the pipeline's rather than invented.

scan_report.py and stock_evaluator.py agree on how a 0-10 score is coloured
(>= 7.5 green, >= 5.0 yellow, below that red) and on what the compact flag block
says. Those are the conventions here too - the same score has to read the same
way whether the person is looking at the terminal report or this dashboard.

Only the medium changes: ANSI escapes become CSS, and flag_cells()'s glyph
column becomes short text labels a table can sort on.
"""

from __future__ import annotations

import math
from typing import Optional

# scan_report.colour()'s thresholds, as colours a browser understands. The
# terminal palette is bright green / yellow / red; these are the readable
# equivalents on Streamlit's light and dark backgrounds.
GREEN = "#1a7f37"
YELLOW = "#9a6700"
RED = "#cf222e"
DIM = "#8b949e"

GOOD = 7.5      # stock_evaluator.colour: >= 7.5 is green and bold
FAIR = 5.0      # >= 5.0 is yellow, below is red


def num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def score_colour(value) -> str:
    """scan_report.colour()'s thresholds, as a CSS colour."""
    value = num(value)
    if value is None:
        return DIM
    if value >= GOOD:
        return GREEN
    if value >= FAIR:
        return YELLOW
    return RED


def score_css(value) -> str:
    """A pandas Styler cell rule for a 0-10 score."""
    value = num(value)
    if value is None:
        return f"color: {DIM}"
    weight = "; font-weight: 600" if value >= GOOD else ""
    return f"color: {score_colour(value)}{weight}"


def score_html(value, digits: int = 2) -> str:
    value = num(value)
    if value is None:
        return f'<span style="color:{DIM}">n/a</span>'
    weight = "font-weight:600;" if value >= GOOD else ""
    return f'<span style="color:{score_colour(value)};{weight}">{value:.{digits}f}</span>'


def bar(value, width: int = 14) -> str:
    """stock_evaluator.bar(), same fill characters."""
    value = num(value)
    value = max(0.0, min(10.0, value or 0.0))
    filled = int(round((value / 10) * width))
    return "█" * filled + "░" * (width - filled)


# ── flag semantics, from scan_report.flag_cells() ─────────────────────────

# The conviction verdicts position_sizer assigns and what scan_report's legend
# calls them. Same words, so the two reports can be read side by side.
CONVICTION_LABELS = {
    "disconnect_supported": ("disconnect+", GREEN,
                             "price low, trend intact, public read agrees"),
    "disconnect_contested": ("disconnect?", YELLOW,
                             "price low and trend intact, but sentiment contests it"),
    "disconnect_unverified": ("disconnect·", GREEN,
                              "price low, trend intact, no sentiment on file"),
    "trap_confirmed": ("value-trap!", RED,
                       "trend and sentiment both negative"),
    "trap_contested": ("trap-hype", RED,
                       "trend down but sentiment high"),
    "trap_unverified": ("value-trap", RED,
                        "trend deteriorating, no sentiment on file"),
    "not_applicable": ("·", DIM, "divergence pattern is neutral"),
}

# The sizing consequence of each verdict, from position_sizer.CONVICTION_SCALE.
# Shown next to the label so a cut has a visible cause.
CONVICTION_HELP = (
    "Sentiment crossed with the price/fundamentals divergence, from "
    "position_sizer.py. disconnect+ / disconnect· keep full size; "
    "disconnect? costs 25%; value-trap 35-50%."
)


def conviction_label(candidate: dict) -> str:
    verdict = ((candidate.get("sizing") or {}).get("conviction") or "not_applicable")
    return CONVICTION_LABELS.get(verdict, ("·", DIM, ""))[0]


def conviction_colour(verdict: Optional[str]) -> str:
    return CONVICTION_LABELS.get(verdict or "not_applicable", ("", DIM, ""))[1]


def liquidity_label(candidate: dict) -> str:
    """flag_cells()'s liquidity cell: a flag means thin, not absent."""
    return "✗ thin" if candidate.get("liquidity_flag") else "✓ ok"


def trend_label(candidate: dict) -> str:
    """flag_cells()'s trend cell. The dot means insufficient history, not False.

    Worth keeping distinct: stock_evaluator's roa_trend_consistent demands a
    non-decreasing ROA in every year it has, so None (too few years) and False
    (a real down year) mean genuinely different things.
    """
    trend = candidate.get("roa_trend_consistent")
    if trend is True:
        return "✓ trend"
    if trend is False:
        return "✗ trend"
    return "· trend"


DIVERGENCE_LABELS = {
    "price_disconnect": "price disconnect",
    "trend_confirms_decline": "trend confirms decline",
    "neutral": "neutral",
}


# ── formatting ────────────────────────────────────────────────────────────

def pct(value, digits: int = 1) -> str:
    value = num(value)
    return "n/a" if value is None else f"{value:.{digits}f}%"


def ratio(value, digits: int = 2) -> str:
    value = num(value)
    return "n/a" if value is None else f"{value:.{digits}f}"


def money(value) -> str:
    """stock_evaluator's compact money format."""
    value = num(value)
    if value is None:
        return "n/a"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.2f}"


def guide_headline(guide: Optional[str]) -> str:
    """The first clause of a position_guidance string.

    "Core: 3%-5%; up to 8% with diversification." -> "Core: 3%-5%", which is
    what scan_report prints in its candidate rows.
    """
    if not guide:
        return "n/a"
    return guide.split(";")[0].strip()

"""Read the pipeline's JSON output off disk. Nothing here writes or fetches.

Every file is optional. The dashboard may be opened before a scan has ever run,
or between scans, so each loader returns a Loaded record carrying the document
(or None), the path, and why it is missing - never an exception.

Freshness follows scan_report.py: a file's age comes from its own generated_at
stamp, falling back to mtime, because generated_at says when the DATA was
produced and mtime only says when the file was last touched.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import streamlit as st

from .pipeline import APP_DIR, PIPELINE, PIPELINE_DIR

# The pipeline's own filenames, in the order the pipeline writes them.
FILENAMES = {
    "candidates": "candidates.json",
    "scored": "scored_candidates.json",
    "sentiment": "sentiment.json",
    "clustered": "clustered.json",
    "sized": "sized_candidates.json",
    "holdings": "holdings.json",
}

# What the scan is expected to have refreshed. Mirrors market_data.TTL_PRICE
# (1 day), which is the TTL scan_report.py judges these stages against.
TTL_PRICE = 24 * 60 * 60


def data_dir() -> Path:
    """<STOCKS_DATA_DIR>, else market_data.BASE_DIR/data - the project convention.

    With market_data unimportable there is no BASE_DIR to ask, so the pipeline
    folder we did find (or the folder above the app) stands in. The sidebar
    shows the resolved path either way, so a wrong guess is visible rather than
    silent.
    """
    override = os.environ.get("STOCKS_DATA_DIR")
    if override:
        return Path(override)
    base = PIPELINE.base_dir()
    if base is not None:
        return base / "data"
    if PIPELINE_DIR is not None:
        return PIPELINE_DIR / "data"
    return APP_DIR.parent / "data"


def _num(value) -> Optional[float]:
    """scan_report._num: float or None, never NaN."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# ── freshness (scan_report.py's rules) ────────────────────────────────────

def file_age_seconds(path: Path, document: Optional[dict]) -> Optional[float]:
    """Age from generated_at, falling back to mtime. scan_report.py's rule."""
    path = Path(path)
    if not path.exists():
        return None
    stamp = (document or {}).get("generated_at")
    if stamp:
        try:
            when = dt.datetime.fromisoformat(str(stamp))
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
            return (dt.datetime.now() - when).total_seconds()
        except (TypeError, ValueError):
            pass
    try:
        return (dt.datetime.now()
                - dt.datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    except OSError:
        return None


def describe_age(seconds: Optional[float]) -> str:
    """scan_report.describe_age, verbatim thresholds."""
    if seconds is None:
        return "missing"
    if seconds < 90:
        return f"{seconds:.0f}s old"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m old"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h old"
    return f"{seconds / 86400:.1f}d old"


@dataclass
class Loaded:
    """One JSON file: what was in it, where it came from, how old it is."""
    key: str
    path: Path
    document: Optional[dict] = None
    error: Optional[str] = None
    age_seconds: Optional[float] = None

    @property
    def exists(self) -> bool:
        return self.document is not None

    @property
    def stale(self) -> bool:
        """Older than the 1-day TTL the scan's own stages are judged against."""
        return self.age_seconds is not None and self.age_seconds >= TTL_PRICE

    @property
    def age_text(self) -> str:
        return describe_age(self.age_seconds)

    @property
    def generated_at(self) -> Optional[str]:
        return (self.document or {}).get("generated_at")


def _stat_key(path: Path):
    """Cache key that changes when the file does.

    The pipeline may be re-run while the dashboard is open, so caching on the
    path alone would serve yesterday's scan indefinitely.
    """
    try:
        info = path.stat()
        return (str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        return (str(path), None, None)


@st.cache_data(show_spinner=False)
def _read_json(key: tuple) -> tuple[Optional[dict], Optional[str]]:
    """Read one JSON file. `key` is (path, mtime_ns, size) and is the cache key.

    The name must not start with an underscore: Streamlit excludes
    underscore-prefixed arguments from the cache key, which would make every
    file after the first a cache hit on the first one's contents.
    """
    path = Path(key[0])
    if not path.exists():
        return None, "not found"
    try:
        with open(path, "r", encoding="utf-8") as f:
            document = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc})"
    except OSError as exc:
        return None, f"could not be read ({exc})"
    if not isinstance(document, dict):
        return None, "unexpected shape (expected a JSON object)"
    return document, None


def load(key: str, directory: Optional[Path] = None) -> Loaded:
    """Read one of the pipeline's files. Never raises."""
    path = Path(directory or data_dir()) / FILENAMES[key]
    document, error = _read_json(_stat_key(path))
    return Loaded(key=key, path=path, document=document, error=error,
                  age_seconds=file_age_seconds(path, document))


@dataclass
class Scan:
    """Everything the dashboard reads, loaded once per rerun."""
    directory: Path
    sized: Loaded
    clustered: Loaded
    scored: Loaded
    holdings: Loaded
    sentiment: Loaded
    _scored_index: dict = field(default_factory=dict, repr=False)

    # ── the primary source ────────────────────────────────────────────────
    @property
    def candidates(self) -> list:
        return (self.sized.document or {}).get("candidates") or []

    @property
    def params(self) -> dict:
        return (self.sized.document or {}).get("params") or {}

    @property
    def holdings_rows(self) -> list:
        """Holdings as position_sizer recorded them in sized_candidates.json.

        This is the file's own copy, already filtered of _example entries by
        position_sizer.load_holdings(). holdings.json itself is read separately
        for the holdings view, which has to do that filtering itself.
        """
        return (self.sized.document or {}).get("holdings") or []

    @property
    def holdings_review(self) -> list:
        """The exit review position_sizer runs over current holdings."""
        return (self.sized.document or {}).get("holdings_review") or []

    @property
    def sentiment_scores(self) -> dict:
        return (self.sentiment.document or {}).get("sentiment") or {}

    @property
    def clusters(self) -> list:
        return (self.clustered.document or {}).get("clusters") or []

    @property
    def standalone(self) -> list:
        return (self.clustered.document or {}).get("standalone") or []

    @property
    def standalone_notes(self) -> dict:
        return (self.clustered.document or {}).get("standalone_notes") or {}

    @property
    def slim(self) -> bool:
        """True when sized_candidates.json was written with --slim.

        The slim output drops metrics/frameworks/value_screen/insider, so the
        drill-down has to fall back to scored_candidates.json for those.
        """
        if self.params.get("slim"):
            return True
        return any("metrics" not in c for c in self.candidates)

    def scored_record(self, ticker: str) -> Optional[dict]:
        """The full per-ticker blob, from wherever it survives.

        A non-slim sized_candidates.json already carries metrics and frameworks;
        only a slim one needs scored_candidates.json, which may itself be absent
        or from a different run.
        """
        if not self._scored_index:
            for record in (self.scored.document or {}).get("scored") or []:
                key = str(record.get("ticker") or "").strip().upper()
                if key:
                    self._scored_index[key] = record
        return self._scored_index.get((ticker or "").strip().upper())

    @property
    def any_data(self) -> bool:
        return self.sized.exists or self.clustered.exists or self.scored.exists

    @property
    def stale_files(self) -> list:
        return [f for f in (self.sized, self.clustered, self.scored, self.sentiment)
                if f.exists and f.stale]


def load_scan(directory: Optional[Path] = None) -> Scan:
    directory = Path(directory or data_dir())
    return Scan(
        directory=directory,
        sized=load("sized", directory),
        clustered=load("clustered", directory),
        scored=load("scored", directory),
        holdings=load("holdings", directory),
        sentiment=load("sentiment", directory),
    )


# ── holdings.json, read directly ──────────────────────────────────────────

# Suffixes Yahoo uses for Canadian listings. Only consulted when the scan does
# not already know a holding's quote_currency.
_CAD_SUFFIXES = (".TO", ".V", ".CN", ".NE")


def holding_entries(document: Optional[dict]) -> list:
    """holdings.json -> the entries the pipeline would actually act on.

    position_sizer.load_holdings() skips entries marked _example, so an
    untouched template counts as no holdings. Read directly, this file has to
    apply the same rule, and it also keeps the skipped rows so the holdings
    view can show that a template is sitting there unedited.
    """
    kept, ignored = [], []
    for entry in (document or {}).get("holdings") or []:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        row = {
            "ticker": ticker,
            "shares": _num(entry.get("shares")),
            "cost_basis": _num(entry.get("cost_basis")),
        }
        if entry.get("_example"):
            row["ignored_because"] = "marked _example - the untouched template"
            ignored.append(row)
        elif not ticker or ticker == "...":
            row["ignored_because"] = "no ticker"
            ignored.append(row)
        else:
            kept.append(row)
    return kept, ignored


def infer_currency(ticker: str, scan: Optional[Scan] = None) -> Optional[str]:
    """A holding's quote currency, preferring what the scan already knows.

    holdings.json records no currency, so a cost-basis total over mixed
    listings would silently add CAD to USD. The scan knows quote_currency for
    every name it scored; the suffix rule only covers what it does not.
    """
    ticker = (ticker or "").strip().upper()
    if scan is not None:
        for candidate in scan.candidates:
            if candidate.get("ticker") == ticker and candidate.get("quote_currency"):
                return str(candidate["quote_currency"]).upper()
        record = scan.scored_record(ticker)
        if record and record.get("quote_currency"):
            return str(record["quote_currency"]).upper()
    if any(ticker.endswith(suffix) for suffix in _CAD_SUFFIXES):
        return "CAD"
    return None


def clear_cache() -> None:
    """Drop every cached read, for the sidebar's refresh button."""
    _read_json.clear()

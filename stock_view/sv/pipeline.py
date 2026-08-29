"""Locate the stocks pipeline and import the functions stock_view reuses.

stock_view deliberately owns no sizing math. sizing_scale(), apply_reduction()
and position_guidance() are imported from position_sizer.py / stock_evaluator.py
so the live recompute cannot drift from what the pipeline actually wrote.

The path search is the pipeline's own _add_script_dir_to_path() pattern, widened
by one level because stock_view sits in a folder BESIDE the pipeline scripts
(C:\\Users\\joey\\stocks\\stock_view\\ next to C:\\Users\\joey\\stocks\\).

The import is optional on purpose. Every view that only reads JSON works with
nothing but streamlit/pandas/plotly installed; the sizing simulator is the one
place that needs the real functions, and it says so plainly when they are not
importable rather than substituting a lookalike of its own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent          # .../stock_view/sv
APP_DIR = HERE.parent                           # .../stock_view


def _candidate_dirs():
    """Where market_data.py might live, best guess first.

    STOCKS_DIR is checked first because market_data.py honours it for BASE_DIR;
    if the person has pointed the pipeline somewhere, point the imports there
    too rather than finding a second copy by accident.
    """
    env = os.environ.get("STOCKS_DIR")
    if env:
        yield Path(env)
    yield APP_DIR                     # pipeline scripts dropped in beside us
    yield APP_DIR.parent              # the suggested layout: stocks/stock_view/
    yield APP_DIR.parent / "stocks"   # repo layout: <repo>/stock_view + <repo>/stocks
    yield APP_DIR.parent.parent
    yield APP_DIR.parent.parent / "stocks"


def add_pipeline_to_path() -> Optional[Path]:
    """Put the pipeline directory on sys.path. Returns it, or None."""
    for candidate in _candidate_dirs():
        try:
            if (candidate / "market_data.py").exists():
                resolved = str(candidate.resolve())
                if resolved not in sys.path:
                    sys.path.insert(0, resolved)
                return candidate.resolve()
        except OSError:
            continue
    return None


PIPELINE_DIR = add_pipeline_to_path()


class Pipeline:
    """The imported pipeline surface, plus why it is missing when it is.

    Attributes are None until load() succeeds. `error` carries the failure in
    the person's own terms, because "no module named yfinance" on a dashboard
    that never touches the network needs the explanation that its sizing
    functions come from a script that does.
    """

    def __init__(self):
        self.dir = PIPELINE_DIR
        self.ok = False
        self.error: Optional[str] = None
        self.md = None                  # market_data
        self.sizing_scale = None        # position_sizer.sizing_scale
        self.apply_reduction = None     # position_sizer.apply_reduction
        self.parse_guide_range = None   # position_sizer.parse_guide_range
        self.position_guidance = None   # stock_evaluator.position_guidance
        self.defaults = {
            # Mirrors of position_sizer's module constants, replaced by the real
            # values on a successful import. They are only ever fallbacks for
            # slider defaults when a file carries no params of its own.
            "correlation_threshold": 0.70,
            "reduction_factor": 0.50,
            "correlation_basis": "raw",
        }

    def load(self) -> "Pipeline":
        if self.ok or self.error:
            return self
        if self.dir is None:
            self.error = (
                "Could not find market_data.py. stock_view looks in $STOCKS_DIR, "
                "its own folder, and the folder above it. Put stock_view/ next to "
                "the pipeline scripts, or set STOCKS_DIR to the folder holding them."
            )
            return self
        try:
            import market_data as md
            from position_sizer import (CORRELATION_BASIS, CORR_THRESHOLD,
                                        REDUCTION_FACTOR, apply_reduction,
                                        parse_guide_range, sizing_scale)
            from stock_evaluator import position_guidance
        except Exception as exc:                      # noqa: BLE001
            self.error = (
                f"{type(exc).__name__}: {exc}\n\n"
                f"Looked in {self.dir}. The sizing simulator imports sizing_scale() "
                f"and apply_reduction() from position_sizer.py rather than "
                f"re-deriving them, and position_sizer.py pulls in market_data.py "
                f"(yfinance, curl_cffi, requests) at import time. Installing the "
                f"pipeline's own dependencies in this interpreter fixes it. No "
                f"network call is made by importing them."
            )
            return self

        self.md = md
        self.sizing_scale = sizing_scale
        self.apply_reduction = apply_reduction
        self.parse_guide_range = parse_guide_range
        self.position_guidance = position_guidance
        self.defaults = {
            "correlation_threshold": CORR_THRESHOLD,
            "reduction_factor": REDUCTION_FACTOR,
            "correlation_basis": CORRELATION_BASIS,
        }
        self.ok = True
        return self

    def base_dir(self) -> Optional[Path]:
        """market_data.BASE_DIR, when it could be imported."""
        if self.md is not None:
            return Path(self.md.BASE_DIR)
        return None


PIPELINE = Pipeline().load()

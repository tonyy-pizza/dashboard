#!/usr/bin/env python3
r"""stock_view - an interactive local view over the stocks scan pipeline's output.

    universe_screen -> stock_evaluator --batch -> rmt_cluster -> position_sizer
                    -> scan_report          -> (this)

It reads the JSON those stages already wrote. It never runs them. If the data
looks stale, the answer is to run scan_report.py in a terminal and press Reload
here - there is no button in this app that will do it for you, on purpose:
re-running the scan means hundreds of network requests and a rate-limited
sentiment stage, which is not something a dashboard should start by accident.

The one thing it does compute is sizing, and it computes it with
position_sizer.py's own functions rather than a copy of them. Everything the
threshold / reduction / basis controls need is already in sized_candidates.json:
each candidate carries both the raw and the MP-cleaned correlation to every
holding it was compared against, so moving a slider is arithmetic over data in
memory, not a refetch.

Launch:
    streamlit run stock_view.py

    (or double-click launch.bat)
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# stock_view.py is the entry point Streamlit runs, so `sv` has to be importable
# from wherever the person launched it.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from sv import data_loader as dl                       # noqa: E402
from sv.pipeline import PIPELINE                        # noqa: E402
from sv.views import clusters, drilldown, holdings, overview, simulator  # noqa: E402

VIEWS = {
    "Overview": overview.render,
    "Cluster explorer": clusters.render,
    "Position sizing simulator": simulator.render,
    "Ticker drill-down": drilldown.render,
    "Holdings": holdings.render,
}

DISCLAIMER = "⚠ For informational use only. Not financial advice."


def sidebar(scan):
    """Where the data came from, how old it is, and the reload button."""
    st.sidebar.title("stock_view")
    st.sidebar.caption("A read-only view over the scan pipeline's output.")

    view = st.sidebar.radio("View", list(VIEWS), key="view")
    st.sidebar.divider()

    if st.sidebar.button("↻ Reload from disk", width="stretch",
                         help="Re-read every JSON file. Press this after running "
                              "scan_report.py in a terminal."):
        dl.clear_cache()
        st.rerun()

    st.sidebar.markdown("**Data**")
    st.sidebar.caption(f"`{scan.directory}`")
    for loaded, label, ages in ((scan.sized, "sized_candidates", True),
                                (scan.clustered, "clustered", True),
                                (scan.scored, "scored_candidates", True),
                                (scan.sentiment, "sentiment", True),
                                # holdings.json is hand-edited and carries no
                                # generated_at: an old one is a settled portfolio,
                                # not stale data.
                                (scan.holdings, "holdings", False)):
        if not loaded.exists:
            st.sidebar.caption(f"⚪ {label} — {loaded.error or 'missing'}")
        elif ages:
            st.sidebar.caption(f"{'🟡' if loaded.stale else '🟢'} {label} — "
                               f"{loaded.age_text}")
        else:
            st.sidebar.caption(f"🟢 {label} — edited {loaded.age_text}")

    st.sidebar.divider()
    st.sidebar.markdown("**Pipeline**")
    if PIPELINE.ok:
        st.sidebar.caption(f"🟢 imported from `{PIPELINE.dir}`")
        st.sidebar.caption("sizing_scale / apply_reduction / position_guidance are "
                           "position_sizer.py's and stock_evaluator.py's own.")
    else:
        st.sidebar.caption("🔴 not importable — the sizing simulator is unavailable")

    return view


def no_data_yet(scan):
    st.title("stock_view")
    st.warning("**No scan data yet.**")
    st.markdown(
        f"stock_view reads the pipeline's JSON out of `{scan.directory}` and found "
        f"nothing usable there. Run the scan first:\n\n"
        f"```\npy scan_report.py\n```\n\n"
        f"That runs whatever is stale — `universe_screen.py` → "
        f"`stock_evaluator.py --batch` → sentiment → `rmt_cluster.py` → "
        f"`position_sizer.py` — and writes the files this dashboard reads. "
        f"Then press **↻ Reload from disk** in the sidebar.")

    rows = []
    for loaded, label in ((scan.sized, "sized_candidates.json"),
                          (scan.clustered, "clustered.json"),
                          (scan.scored, "scored_candidates.json"),
                          (scan.sentiment, "sentiment.json"),
                          (scan.holdings, "holdings.json")):
        rows.append(f"- `{label}` — {'found' if loaded.exists else (loaded.error or 'missing')}")
    st.markdown("\n".join(rows))

    st.divider()
    st.caption(
        f"Looking in `{scan.directory}`. That is `$STOCKS_DATA_DIR` if set, else "
        f"`market_data.BASE_DIR / \"data\"` — the same convention every script in the "
        f"project uses. Set `STOCKS_DATA_DIR` before launching to point it elsewhere.")
    if not PIPELINE.ok:
        with st.expander("The pipeline modules could not be imported either"):
            st.code(PIPELINE.error or "unknown import failure")


def main():
    st.set_page_config(page_title="stock_view", page_icon="📊", layout="wide")

    scan = dl.load_scan()
    view = sidebar(scan)

    # holdings.json is hand-edited and exists independently of any scan, so the
    # holdings view is worth showing even before the first one has run.
    if not scan.any_data:
        if view == "Holdings" and scan.holdings.exists:
            holdings.render(scan)
            st.divider()
            st.caption(f"No scan data in `{scan.directory}` yet — run "
                       f"`py scan_report.py` for the other views.")
            return
        no_data_yet(scan)
        return

    if not scan.sized.exists and view in ("Overview", "Position sizing simulator",
                                          "Ticker drill-down"):
        st.title("stock_view")
        st.warning(
            f"`sized_candidates.json` is missing from `{scan.directory}`, and this "
            f"view is built on it. Run `py scan_report.py`, then press ↻ Reload. "
            f"The cluster explorer and holdings views work without it.")
        return

    stale = scan.stale_files
    if stale:
        names = ", ".join(f"{f.key} ({f.age_text})" for f in stale)
        st.warning(f"**Stale data:** {names}. These are past the 1-day TTL the "
                   f"pipeline's own stages are judged against. Run `py scan_report.py` "
                   f"and press ↻ Reload — stock_view will not run it for you.")

    VIEWS[view](scan)

    st.divider()
    generated = scan.sized.generated_at or scan.clustered.generated_at
    st.caption(
        (f"Scan generated {generated}  ·  " if generated else "")
        + f"read from `{scan.directory}`  ·  stock_view never re-runs the pipeline "
          f"and never writes to its output.")
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()

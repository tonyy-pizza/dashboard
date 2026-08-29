"""Position sizing simulator - the core view.

Move a control, and every not-yet-held candidate is re-sized from the
correlations already on disk. No network call, no re-run of position_sizer.py.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import display as d
from .. import sizing as sz
from ..pipeline import PIPELINE


def _controls(params) -> sz.Controls:
    defaults = sz.controls_from_params(params)

    st.sidebar.markdown("### Sizing controls")
    threshold = st.sidebar.slider(
        "Correlation threshold", 0.0, 1.0, float(defaults.threshold), 0.01,
        key="sim_threshold",
        help="Above this correlation with a holding, a candidate is treated as "
             "partly the same position. position_sizer.py's default is 0.70.")
    reduction = st.sidebar.slider(
        "Reduction factor", 0.0, 1.0, float(defaults.reduction_factor), 0.01,
        key="sim_reduction",
        help="How much of the base size a perfectly correlated (1.0) duplicate loses.")
    basis = st.sidebar.radio(
        "Correlation basis", ["raw", "cleaned"],
        index=0 if defaults.basis == "raw" else 1, horizontal=True, key="sim_basis",
        help="raw is the sample correlation, which is what a correlation threshold "
             "is conventionally quoted against. cleaned is the same matrix through "
             "the Marchenko-Pastur filter, which runs lower — a 0.70 bar on cleaned "
             "values behaves like roughly a 0.75 bar on raw ones.")
    mode = st.sidebar.radio(
        "Reduction mode", ["proportional", "flat"],
        index=1 if defaults.flat else 0, horizontal=True, key="sim_mode",
        help="proportional takes nothing at the threshold and the full factor at 1.0. "
             "flat applies the whole factor the moment the threshold is crossed.")
    conviction = st.sidebar.checkbox(
        "Apply the conviction modifier", value=True, key="sim_conviction",
        help="position_sizer.py multiplies the correlation scale by a second, "
             "independent conviction scale from the sentiment stage. Unticking this "
             "is position_sizer.py --no-conviction: the verdict is still reported, "
             "it just stops changing size.")

    if st.sidebar.button("Reset to the scan's own settings", width="stretch"):
        for key in ("sim_threshold", "sim_reduction", "sim_basis", "sim_mode",
                    "sim_conviction"):
            st.session_state.pop(key, None)
        st.rerun()

    return sz.Controls(threshold=threshold, reduction_factor=reduction, basis=basis,
                       flat=(mode == "flat"), apply_conviction=conviction)


def _frame(results) -> pd.DataFrame:
    rows = []
    for item in results:
        rows.append({
            "Ticker": item.ticker,
            "Name": item.name,
            "Sector": item.sector or "Unknown",
            "Composite": item.composite,
            "Base guide": d.guide_headline(item.base_guide),
            "Adjusted guide": d.guide_headline(item.adjusted_guide),
            "Base %": item.base_high,
            "Adj %": item.adjusted_high,
            "Cut %": item.cut_pct,
            "Max corr": item.max_correlation,
            "Driven by": item.correlated_with or "—",
            "Conviction": (d.CONVICTION_LABELS.get(item.conviction or "not_applicable",
                                                   ("·",))[0]),
            "Conv ×": item.conviction_scale,
        })
    frame = pd.DataFrame(rows)
    if len(frame):
        frame = frame.sort_values("Composite", ascending=False, na_position="last")
    return frame


def _waterfall(results):
    """Where the size is going, name by name, base high vs adjusted high."""
    adjusted = [r for r in results if r.base_high is not None and r.adjusted_high is not None]
    adjusted.sort(key=lambda r: -(r.base_high - r.adjusted_high))
    adjusted = [r for r in adjusted if r.base_high - r.adjusted_high > 0][:15]
    if not adjusted:
        return None
    figure = go.Figure()
    figure.add_bar(name="adjusted", x=[r.ticker for r in adjusted],
                   y=[r.adjusted_high for r in adjusted], marker_color="#1a7f37")
    figure.add_bar(name="cut", x=[r.ticker for r in adjusted],
                   y=[r.base_high - r.adjusted_high for r in adjusted],
                   marker_color="#cf222e",
                   customdata=[r.correlated_with or "conviction" for r in adjusted],
                   hovertemplate="%{x}<br>cut %{y:.2f} pts<br>driven by %{customdata}"
                                 "<extra></extra>")
    figure.update_layout(barmode="stack", height=340,
                         margin=dict(l=10, r=10, t=40, b=10),
                         title="Top of the base range: what survives, what is cut",
                         yaxis_title="% of account",
                         legend=dict(orientation="h", y=1.12, x=0))
    return figure


def _rollup(selection, scan):
    st.markdown("#### Selection rollup")
    totals = sz.rollup(selection, scan)

    columns = st.columns(4)
    columns[0].metric("Names", totals["count"])
    columns[1].metric("Total allocation",
                      f"{totals['low']:.1f}–{totals['high']:.1f}%",
                      help="Sum of the adjusted ranges. Percentages of the account, "
                           "so currency-neutral.")
    columns[2].metric("Before adjustment",
                      f"{totals['base_low']:.1f}–{totals['base_high']:.1f}%")
    columns[3].metric("Given up", f"{totals['points_given_up']:.2f} pts",
                      help="Percentage points of account removed from the top of the "
                           "combined range by the correlation and conviction modifiers.")

    if totals["high"] > 100:
        st.error(f"The selection's adjusted range tops out at {totals['high']:.1f}% "
                 f"of the account — more than the whole account.")

    if totals["mixed_currency"]:
        parts = [f"{cur} ({', '.join(sorted(names))})"
                 for cur, names in sorted(totals["currencies"].items())]
        st.warning(
            "**Mixed quote currencies in this selection:** " + "; ".join(parts) +
            ". The percentages above are shares of the account and add up honestly. "
            "Any dollar figure across these names would need conversion first, so "
            "none is shown.")

    left, right = st.columns(2)

    left.markdown("**Sector concentration**")
    sectors = totals["by_sector"]
    if sectors:
        frame = pd.DataFrame({"Sector": list(sectors), "% of account": list(sectors.values())})
        frame["Share of selection"] = frame["% of account"] / max(1e-9, totals["high"])
        left.dataframe(
            frame.style.format({"% of account": "{:.2f}",
                                "Share of selection": "{:.0%}"}),
            width="stretch", hide_index=True)
        top_sector, top_value = next(iter(sectors.items()))
        if totals["high"] > 0 and top_value / totals["high"] >= 0.5:
            left.caption(f"⚠ {top_sector} is {top_value / totals['high']:.0%} of the "
                         f"selection's allocation.")
    else:
        left.caption("No sized positions in the selection.")

    right.markdown("**Holdings driving the reduction**")
    holdings = totals["by_holding"]
    if holdings:
        frame = pd.DataFrame([
            {"Holding": holding,
             "Points removed": info["points"],
             "Names affected": len(info["names"]),
             "Which": ", ".join(sorted(info["names"]))}
            for holding, info in holdings.items()])
        right.dataframe(frame.style.format({"Points removed": "{:.2f}"}),
                        width="stretch", hide_index=True)
    else:
        right.caption("No candidate in the selection is correlation-adjusted at these "
                      "settings.")


def render(scan):
    st.subheader("Position sizing simulator")

    if not PIPELINE.ok:
        st.error("**The sizing simulator needs position_sizer.py.**\n\n"
                 "It imports sizing_scale() and apply_reduction() rather than "
                 "re-deriving them, so that what you see here cannot drift from what "
                 "the pipeline writes.")
        st.code(PIPELINE.error or "unknown import failure")
        return

    candidates = scan.candidates
    if not candidates:
        st.info("No candidates in sized_candidates.json to size.")
        return

    controls = _controls(scan.params)

    document = scan.sized.document or {}
    if document.get("note"):
        st.warning(f"{document['note']} — the correlation controls have nothing to "
                   f"act on until holdings.json has real positions in it.")

    include_held = st.checkbox(
        "Include names already held", value=False,
        help="position_sizer.py does not size a held name as a new position, so these "
             "pass through unadjusted. Shown for completeness.")

    results = sz.resize_all(candidates, controls, include_held=include_held)
    if not results:
        st.info("Every candidate in this scan is already held.")
        return

    adjusted = [r for r in results if r.correlation_adjusted]
    summary = st.columns(4)
    summary[0].metric("Candidates sized", len(results))
    summary[1].metric("Correlation-reduced", len(adjusted))
    summary[2].metric("Mode", controls.reduction_mode)
    summary[3].metric("Basis", controls.basis)

    stored = sz.controls_from_params(scan.params)
    if (abs(stored.threshold - controls.threshold) > 1e-9
            or abs(stored.reduction_factor - controls.reduction_factor) > 1e-9
            or stored.basis != controls.basis or stored.flat != controls.flat):
        st.caption(f"↺ Simulating. The scan itself was sized at threshold "
                   f"{stored.threshold:.2f}, reduction {stored.reduction_factor:.2f}, "
                   f"{stored.basis} basis, {stored.reduction_mode}. Nothing on disk has "
                   f"changed — sized_candidates.json is read-only here.")

    frame = _frame(results)
    st.dataframe(
        frame.style
        .map(d.score_css, subset=["Composite"])
        .format({"Composite": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}",
                 "Base %": "{:.2f}", "Adj %": "{:.2f}", "Cut %": "{:.1f}",
                 "Conv ×": "{:.2f}",
                 "Max corr": lambda v: "n/a" if pd.isna(v) else f"{v:.3f}"}),
        width="stretch", hide_index=True,
        column_config={
            "Base %": st.column_config.Column(
                "Base %", help="Top of the base guide range, % of account"),
            "Adj %": st.column_config.Column(
                "Adj %", help="Top of the adjusted range at the current settings"),
            "Cut %": st.column_config.Column(
                help="Share of the base size removed by both modifiers combined"),
            "Driven by": st.column_config.Column(
                help="The holding whose correlation is driving the cut"),
            "Conv ×": st.column_config.Column(
                help="The conviction scale, from the sentiment stage. Independent of "
                     "the sliders."),
        })

    chart = _waterfall(results)
    if chart is not None:
        st.plotly_chart(chart, width="stretch")

    st.divider()
    st.markdown("### Considering a few of these together")
    tickers = list(frame["Ticker"])
    chosen = st.multiselect(
        "Pick the candidates you are actually weighing", tickers, default=[],
        placeholder="Select a handful of names to see the combined allocation")
    if chosen:
        selection = [r for r in results if r.ticker in set(chosen)]
        _rollup(selection, scan)
    else:
        st.caption("Nothing selected yet.")

    with st.expander("Integrity check — does this reproduce the pipeline's own numbers?"):
        result = sz.verify(candidates, scan.params)
        if not result.get("ran"):
            st.warning(result.get("reason"))
        elif result["ok"]:
            skipped = result.get("skipped") or 0
            tail = (f" {skipped} candidate(s) had no percentage range to check "
                    f"(\"Avoid; research only.\")." if skipped else "")
            st.success(
                f"Yes. Re-sized {result['checked']} candidate(s) at the scan's own "
                f"parameters and got sized_candidates.json's adjusted ranges back, to "
                f"the two decimals the file stores.{tail}")
        else:
            st.error(f"Re-sizing at the scan's own parameters did not reproduce the "
                     f"file for {len(result['mismatches'])} of {result['checked']} "
                     f"candidate(s). The dashboard's arithmetic and position_sizer.py "
                     f"have diverged — trust the file, not this view.")
            st.dataframe(pd.DataFrame(result["mismatches"]), width="stretch",
                         hide_index=True)
        st.caption(
            "The simulator recomputes scale as correlation_scale × conviction_scale, "
            "which is what position_sizer.size_candidate() does. This check re-runs "
            "every candidate at the parameters the file was written with and compares "
            "against the file.")

"""Overview - one row per candidate, sortable and filterable."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import display as d


def cluster_role(candidate: dict) -> str:
    """Where this name sits in its cluster, in one sortable word."""
    cluster = candidate.get("cluster")
    if not cluster:
        return "standalone"
    if cluster.get("resolution") == "pick_winner":
        return "winner" if cluster.get("is_winner") else "demoted"
    return "member"


def cluster_resolution(candidate: dict) -> str:
    cluster = candidate.get("cluster")
    if not cluster:
        return "standalone"
    return cluster.get("resolution") or "standalone"


def build_frame(candidates, sentiment) -> pd.DataFrame:
    rows = []
    for candidate in candidates:
        ticker = candidate.get("ticker")
        scores = (sentiment or {}).get(ticker) or {}
        sizing = candidate.get("sizing") or {}
        rows.append({
            "Held": bool(candidate.get("already_held")),
            "Ticker": ticker,
            "Name": candidate.get("name"),
            "Sector": candidate.get("sector") or "Unknown",
            "Composite": d.num(candidate.get("composite")),
            "Rating": candidate.get("rating"),
            "Cluster": cluster_resolution(candidate),
            "Role": cluster_role(candidate),
            "Liquidity": d.liquidity_label(candidate),
            "Trend": d.trend_label(candidate),
            "Divergence": d.DIVERGENCE_LABELS.get(
                candidate.get("divergence_pattern"),
                candidate.get("divergence_pattern") or "n/a"),
            "Conviction": d.conviction_label(candidate),
            "Sentiment": d.num(scores.get("overall")),
            "Guide": d.guide_headline(sizing.get("adjusted_guide")),
            "Cur": candidate.get("quote_currency"),
        })
    return pd.DataFrame(rows)


def render(scan):
    st.subheader("Overview")
    candidates = scan.candidates
    if not candidates:
        st.info("sized_candidates.json holds no candidates. "
                "That can be a legitimate market condition rather than an error - "
                "scan_report.py says the same thing when nothing clears the bar.")
        return

    frame = build_frame(candidates, scan.sentiment_scores)

    # ── filters ───────────────────────────────────────────────────────────
    row1 = st.columns([2, 1, 1, 1])
    sectors = sorted(s for s in frame["Sector"].dropna().unique())
    chosen_sectors = row1[0].multiselect("Sector", sectors, default=[],
                                         placeholder="All sectors")

    composites = frame["Composite"].dropna()
    if len(composites):
        low, high = float(composites.min()), float(composites.max())
    else:
        low, high = 0.0, 10.0
    # A slider whose ends are equal is invalid, and one candidate (or several
    # on the same score) is a real case.
    if high - low < 0.01:
        low, high = max(0.0, low - 0.5), min(10.0, high + 0.5)
    min_composite = row1[1].slider("Min composite", min_value=round(low, 2),
                                   max_value=round(high, 2), value=round(low, 2),
                                   step=0.1)

    resolutions = ["All"] + sorted(frame["Cluster"].unique())
    chosen_resolution = row1[2].selectbox("Cluster resolution", resolutions)
    held_choice = row1[3].selectbox("Holdings", ["All", "Not held", "Held only"])

    view = frame.copy()
    if chosen_sectors:
        view = view[view["Sector"].isin(chosen_sectors)]
    view = view[view["Composite"].isna() | (view["Composite"] >= min_composite)]
    if chosen_resolution != "All":
        view = view[view["Cluster"] == chosen_resolution]
    if held_choice == "Not held":
        view = view[~view["Held"]]
    elif held_choice == "Held only":
        view = view[view["Held"]]

    view = view.sort_values("Composite", ascending=False, na_position="last")

    st.caption(f"{len(view)} of {len(frame)} candidates  ·  "
               f"click any column header to sort  ·  "
               f"composite is coloured on scan_report.py's thresholds "
               f"(green ≥ {d.GOOD}, yellow ≥ {d.FAIR}, red below)")

    styled = (view.style
              .map(d.score_css, subset=["Composite", "Sentiment"])
              .format({"Composite": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}",
                       "Sentiment": lambda v: "n/a" if pd.isna(v) else f"{v:.1f}"}))

    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        column_config={
            "Held": st.column_config.CheckboxColumn(
                "Held", help="Already in holdings.json - not sized as a new position"),
            "Composite": st.column_config.Column(help="0-10, stock_evaluator.py"),
            "Sentiment": st.column_config.Column(
                help="0-10 public sentiment from sentiment.json, centred on 5.0"),
            "Conviction": st.column_config.Column(help=d.CONVICTION_HELP),
            "Trend": st.column_config.Column(
                help="Multi-year ROA consistency. '·' means too little history to say, "
                     "which is not the same as a failed trend."),
            "Guide": st.column_config.Column(
                help="The adjusted guide as position_sizer.py wrote it. The sizing "
                     "simulator recomputes this live."),
            "Cur": st.column_config.Column(
                "Cur", help="Quote currency. Mixed currencies are never summed."),
        },
    )

    currencies = sorted({c for c in view["Cur"].dropna().unique()})
    if len(currencies) > 1:
        st.caption(f"⚠ Mixed quote currencies in this view ({', '.join(currencies)}). "
                   f"Position guides are percentages of the account and so are "
                   f"comparable; any dollar figure across these names would not be "
                   f"without conversion.")

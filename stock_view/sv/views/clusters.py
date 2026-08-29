"""Cluster explorer - clustered.json, which is richer than the slim per-candidate view.

sized_candidates.json carries a trimmed `cluster` object per candidate. The
eigenvalue, the share of variance the mode explains, the score spread and the
resolution note all stay behind in clustered.json, and they are the part that
says WHY a cluster resolved the way it did.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import display as d


def _correlation_panel(members, candidates_by_ticker, basis):
    """Members x holdings correlations, from what the pipeline stores.

    Worth being exact about what this can and cannot show. position_sizer.py
    builds one matrix over candidates AND holdings together, but it only writes
    out the candidate-to-holding column of it - `sizing.correlations` is keyed
    by holding. The candidate-to-candidate block, which a members-by-members
    heatmap would need, is never persisted; clustered.json keeps only the
    cluster's average pairwise correlation as a single number.

    So this is the real pairwise structure that exists on disk: how each member
    of the cluster relates to each position already owned. It answers the
    question the sizing simulator acts on - which holding is driving the cut,
    and whether the whole cluster is driven by the same one.
    """
    holdings = []
    for ticker in members:
        pairs = ((candidates_by_ticker.get(ticker) or {}).get("sizing") or {}).get(
            "correlations") or {}
        for holding in pairs:
            if holding not in holdings:
                holdings.append(holding)
    if not holdings:
        return None, None

    holdings.sort()
    rows, index = [], []
    for ticker in members:
        pairs = ((candidates_by_ticker.get(ticker) or {}).get("sizing") or {}).get(
            "correlations") or {}
        if not pairs:
            continue
        index.append(ticker)
        rows.append([d.num((pairs.get(h) or {}).get(basis)) for h in holdings])
    if not rows:
        return None, None
    return pd.DataFrame(rows, index=index, columns=holdings), holdings


def _heatmap(frame, title):
    figure = go.Figure(data=go.Heatmap(
        z=frame.values,
        x=list(frame.columns),
        y=list(frame.index),
        zmin=-1, zmax=1,
        colorscale="RdBu_r",
        colorbar=dict(title="corr"),
        hovertemplate="%{y} vs %{x}<br>correlation %{z:.3f}<extra></extra>",
        texttemplate="%{z:.2f}",
        textfont={"size": 11},
    ))
    figure.update_layout(
        title=title,
        height=max(240, 42 * len(frame.index) + 130),
        margin=dict(l=10, r=10, t=50, b=10),
        xaxis_title="holding",
        yaxis_title="cluster member",
    )
    return figure


def _cluster_title(index, cluster):
    members = cluster.get("members") or []
    corr = d.num(cluster.get("avg_correlation"))
    resolution = cluster.get("resolution") or "unresolved"
    corr_text = f"avg corr {corr:.2f}" if corr is not None else "avg corr n/a"
    winner = cluster.get("winner")
    tail = f"  ·  winner {winner}" if winner else ""
    return (f"Cluster {index + 1}  ·  {len(members)} names  ·  {corr_text}  ·  "
            f"{resolution}{tail}")


def render(scan):
    st.subheader("Cluster explorer")

    if not scan.clustered.exists:
        st.warning(f"No clustered.json in {scan.directory}. "
                   f"Run scan_report.py to build it.")
        return

    document = scan.clustered.document or {}
    if document.get("insufficient_data_for_clustering"):
        st.warning(document.get("note") or "Clustering did not run for this scan.")

    clusters = scan.clusters
    params = document.get("params") or {}
    candidates_by_ticker = {c["ticker"]: c for c in scan.candidates}

    top = st.columns(4)
    top[0].metric("Clusters", len(clusters))
    top[1].metric("Standalone", len(scan.standalone))
    matrix = params.get("assets_in_matrix")
    top[2].metric("Names in matrix", matrix if matrix is not None else "n/a")
    bets = params.get("effective_independent_bets")
    top[3].metric("Effective bets", f"{bets:.1f}" if isinstance(bets, (int, float))
                  else "n/a")

    if params:
        bits = []
        for label, key, fmt in (("T/N", "q_ratio", "{:.2f}"),
                                ("MP edge", "mp_threshold", "{:.3f}"),
                                ("noise σ²", "noise_sigma2", "{:.3f}"),
                                ("signal modes", "signals_found", "{}"),
                                ("lookback", "lookback_years", "{:.2f}y")):
            value = params.get(key)
            if value is not None:
                try:
                    bits.append(f"{label} {fmt.format(value)}")
                except (TypeError, ValueError):
                    bits.append(f"{label} {value}")
        if bits:
            st.caption("  ·  ".join(bits))
    if params.get("warning"):
        st.warning(params["warning"])

    dropped = document.get("dropped_duplicates") or []
    if dropped:
        with st.expander(f"{len(dropped)} duplicate listing(s) dropped before correlating"):
            st.caption("A dual-class or cross-listed pair correlates at ~1.0 and would "
                       "look exactly like a real cluster.")
            st.dataframe(pd.DataFrame(dropped), width="stretch", hide_index=True)

    if not clusters:
        st.info("No genuine correlation clusters in this scan - every name is standalone.")
    else:
        basis = st.radio("Correlation basis for the heatmaps", ["raw", "cleaned"],
                         horizontal=True, key="cluster_basis",
                         help="raw is the sample correlation; cleaned is the same matrix "
                              "put through the Marchenko-Pastur filter.")

    for index, cluster in enumerate(clusters):
        members = cluster.get("members") or []
        with st.expander(_cluster_title(index, cluster),
                         expanded=(index == 0 and len(clusters) <= 3)):
            left, right = st.columns([3, 2])

            scores = cluster.get("scores") or {}
            rows = []
            for ticker in members:
                candidate = candidates_by_ticker.get(ticker) or {}
                role = "—"
                if cluster.get("winner") == ticker:
                    role = "winner"
                elif ticker in (cluster.get("demoted_peers") or []):
                    role = "demoted"
                rows.append({
                    "Ticker": ticker,
                    "Role": role,
                    "Score": d.num(scores.get(ticker)),
                    "Name": candidate.get("name"),
                    "Sector": candidate.get("sector"),
                    "Held": bool(candidate.get("already_held")),
                })
            member_frame = pd.DataFrame(rows).sort_values(
                "Score", ascending=False, na_position="last")
            left.dataframe(
                member_frame.style
                .map(d.score_css, subset=["Score"])
                .format({"Score": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}"}),
                width="stretch", hide_index=True)

            facts = {
                "avg correlation": d.ratio(cluster.get("avg_correlation")),
                "dispersion (stdev)": d.ratio(cluster.get("dispersion")),
                "score range": d.ratio(cluster.get("score_range")),
                "winner gap": d.ratio(cluster.get("winner_gap")),
                "winner lead": d.ratio(cluster.get("winner_lead")),
                "eigenvalue": d.ratio(cluster.get("eigenvalue")),
                "% of variance": d.pct(cluster.get("pct_variance"), 2),
                "mode": f"{cluster.get('mode_rank')} ({cluster.get('mode_side')})",
            }
            right.dataframe(
                pd.DataFrame({"value": facts}).rename_axis("metric").reset_index(),
                width="stretch", hide_index=True)

            if cluster.get("resolution_note"):
                st.caption(f"**{cluster.get('resolution')}** — {cluster['resolution_note']}")

            frame, _ = _correlation_panel(members, candidates_by_ticker, basis)
            if frame is None:
                st.caption(
                    "No correlation heatmap: the pipeline only stores candidate-to-holding "
                    "correlations, and no member of this cluster has any on file "
                    "(no holdings, or these names missed the correlation panel).")
            else:
                st.plotly_chart(
                    _heatmap(frame, f"{basis} correlation — cluster members × holdings"),
                    width="stretch", key=f"heat_{index}")
                st.caption(
                    "Member-to-member correlations are not written to disk — "
                    "position_sizer.py persists only each candidate's correlation to "
                    "each holding, and clustered.json keeps the within-cluster figure "
                    "as the single average above. This shows the pairwise structure "
                    "that does exist: how the cluster relates to what is already owned.")

    if scan.standalone:
        with st.expander(f"Standalone — {len(scan.standalone)} name(s)"):
            notes = scan.standalone_notes
            rows = [{"Ticker": t,
                     "Name": (candidates_by_ticker.get(t) or {}).get("name"),
                     "Composite": d.num((candidates_by_ticker.get(t) or {}).get("composite")),
                     "Why standalone": notes.get(t, "—")}
                    for t in scan.standalone]
            frame = pd.DataFrame(rows).sort_values("Composite", ascending=False,
                                                   na_position="last")
            st.dataframe(
                frame.style
                .map(d.score_css, subset=["Composite"])
                .format({"Composite": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}"}),
                width="stretch", hide_index=True)

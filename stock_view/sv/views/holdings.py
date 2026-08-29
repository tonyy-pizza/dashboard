"""Holdings - holdings.json as a table, plus the exit review from the scan.

Read-only in v1. holdings.json is the single canonical file the rest of the
pipeline reads, so nothing here writes to it and no second copy is kept
anywhere.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data_loader as dl
from .. import display as d

VERDICT_COLOURS = {
    "exit_review": d.RED,
    "watch": d.YELLOW,
    "hold": d.GREEN,
    "unavailable": d.DIM,
}


def _verdict_css(value):
    return f"color: {VERDICT_COLOURS.get(value, d.DIM)}"


def _cost_basis_by_currency(rows, scan):
    """Cost basis totalled per currency, never across them.

    holdings.json records no currency — just ticker, shares and cost_basis in
    "the listing's own currency". A single total over a mix of TSX and US names
    would be adding CAD to USD, so the currency is inferred per name (from the
    scan's own quote_currency where it knows it) and each one is totalled
    separately.
    """
    totals = {}
    for row in rows:
        shares, cost = row.get("shares"), row.get("cost_basis")
        currency = dl.infer_currency(row["ticker"], scan) or "unknown"
        entry = totals.setdefault(currency, {"value": 0.0, "names": [],
                                             "incomplete": []})
        entry["names"].append(row["ticker"])
        if shares is None or cost is None:
            entry["incomplete"].append(row["ticker"])
        else:
            entry["value"] += shares * cost
    return totals


def render(scan):
    st.subheader("Holdings")

    loaded = scan.holdings
    if not loaded.exists:
        st.warning(
            f"No holdings.json in {scan.directory}.\n\n"
            f"position_sizer.py writes a template there on its first run. Until it "
            f"has real positions in it, sizing is not correlation-adjusted at all — "
            f"which is the honest answer rather than assuming a correlation of zero.")
        if loaded.error and loaded.error != "not found":
            st.caption(f"({loaded.error})")
        return

    kept, ignored = dl.holding_entries(loaded.document)
    comment = (loaded.document or {}).get("_comment")

    st.caption(f"`{loaded.path}`  ·  {loaded.age_text}  ·  read-only")

    if not kept:
        st.info(
            "holdings.json has no real positions in it — every entry is the example "
            "the template ships with, and the pipeline ignores those. Edit the file "
            "with your own positions and re-run scan_report.py to get "
            "correlation-adjusted sizing.")
        if comment:
            st.caption(comment)
        if ignored:
            st.dataframe(pd.DataFrame(ignored), width="stretch", hide_index=True)
        return

    rows = []
    for row in kept:
        ticker = row["ticker"]
        currency = dl.infer_currency(ticker, scan)
        shares, cost = row.get("shares"), row.get("cost_basis")
        candidate = next((c for c in scan.candidates if c.get("ticker") == ticker), None)
        rows.append({
            "Ticker": ticker,
            "Name": (candidate or {}).get("name"),
            "Sector": (candidate or {}).get("sector"),
            "Shares": shares,
            "Cost basis": cost,
            "Book value": None if (shares is None or cost is None) else shares * cost,
            "Cur": currency or "?",
            "In this scan": candidate is not None,
        })
    frame = pd.DataFrame(rows)
    st.dataframe(
        frame.style.format({"Shares": lambda v: "n/a" if pd.isna(v) else f"{v:g}",
                            "Cost basis": lambda v: "n/a" if pd.isna(v) else f"{v:,.2f}",
                            "Book value": lambda v: "n/a" if pd.isna(v) else f"{v:,.2f}"}),
        width="stretch", hide_index=True,
        column_config={
            "Cur": st.column_config.Column(
                "Cur", help="Inferred: holdings.json records no currency. Taken from "
                            "the scan's quote_currency where it knows the name, else "
                            "from the Yahoo suffix. '?' means neither applied."),
            "Book value": st.column_config.Column(
                help="shares × cost basis, in that listing's own currency"),
            "In this scan": st.column_config.CheckboxColumn(
                help="Whether this scan scored the holding, which is what the exit "
                     "review below needs"),
        })

    totals = _cost_basis_by_currency(kept, scan)
    st.markdown("**Book value at cost**")
    columns = st.columns(max(1, len(totals)))
    for column, (currency, entry) in zip(columns, sorted(totals.items())):
        suffix = ""
        if entry["incomplete"]:
            suffix = f" (excludes {', '.join(entry['incomplete'])} — missing shares/cost)"
        column.metric(f"{currency}", f"{entry['value']:,.2f}",
                      help=f"{', '.join(sorted(entry['names']))}{suffix}")
    if len(totals) > 1:
        st.warning(
            "**Mixed currencies — these are not added together.** " +
            "; ".join(f"{cur}: {', '.join(sorted(e['names']))}"
                      for cur, e in sorted(totals.items())) +
            ". A single portfolio total would need an FX rate, and the pipeline does "
            "not carry one.")
    if "unknown" in totals or "?" in totals:
        st.caption("'unknown' currency: the name is not in this scan and its suffix "
                   "does not identify a Canadian listing, so its currency was not "
                   "inferred.")

    if ignored:
        with st.expander(f"{len(ignored)} entry/entries the pipeline ignores"):
            st.caption("Entries marked _example, or without a ticker, are skipped "
                       "everywhere in the pipeline. stock_view skips them too.")
            st.dataframe(pd.DataFrame(ignored), width="stretch", hide_index=True)

    # ── exit review ───────────────────────────────────────────────────────
    reviews = scan.holdings_review
    st.divider()
    st.markdown("### Exit review")
    if not reviews:
        st.caption(
            "No exit review in this scan. position_sizer.py re-scores every holding "
            "through Stage 1 on each run unless it was given --no-exit-review.")
        return

    st.caption("position_sizer.py re-scores each holding through the same Stage 1 "
               "scoring a candidate gets, so a thesis that has quietly stopped being "
               "true shows up beside the new ideas.")
    order = {"exit_review": 0, "watch": 1, "unavailable": 2, "hold": 3}
    rows = []
    for review in sorted(reviews, key=lambda r: order.get(r.get("verdict"), 9)):
        reasons = review.get("reasons") or []
        rows.append({
            "Ticker": review.get("ticker"),
            "Verdict": review.get("verdict"),
            "Composite": d.num(review.get("composite")),
            "Δ since last scan": d.num(review.get("composite_delta")),
            "Rating": review.get("rating"),
            "Debt trend": review.get("debt_trend"),
            "Why": reasons[0] if reasons else "",
        })
    review_frame = pd.DataFrame(rows)
    st.dataframe(
        review_frame.style
        .map(d.score_css, subset=["Composite"])
        .map(_verdict_css, subset=["Verdict"])
        .format({"Composite": lambda v: "n/a" if pd.isna(v) else f"{v:.2f}",
                 "Δ since last scan": lambda v: "—" if pd.isna(v) else f"{v:+.2f}"}),
        width="stretch", hide_index=True)

    flagged = [r for r in reviews if r.get("verdict") == "exit_review"]
    if flagged:
        st.error(f"{len(flagged)} holding(s) flagged for exit review: "
                 + ", ".join(r["ticker"] for r in flagged))

    st.divider()
    st.caption(
        "**Read-only.** holdings.json is the one canonical file the rest of the "
        "pipeline reads, so stock_view never writes to it — edit it directly and "
        "re-run scan_report.py, then press Reload in the sidebar.")

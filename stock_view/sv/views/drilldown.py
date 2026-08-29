"""Ticker drill-down - the full per-name blob behind a composite score.

Everything on this page is read from JSON already on disk, with one exception
that is deliberately behind a button: the price sparkline calls
market_data.get_price_history(), which is the only place the dashboard touches
the network/cache layer at all.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import display as d
from ..pipeline import PIPELINE

# metrics keys worth surfacing, grouped the way stock_evaluator's own report
# groups them. (key, label, formatter)
METRIC_GROUPS = {
    "Valuation": [
        ("pe", "P/E", "ratio"), ("fwd_pe", "Forward P/E", "ratio"),
        ("peg", "PEG", "ratio"), ("ps", "P/S", "ratio"), ("pb", "P/B", "ratio"),
        ("ev_ebitda", "EV/EBITDA", "ratio"),
    ],
    "Profitability": [
        ("gross_margin", "Gross margin", "share"), ("op_margin", "Operating margin", "share"),
        ("net_margin", "Net margin", "share"), ("roe", "ROE", "share"),
        ("roa", "ROA", "share"), ("roic", "ROIC", "share"),
    ],
    "Growth": [
        ("rev_growth_raw", "Revenue growth", "share"),
        ("eps_growth_raw", "EPS growth", "share"),
        ("fcf_growth_raw", "FCF growth", "share"),
        ("ni_growth", "Net income growth", "share"),
        ("rnd_intensity", "R&D / revenue", "share"),
    ],
    "Health": [
        ("debt_eq", "Debt / equity", "ratio"), ("curr_ratio", "Current ratio", "ratio"),
        ("quick_ratio", "Quick ratio", "ratio"),
        ("int_coverage", "Interest coverage", "ratio"),
        ("fcf_quality", "FCF / net income", "ratio"),
        ("runway_months", "Cash runway (months)", "ratio"),
    ],
    "Market": [
        ("price", "Price", "ratio"), ("mktcap", "Market cap", "money"),
        ("beta", "Beta", "ratio"), ("52w_high", "52-week high", "ratio"),
        ("52w_low", "52-week low", "ratio"),
        ("pos_52w", "Position in 52-week range", "share"),
        ("avg_volume", "Average volume", "money"),
    ],
}


def _format(value, kind):
    if kind == "share":
        value = d.num(value)
        return "n/a" if value is None else f"{value * 100:.2f}%"
    if kind == "money":
        return d.money(value)
    return d.ratio(value)


def _blob(candidate, scan):
    """metrics/frameworks from wherever they survive.

    A non-slim sized_candidates.json carries them; a slim one needs
    scored_candidates.json, which may be missing or from a different run.
    """
    if candidate.get("metrics"):
        return candidate, "sized_candidates.json"
    record = scan.scored_record(candidate.get("ticker"))
    if record:
        return record, "scored_candidates.json"
    return None, None


def _frameworks(frameworks):
    piotroski = frameworks.get("piotroski")
    if piotroski:
        st.markdown(f"**Piotroski** — {piotroski.get('score')}/{piotroski.get('max', 9)}"
                    f"  ·  {piotroski.get('label')}")
        signals = piotroski.get("signals") or {}
        passed = [k for k, v in signals.items() if v]
        failed = [k for k, v in signals.items() if not v]
        if passed:
            st.caption("✓ " + " | ".join(passed))
        if failed:
            st.caption("✗ " + " | ".join(failed))

    altman = frameworks.get("altman_z")
    if altman:
        zone = altman.get("zone")
        colour = {"Safe": d.GREEN, "Grey": d.YELLOW}.get(zone, d.RED)
        st.markdown(f"**Altman Z** — {altman.get('score')}  "
                    f"<span style='color:{colour}'>[{zone}]</span>",
                    unsafe_allow_html=True)
        if altman.get("warning"):
            st.caption(f"⚠ {altman['warning']}")

    graham = frameworks.get("graham")
    if graham:
        mos = d.num(graham.get("mos"))
        arrow = "↑" if (mos or 0) > 0 else "↓"
        st.markdown(f"**Graham number** — ${graham.get('graham')}  {arrow}  "
                    f"{d.pct(mos)} margin of safety  (price ${graham.get('price')})")

    magic = frameworks.get("magic_formula")
    if magic:
        st.markdown(f"**Magic Formula** — earnings yield {d.pct(magic.get('ey'), 2)}  ·  "
                    f"ROIC {d.pct(magic.get('roic'), 2)}  ·  "
                    f"combined {d.ratio(magic.get('combined'))}")
        if magic.get("warning"):
            st.caption(f"⚠ {magic['warning']}")

    dcf = frameworks.get("dcf")
    if dcf:
        st.markdown("**DCF scenarios**")
        rows = []
        for name in ("bear", "base", "bull"):
            scenario = dcf.get(name)
            if not scenario:
                continue
            rows.append({
                "Scenario": name,
                "Intrinsic value": scenario.get("iv"),
                "Upside %": scenario.get("upside"),
                "Growth %": scenario.get("growth"),
                "Terminal %": scenario.get("terminal_growth"),
                "Discount %": scenario.get("discount_rate"),
                "FCF adj": scenario.get("fcf_adjustment"),
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _sparkline(ticker):
    """The one optional network call in the dashboard, behind a button."""
    period = st.session_state.get("dd_period", "1y")
    with st.spinner(f"market_data.get_price_history({ticker!r}, period={period!r})…"):
        try:
            rows = PIPELINE.md.get_price_history(ticker, period=period)
        except Exception as exc:                       # noqa: BLE001
            st.error(f"Price history failed: {type(exc).__name__}: {exc}")
            return
    if not rows:
        st.warning("market_data returned no price history. It never raises on a network "
                   "failure — it returns None, which is what happened here.")
        return
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], format="mixed", utc=True)
    figure = go.Figure(go.Scatter(x=frame["date"], y=frame["close"], mode="lines",
                                  line=dict(width=1.6, color="#1f6feb"),
                                  hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f}<extra></extra>"))
    figure.update_layout(height=240, margin=dict(l=10, r=10, t=30, b=10),
                         title=f"{ticker} — {period} close",
                         yaxis_title="close")
    st.plotly_chart(figure, width="stretch")
    st.caption(f"{len(frame)} bars, cached by market_data.py under its price TTL "
               f"(1 day). This is the only network call stock_view makes.")


def render(scan):
    st.subheader("Ticker drill-down")

    candidates = scan.candidates
    if not candidates:
        st.info("No candidates in sized_candidates.json.")
        return

    by_ticker = {c["ticker"]: c for c in candidates}
    tickers = sorted(by_ticker)
    ticker = st.selectbox("Ticker", tickers, key="dd_ticker")
    candidate = by_ticker[ticker]

    header = st.columns([3, 1, 1, 1])
    header[0].markdown(f"### {candidate.get('name') or ticker}  ·  `{ticker}`")
    header[0].caption(f"{candidate.get('sector') or 'Unknown sector'}  ·  "
                      f"quoted in {candidate.get('quote_currency') or 'n/a'}"
                      + ("  ·  **already held**" if candidate.get("already_held") else ""))
    composite = d.num(candidate.get("composite"))
    header[1].metric("Composite", "n/a" if composite is None else f"{composite:.2f}")
    header[2].metric("Rating", candidate.get("rating") or "n/a")
    scores = scan.sentiment_scores.get(ticker) or {}
    sentiment = d.num(scores.get("overall"))
    header[3].metric("Sentiment", "n/a" if sentiment is None else f"{sentiment:.1f}",
                     help=scores.get("interpretation") or "from sentiment.json")

    dims = candidate.get("dims") or {}
    if dims:
        st.markdown("**Dimension breakdown**")
        for dim, value in dims.items():
            value = d.num(value)
            st.markdown(
                f"<code>{dim:<14}</code> <code>{d.bar(value, 18)}</code> "
                f"{d.score_html(value)}",
                unsafe_allow_html=True)

    sizing = candidate.get("sizing") or {}
    st.markdown("**Sizing as the pipeline wrote it**")
    box = st.columns(3)
    box[0].metric("Base guide", d.guide_headline(sizing.get("base_guide")))
    box[1].metric("Adjusted guide", d.guide_headline(sizing.get("adjusted_guide")))
    reduction = d.num(sizing.get("reduction")) or 0.0
    box[2].metric("Total cut", f"{reduction * 100:.0f}%")
    if sizing.get("note"):
        st.caption(sizing["note"])
    flags = sizing.get("risk_flags") or []
    if flags:
        st.caption("Risk flags: " + ", ".join(flags))

    conviction = sizing.get("conviction")
    if conviction and conviction != "not_applicable":
        label, colour, meaning = d.CONVICTION_LABELS.get(conviction, (conviction, d.DIM, ""))
        detail = sizing.get("conviction_detail") or {}
        st.markdown(f"**Conviction** — <span style='color:{colour}'>{label}</span> "
                    f"(×{d.ratio(sizing.get('conviction_scale'))}) — {meaning}",
                    unsafe_allow_html=True)
        if detail.get("reason"):
            st.caption(detail["reason"])

    pairs = sizing.get("correlations") or {}
    if pairs:
        with st.expander(f"Correlations to holdings ({len(pairs)})"):
            frame = pd.DataFrame([
                {"Holding": holding, "raw": d.num(values.get("raw")),
                 "cleaned": d.num(values.get("cleaned"))}
                for holding, values in pairs.items()
            ]).sort_values("raw", ascending=False)
            st.dataframe(frame.style.format({"raw": "{:.4f}", "cleaned": "{:.4f}"}),
                         width="stretch", hide_index=True)

    warnings = candidate.get("warnings") or []
    if warnings:
        st.markdown("**Warnings**")
        for warning in warnings:
            st.warning(warning, icon="⚠")

    st.divider()
    blob, source = _blob(candidate, scan)
    if blob is None:
        st.info(
            "No metrics/frameworks for this name. sized_candidates.json was written "
            "with --slim, and scored_candidates.json is "
            + ("missing from " + str(scan.directory) if not scan.scored.exists
               else "present but has no record for this ticker")
            + ". Re-run scan_report.py, or run position_sizer.py without --slim.")
        return

    st.caption(f"Full metrics from {source}"
               + (f" ({scan.scored.age_text})" if source == "scored_candidates.json" else ""))

    notes = blob.get("notes") or []
    if notes:
        for note in notes:
            st.caption(f"· {note}")

    metrics = blob.get("metrics") or {}
    tabs = st.tabs(list(METRIC_GROUPS) + ["Frameworks", "Value screen", "Insider",
                                          "Price history"])
    for tab, (group, fields) in zip(tabs, METRIC_GROUPS.items()):
        with tab:
            rows = [{"Metric": label, "Value": _format(metrics.get(key), kind)}
                    for key, label, kind in fields]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with tabs[len(METRIC_GROUPS)]:
        frameworks = blob.get("frameworks") or {}
        if frameworks:
            _frameworks(frameworks)
        else:
            st.caption("No frameworks block on file for this name.")

    with tabs[len(METRIC_GROUPS) + 1]:
        screen = blob.get("value_screen") or {}
        if screen.get("triggered"):
            st.info(screen.get("verdict") or "triggered")
        else:
            st.caption("Not near its 52-week low — the value screen did not trigger.")

    with tabs[len(METRIC_GROUPS) + 2]:
        insider = blob.get("insider")
        if insider:
            st.markdown(f"**{insider.get('verdict')}**")
            st.dataframe(pd.DataFrame([insider]), width="stretch", hide_index=True)
            st.caption("Informational only — insider data is noisy and is not folded "
                       "into the composite.")
        else:
            st.caption("No insider data on file. The batch run does not fetch it by "
                       "default.")

    with tabs[len(METRIC_GROUPS) + 3]:
        st.caption("Everything else in stock_view reads static JSON. This is the one "
                   "place it would call out to the network, so it only happens when "
                   "you press the button.")
        if not PIPELINE.ok:
            st.warning("market_data.py could not be imported, so the price history is "
                       "unavailable here.")
        else:
            columns = st.columns([1, 3])
            columns[0].selectbox("Period", ["6mo", "1y", "2y", "5y"], index=1,
                                 key="dd_period")
            if columns[1].button(f"Fetch {ticker} price history",
                                 key=f"fetch_{ticker}"):
                _sparkline(ticker)

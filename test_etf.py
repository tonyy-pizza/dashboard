#!/usr/bin/env python3
"""
Offline regression tests for etf.py.

Every check runs without network access: yfinance's data shapes are
reconstructed exactly as of 1.x, and price series are synthesised with
known analytic answers. Run with:

    python test_etf.py
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

import etf


FAILS: list[str] = []
CHECKS = [0]


def check(label: str, got, want, tol: float | None = None) -> None:
    CHECKS[0] += 1
    if tol is not None and got is not None and want is not None:
        ok = abs(float(got) - float(want)) <= tol
    else:
        ok = got == want

    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        FAILS.append(label)


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# ---------------------------------------------------------------------------
# Fixtures matching yfinance 1.x FundsData exactly
# ---------------------------------------------------------------------------

class FakeFundsData:
    """Mirrors yfinance/scrapers/funds.py output shapes."""

    def __init__(self, symbol: str = "SPY"):
        self._symbol = symbol

        self.top_holdings = pd.DataFrame(
            {
                "Symbol": ["NVDA", "MSFT", "AAPL", "AMZN", "META",
                           "AVGO", "GOOGL", "TSLA", "GOOG", "BRK.B"],
                "Name": ["NVIDIA Corp", "Microsoft Corp", "Apple Inc",
                         "Amazon.com Inc", "Meta Platforms", "Broadcom Inc",
                         "Alphabet Inc A", "Tesla Inc", "Alphabet Inc C",
                         "Berkshire Hathaway B"],
                "Holding Percent": [.0784, .0662, .0571, .0398, .0286,
                                    .0247, .0212, .0198, .0175, .0163],
            }
        ).set_index("Symbol")

        self.equity_holdings = pd.DataFrame(
            {
                "Average": ["Price/Earnings", "Price/Book", "Price/Sales",
                            "Price/Cashflow", "Median Market Cap",
                            "3 Year Earnings Growth"],
                symbol: [26.4, 4.9, 3.2, 18.1, 3.1e11, 0.19],
                "Category Average": [24.0, 4.1, 2.8, 16.0, 2.4e11, 0.16],
            }
        ).set_index("Average")

        self.bond_holdings = pd.DataFrame(
            {
                "Average": ["Duration", "Maturity", "Credit Quality"],
                symbol: [6.1, 8.4, float("nan")],
                "Category Average": [6.0, 8.2, float("nan")],
            }
        ).set_index("Average")

        self.fund_operations = pd.DataFrame(
            {
                "Attributes": ["Annual Report Expense Ratio",
                               "Annual Holdings Turnover", "Total Net Assets"],
                symbol: [0.0945, 2.0, 5.4e11],
                "Category Average": [0.7800, 45.0, 2.0e10],
            }
        ).set_index("Attributes")

        self.fund_overview = {
            "categoryName": "Large Blend",
            "family": "SPDR State Street Global Advisors",
            "legalType": "Unit Investment Trust",
        }

        self.bond_ratings = {"aaa": .72, "aa": .03, "a": .11, "bbb": .14,
                             "bb": .0, "b": .0, "below_b": .0,
                             "us_government": .0, "other": .0}

        self.sector_weightings = {
            "technology": .335, "financial_services": .135, "healthcare": .095,
            "consumer_cyclical": .104, "communication_services": .099,
            "industrials": .075, "consumer_defensive": .055, "energy": .031,
            "utilities": .024, "realestate": .021, "basic_materials": .018,
        }

        self.asset_classes = {
            "cashPosition": .004, "stockPosition": .996, "bondPosition": .0,
            "preferredPosition": .0, "convertiblePosition": .0,
            "otherPosition": .0,
        }


def make_prices(years: float = 5.0, cagr: float = 0.10,
                vol: float = 0.0, seed: int = 1) -> pd.DataFrame:
    """Business-day series ending today with a known compound growth rate."""
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=int(round(365.25 * years)))
    idx = pd.bdate_range(start=start, end=end)
    n = len(idx)
    rng = np.random.default_rng(seed)
    # Calibrate per-bar growth to the actual bar count so the compound
    # rate over the window is exactly `cagr`.
    mu = (1 + cagr) ** (years / (n - 1)) - 1
    r = np.full(n - 1, mu)
    if vol:
        r = r + rng.standard_normal(n - 1) * vol / math.sqrt(252)
    px = 100 * np.cumprod(np.r_[1.0, 1 + r])
    return pd.DataFrame({"Close": px, "Volume": np.full(n, 1_000_000.0)}, index=idx)


# ---------------------------------------------------------------------------
# 1. Holdings extraction against real yfinance shapes
# ---------------------------------------------------------------------------

section("1. Holdings extraction (yfinance 1.x DataFrame shapes)")

h = etf.extract_holdings(FakeFundsData("SPY"), symbol="SPY")

check("top holdings parsed", len(h["top_holdings"]), 10)
check("top holding symbol", h["top_holdings"][0]["symbol"], "NVDA")
check("top holding weight", h["top_holdings"][0]["pct"], 0.0784, tol=1e-9)
check("top-10 weight summed", h["top10_weight"], 0.3696, tol=1e-9)
check("equity aggregates read", h["equity_details"].get("P/E (aggregate)"), (26.4, 24.0))
check("bond aggregates read", h["bond_details"].get("Eff. Duration (yrs)"), (6.1, 6.0))
check("NaN credit quality skipped", "Avg Credit Quality" in h["bond_details"], False)
check("category expense ratio", h["category_expense_ratio"], 0.0078, tol=1e-9)
check("turnover normalized", h["turnover"], 0.020, tol=1e-9)
check("legal type read", h["legal_type"], "Unit Investment Trust")
check("sectors parsed", len(h["sectors"]), 11)
check("sector name aliased", h["sectors"][0]["sector"], "Technology")
check("realestate aliased", etf.fmt_sector("realestate"), "Real Estate")
check("asset mix read", h["equity_pct"], 0.996, tol=1e-9)
check("bond ratings read", len(h["bond_ratings"]), 9)

# Partial disclosure must not look less concentrated.
partial = FakeFundsData("XYZ")
partial.top_holdings = partial.top_holdings.iloc[:5]
hp = etf.extract_holdings(partial, symbol="XYZ")
check("5 disclosed -> no top-10 figure", hp["top10_weight"], None)
check("5 disclosed -> count recorded", hp["holdings_reported"], 5)

m = {"etf_type": "equity_broad", "top1_pct": h["top_holdings"][0]["pct"],
     "top10_pct": h["top10_weight"]}
score, cov = etf.s_concentration(m)
check("concentration now scored", cov, 1.0)
check("concentration score sane", score, 6.5, tol=0.35)


# ---------------------------------------------------------------------------
# 2. ETF type classification
# ---------------------------------------------------------------------------

section("2. Type classification")

cases = [
    ("SGOV", "iShares 0-3 Month Treasury Bond ETF", "Ultrashort Bond", "fixed_income"),
    ("JPST", "JPMorgan Ultra-Short Income ETF", "Ultrashort Bond", "fixed_income"),
    ("MINT", "PIMCO Enhanced Short Maturity Active ETF", "Ultrashort Bond", "fixed_income"),
    ("SHV", "iShares Short Treasury Bond ETF", "Ultrashort Bond", "fixed_income"),
    ("BIL", "SPDR Bloomberg 1-3 Month T-Bill ETF", "Ultrashort Bond", "fixed_income"),
    ("SPSB", "SPDR Portfolio Short Term Corporate Bond ETF", "Short-Term Bond", "fixed_income"),
    ("AGG", "iShares Core U.S. Aggregate Bond ETF", "Intermediate Core Bond", "fixed_income"),
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "Long Government", "fixed_income"),
    ("TQQQ", "ProShares UltraPro QQQ", "Trading--Leveraged Equity", "leveraged_inverse"),
    ("TBT", "ProShares UltraShort 20+ Year Treasury", "Trading--Inverse Debt", "leveraged_inverse"),
    ("SQQQ", "ProShares UltraPro Short QQQ", "Trading--Inverse Equity", "leveraged_inverse"),
    ("SOXL", "Direxion Daily Semiconductor Bull 3X Shares", "Trading--Leveraged Equity", "leveraged_inverse"),
    ("XLE", "Energy Select Sector SPDR Fund", "Equity Energy", "equity_sector"),
    ("GDX", "VanEck Gold Miners ETF", "Equity Precious Metals", "equity_sector"),
    ("XLV", "Health Care Select Sector SPDR Fund", "Health", "equity_sector"),
    ("XLU", "Utilities Select Sector SPDR Fund", "Utilities", "equity_sector"),
    ("SMH", "VanEck Semiconductor ETF", "Technology", "equity_sector"),
    ("GLD", "SPDR Gold Shares", "Commodities Focused", "commodity"),
    ("DBC", "Invesco DB Commodity Index Tracking Fund", "Commodities Broad Basket", "commodity"),
    ("VNQ", "Vanguard Real Estate Index Fund ETF Shares", "Real Estate", "real_estate"),
    ("VT", "Vanguard Total World Stock Index Fund ETF", "Global Large-Stock Blend", "equity_international"),
    ("VWO", "Vanguard FTSE Emerging Markets ETF", "Diversified Emerging Mkts", "equity_international"),
    ("VEA", "Vanguard FTSE Developed Markets ETF", "Foreign Large Blend", "equity_international"),
    ("XIC.TO", "iShares Core S&P/TSX Capped Composite Index ETF", "Canadian Equity", "equity_international"),
    ("SPY", "SPDR S&P 500 ETF Trust", "Large Blend", "equity_broad"),
    ("VTI", "Vanguard Total Stock Market Index Fund ETF Shares", "Large Blend", "equity_broad"),
    ("QQQ", "Invesco QQQ Trust", "Large Growth", "equity_factor"),
    ("SCHD", "Schwab US Dividend Equity ETF", "Large Value", "equity_factor"),
    ("USMV", "iShares MSCI USA Min Vol Factor ETF", "Large Blend", "equity_factor"),
    ("ARKK", "ARK Innovation ETF", "Mid-Cap Growth", "equity_thematic"),
    ("IBIT", "iShares Bitcoin Trust", "Digital Assets", "equity_thematic"),
    ("AOR", "iShares Core Growth Allocation ETF", "Moderate Allocation", "multi_asset"),
]

for tk, name, cat, want in cases:
    got = etf.detect_etf_type({"longName": name, "category": cat})
    check(f"{tk:<7} {cat:<26} -> {want}", got, want)


# ---------------------------------------------------------------------------
# 3. Return windows anchored on dates, not bar counts
# ---------------------------------------------------------------------------

section("3. Return windows")

full = etf.calc_returns(make_prices(years=5.0, cagr=0.10))
check("1Y  on 5y history", full.get("1y"), 0.10, tol=0.004)
check("3Y  on 5y history", full.get("3y_ann"), 0.10, tol=0.004)
check("5Y  on 5y history", full.get("5y_ann"), 0.10, tol=0.004)

short = etf.calc_returns(make_prices(years=0.5, cagr=0.10))
check("6mo history -> no 1Y number", short.get("1y"), None)
check("6mo history -> no 3Y number", short.get("3y_ann"), None)

tiny = etf.calc_returns(make_prices(years=0.1, cagr=0.10))
check("25d history -> no 1Y number", tiny.get("1y"), None)
check("25d history -> no volatility", tiny.get("ann_vol"), None)

m_tiny = {"etf_type": "equity_broad", "returns": tiny}
_, cov = etf.s_performance(m_tiny, None, True)
check("25d fund -> performance unmeasured", cov, 0.0)

three = etf.calc_returns(make_prices(years=3.2, cagr=0.10))
check("3.2y history -> 3Y present", three.get("3y_ann") is not None, True)
check("3.2y history -> 5Y absent", three.get("5y_ann"), None)
check("YTD computed", full.get("ytd") is not None, True)


# ---------------------------------------------------------------------------
# 4. Sharpe uses geometric return
# ---------------------------------------------------------------------------

section("4. Sharpe ratio")

print("  vol    arith-annualized   CAGR      script Sharpe   naive Sharpe")
worst = 0.0
for vol in (0.15, 0.45, 0.85):
    h5 = make_prices(years=5.0, cagr=0.10, vol=vol, seed=11)
    r = etf.calc_returns(h5, rfr=0.03)
    d = h5["Close"].pct_change().dropna()
    arith = d.mean() * 252
    naive = (arith - 0.03) / r["ann_vol"]
    print(f"  {vol:4.0%}   {arith:+13.2%}   {r['cagr']:+7.2%}   "
          f"{r['sharpe']:12.2f}   {naive:12.2f}")
    worst = max(worst, abs(naive - r["sharpe"]))

check("CAGR is what Sharpe uses", 
      abs(etf.calc_returns(make_prices(5.0, 0.10, 0.45, 11), rfr=0.03)["sharpe"]
          - (etf.calc_returns(make_prices(5.0, 0.10, 0.45, 11))["cagr"] - 0.03)
          / etf.calc_returns(make_prices(5.0, 0.10, 0.45, 11))["ann_vol"]) < 1e-9,
      True)
check("geometric vs naive diverge at high vol", worst > 0.15, True)

# Sharpe must no longer influence the Risk dimension.
base = {"etf_type": "equity_broad",
        "returns": {"ann_vol": 0.16, "max_drawdown": -0.24, "sharpe": 0.4}}
hi = {"etf_type": "equity_broad",
      "returns": {"ann_vol": 0.16, "max_drawdown": -0.24, "sharpe": 1.4}}
check("Risk ignores Sharpe (no double count)",
      etf.s_risk(base)[0], etf.s_risk(hi)[0], tol=1e-9)


# ---------------------------------------------------------------------------
# 5. Coverage-aware scoring
# ---------------------------------------------------------------------------

section("5. Coverage-aware scoring")

empty = {"etf_type": "equity_broad", "expense_ratio": None, "aum": None,
         "nav_premium": None, "yield": None, "returns": {},
         "top1_pct": None, "top10_pct": None, "category_expense_ratio": None}
sc = etf.build_scores(empty, None, None)
check("no data -> no composite", sc["composite"], None)
check("no data -> flagged as such", sc["rating"], "INSUFFICIENT DATA")
check("no data -> zero coverage", round(sc["coverage"], 3), 0.0)

good = {"etf_type": "equity_broad", "expense_ratio": 0.0003, "aum": 6.0e11,
        "nav_premium": 0.0002, "yield": 0.012, "category_expense_ratio": 0.0078,
        "returns": {"ann_vol": 0.15, "max_drawdown": -0.18, "sharpe": 1.1,
                    "1y": 0.14, "dollar_volume": 2.5e10},
        "top1_pct": 0.075, "top10_pct": 0.37}
sc2 = etf.build_scores(good, 0.001, {"1y": 0.13})
check("full data -> composite present", sc2["composite"] is not None, True)
check("full data -> high coverage", sc2["coverage"] > 0.9, True)
check("income unmeasured for broad equity", "Income" in sc2["missing"], True)
check("composite rounded to 1dp",
      sc2["composite"], round(sc2["composite"], 1), tol=1e-12)

# Weight of a missing dimension is redistributed, not filled with 5.0.
no_conc = dict(good, top1_pct=None, top10_pct=None)
sc3 = etf.build_scores(no_conc, 0.001, {"1y": 0.13})
check("missing concentration is dropped", sc3["dims"]["Concentration"], None)
check("missing concentration lowers coverage", sc3["coverage"] < sc2["coverage"], True)

for k, v in etf.ETF_WEIGHTS.items():
    check(f"weights sum to 1.0 ({k})", round(sum(v), 9), 1.0)


# ---------------------------------------------------------------------------
# 6. Income, cost, structure, liquidity semantics
# ---------------------------------------------------------------------------

section("6. Dimension semantics")

check("income not scored for broad equity",
      etf.s_income({"etf_type": "equity_broad", "yield": 0.012})[1], 0.0)
check("income scored for bonds",
      etf.s_income({"etf_type": "fixed_income", "yield": 0.05})[1], 1.0)
check("zero yield is a real zero, not missing",
      etf.s_income({"etf_type": "fixed_income", "yield": 0.0})[0], 1.0)
check("missing yield is unmeasured",
      etf.s_income({"etf_type": "fixed_income", "yield": None})[1], 0.0)

cheap = {"etf_type": "equity_broad", "expense_ratio": 0.0003,
         "category_expense_ratio": 0.0078}
dear = {"etf_type": "equity_broad", "expense_ratio": 0.0070,
        "category_expense_ratio": 0.0020}
check("cost beats peers -> high", etf.s_cost(cheap)[0] > 9.0, True)
check("cost above peers -> low", etf.s_cost(dear)[0] < 3.0, True)
check("cost with peers -> full coverage", etf.s_cost(cheap)[1], 1.0)
check("cost without peers -> partial coverage",
      etf.s_cost({"etf_type": "equity_broad", "expense_ratio": 0.0003})[1], 0.75)

# Liquidity is log-scaled and volume aware.
big_thin = {"aum": 3e9, "returns": {"dollar_volume": 2e5}}
small_deep = {"aum": 4e8, "returns": {"dollar_volume": 2e7}}
check("thin $3B scores below deep $400M",
      etf.s_liquidity(big_thin)[0] < etf.s_liquidity(small_deep)[0], True)
check("liquidity uses log scale — $1B is not near-zero",
      etf.s_liquidity({"aum": 1e9, "returns": {}})[0] > 5.0, True)

# Proxy deviation is only scored where the proxy is credible.
check("sector deviation not scored",
      etf.s_structure({"etf_type": "equity_sector", "nav_premium": None}, 0.08)[1], 0.0)
check("broad-equity deviation is scored",
      etf.s_structure({"etf_type": "equity_broad", "nav_premium": None}, 0.008)[1], 0.4)


# ---------------------------------------------------------------------------
# 7. Units, warnings, benchmarks
# ---------------------------------------------------------------------------

section("7. Units, warnings, benchmarks")

check("percent-scaled rate coerced", etf.normalize_rate(1.32), 0.0132, tol=1e-12)
check("big return NOT rescaled", etf.as_float(0.80), 0.80, tol=1e-12)
check("negative return preserved", etf.as_float(-0.35), -0.35, tol=1e-12)
check("nan return dropped", etf.as_float(float("nan")), None)
check("decimal rate untouched", etf.normalize_rate(0.0132), 0.0132, tol=1e-12)
check("zero preserved", etf.normalize_rate(0.0), 0.0)
check("none preserved", etf.normalize_rate(None), None)


def warn_for(er, etype="equity_broad"):
    m = {"etf_type": etype, "expense_ratio": er, "aum": 5e9, "nav_premium": None,
         "yield": None, "returns": {}, "name": "X", "currency": "USD",
         "category_expense_ratio": None, "warnings": [], "notes": [],
         "holdings": {"top_holdings": [], "top10_weight": None,
                      "legal_type": None}}
    etf.build_warnings(m)
    return " ".join(m["warnings"])


check("0.50% ER -> no warning", "expense ratio" in warn_for(0.0050), False)
check("0.80% ER -> 'High'", "High expense ratio" in warn_for(0.0080), True)
check("1.20% ER -> 'Very high' (was unreachable)",
      "Very high expense ratio" in warn_for(0.0120), True)
check("2.50% ER -> 'Very high'",
      "Very high expense ratio" in warn_for(0.0250), True)

# NAV premium inside one session's move is a note, not a warning.
quiet = {"etf_type": "equity_broad", "expense_ratio": 0.0003, "aum": 5e9,
         "nav_premium": 0.004, "yield": None, "currency": "USD",
         "returns": {"ann_vol": 0.16}, "category_expense_ratio": None,
         "warnings": [], "notes": [], "name": "X",
         "holdings": {"top_holdings": [], "top10_weight": None, "legal_type": None}}
etf.build_warnings(quiet)
check("small NAV gap is not a warning",
      any("premium" in w for w in quiet["warnings"]), False)
check("small NAV gap is a note",
      any("within one session" in n for n in quiet["notes"]), True)

loud = dict(quiet, nav_premium=0.05, warnings=[], notes=[])
etf.build_warnings(loud)
check("large NAV gap is a warning",
      any("premium" in w for w in loud["warnings"]), True)

# Benchmarks follow the listing currency.
check("USD broad equity -> SPY",
      etf.pick_benchmark("equity_broad", "Large Blend", "USD")[0], "SPY")
check("CAD broad equity -> TSX proxy",
      etf.pick_benchmark("equity_broad", "Canadian Equity", "CAD")[0], "XIC.TO")
check("CAD bonds -> CAD bond proxy",
      etf.pick_benchmark("fixed_income", "Canadian Fixed Income", "CAD")[0], "XBB.TO")
check("emerging split from developed",
      etf.pick_benchmark("equity_international", "Diversified Emerging Mkts", "USD")[0],
      "VWO")
check("developed stays developed",
      etf.pick_benchmark("equity_international", "Foreign Large Blend", "USD")[0],
      "VEA")
check("unsupported currency flagged",
      etf.pick_benchmark("equity_broad", "Japan Equity", "JPY")[2], False)


# ---------------------------------------------------------------------------
# 8. End-to-end: the fund that broke v1.1
# ---------------------------------------------------------------------------

section("8. End-to-end — SGOV (0-3 month T-bills)")

info = {"longName": "iShares 0-3 Month Treasury Bond ETF",
        "category": "Ultrashort Bond", "currency": "USD"}
etype = etf.detect_etf_type(info)
bm, bml, matched = etf.pick_benchmark(etype, info["category"], "USD")

sgov = {"etf_type": etype, "expense_ratio": 0.0009, "category_expense_ratio": 0.0031,
        "aum": 5.4e10, "nav_premium": 0.0001, "yield": 0.0512,
        "returns": {"ann_vol": 0.004, "max_drawdown": -0.0009, "sharpe": 0.9,
                    "1y": 0.0521, "dollar_volume": 8e8},
        "top1_pct": None, "top10_pct": None}
sc = etf.build_scores(sgov, 0.011, {"1y": 0.16})

print(f"  type       : {etype}")
print(f"  benchmark  : {bm} ({bml})")
print(f"  dims       : {sc['dims']}")
print(f"  composite  : {sc['composite']} -> {sc['rating']}")
print(f"  coverage   : {sc['coverage'] * 100:.0f}%")

big = etf.calc_metrics({
    "ticker": "TQQQ", "funds_data": None, "yf": None, "hist": None,
    "info": {"shortName": "ProShares UltraPro QQQ",
             "longName": "ProShares UltraPro QQQ",
             "category": "Trading--Leveraged Equity", "currency": "USD",
             "ytdReturn": 0.82, "oneYearTotalReturn": 1.45},
})
check("+82% YTD survives intact", big["yahoo_returns"]["YTD"], 0.82, tol=1e-12)
check("+145% 1Y survives intact", big["yahoo_returns"]["1Y"], 1.45, tol=1e-12)

check("SGOV classified as fixed income", etype, "fixed_income")
check("SGOV benchmarked to a bond index", bm, "AGG")
check("SGOV not called speculative", "SPECULATIVE" in sc["rating"], False)
check("SGOV concentration not scored", sc["dims"]["Concentration"], None)
check("SGOV scores well", sc["composite"] > 7.5, True)


# ---------------------------------------------------------------------------
# 9. Full report renders end to end
# ---------------------------------------------------------------------------

section("9. Report rendering (no network)")

etf.get_risk_free_rate = lambda ccy, hist: (0.030, "test stub")

etf_hist = make_prices(years=5.0, cagr=0.13, vol=0.16, seed=3)
bm_hist = make_prices(years=5.0, cagr=0.12, vol=0.15, seed=4)

data = {
    "ticker": "SPY",
    "info": {
        "longName": "SPDR S&P 500 ETF Trust", "shortName": "SPDR S&P 500",
        "category": "Large Blend", "currency": "USD", "quoteType": "ETF",
        "fundFamily": "SPDR State Street Global Advisors",
        "regularMarketPrice": 642.18, "navPrice": 641.94,
        "totalAssets": 6.4e11, "annualReportExpenseRatio": 0.000945,
        "yield": 0.0113, "beta3Year": 1.0, "totalHoldings": 503,
        "fiftyTwoWeekHigh": 655.20, "fiftyTwoWeekLow": 481.80,
        "ytdReturn": 0.104, "threeYearAverageReturn": 0.171,
    },
    "funds_data": FakeFundsData("SPY"),
    "hist": etf_hist,
    "yf": None,
}

m = etf.calc_metrics(data)
bm_tick, bm_label, matched = etf.pick_benchmark(
    m["etf_type"], m.get("category"), m.get("currency")
)
bm_ret = etf.calc_returns(bm_hist, rfr=m["rfr"])
td = etf.calc_tracking_diff(etf_hist, bm_hist)
sc = etf.build_scores(m, td, bm_ret, currency_matched=matched)

import io
import contextlib

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    etf.print_report("SPY", m, sc, td, bm_ret, bm_tick, bm_label, matched)
report = buf.getvalue()

check("report renders", len(report) > 1500, True)
for heading in ("COMPOSITE SCORE", "Data coverage", "DIMENSION BREAKDOWN",
                "SUMMARY", "ETF OVERVIEW", "HOLDINGS BREAKDOWN",
                "RISK & PERFORMANCE"):
    check(f"report contains {heading!r}", heading in report, True)

check("report shows peer expense ratio", "category avg" in report, True)
check("report shows median volume", "/day" in report, True)
check("report shows top holdings", "NVIDIA Corp" in report, True)
check("report shows geometric Sharpe", "Sharpe (geometric)" in report, True)
check("report separates Yahoo-reported returns",
      "Yahoo-reported returns" in report, True)
check("report labels beta window", "Beta (3Y)" in report, True)
check("no stray None in output", " None" in report, False)

print()
print(report)


# ---------------------------------------------------------------------------

print()
print("=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILED of {CHECKS[0]} checks:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)

print(f"All {CHECKS[0]} checks passed.")

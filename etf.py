#!/usr/bin/env python3
"""
ETF Evaluator v2.0 — Multi-Type ETF Analysis

Yahoo Finance via yfinance.

WHAT THE SCORE MEANS
    The composite grades how well a fund IMPLEMENTS ITS OWN CATEGORY —
    cost versus its peers, liquidity, structural integrity, index fidelity,
    concentration. It does NOT say whether that category belongs in your
    portfolio. A 9/10 commodity ETF is a good commodity ETF; that is a
    separate question from whether you should own commodities at all.

    Dimensions are scored only where the underlying data actually exists.
    Weights are renormalized over what was measured, and the report prints
    a data-coverage figure. Below MIN_COVERAGE no composite is emitted —
    an unmeasured fund is reported as unmeasured, not as mediocre.

Handles:
- Broad equity, sector, factor / smart beta, thematic, international
- Fixed income, commodity, real estate, leveraged / inverse, multi-asset

Setup:
    pip install yfinance

Usage:
    python etf.py SPY
    python etf.py XIC.TO
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from typing import Any, Optional

import pandas as pd
import yfinance as yf


if sys.platform == "win32":
    os.system("color")


# ANSI colors
G = "\033[92m"
Y = "\033[93m"
R = "\033[91m"
C = "\033[96m"
B = "\033[1m"
X = "\033[0m"


# Short-rate fallbacks by fund currency, used when the live proxy is
# unavailable. Only ever a fallback — see get_risk_free_rate().
RFR_FALLBACK = {
    "USD": 0.043,
    "CAD": 0.032,
    "EUR": 0.026,
    "GBP": 0.042,
    "JPY": 0.005,
    "CHF": 0.005,
    "AUD": 0.043,
}
RFR_DEFAULT = 0.030

# Yahoo ticker for the 13-week US T-bill yield, quoted in percent.
RFR_PROXY_USD = "^IRX"

# Composite is suppressed below this weighted data coverage.
MIN_COVERAGE = 0.55


# ---------------------------------------------------------------------------
# DISPLAY HELPERS
# ---------------------------------------------------------------------------

def colour(v: Optional[float], t: Optional[str] = None) -> str:
    """Color a 0-10 score."""
    if v is None:
        return f"{Y}N/A{X}"

    label = t if t is not None else f"{v:.1f}"

    if v >= 7.5:
        return f"{G}{B}{label}{X}"
    if v >= 5.0:
        return f"{Y}{label}{X}"
    return f"{R}{label}{X}"


def bar(s: Optional[float], w: int = 14) -> str:
    """Return a 0-10 visual score bar."""
    if s is None:
        return "·" * w

    s = max(0.0, min(10.0, s))
    filled = int(round((s / 10.0) * w))
    return "█" * filled + "░" * (w - filled)


def pct_str(v: Optional[float], decimals: int = 2) -> str:
    """Format a decimal as a percentage."""
    if v is None:
        return "N/A"
    return f"{v * 100:.{decimals}f}%"


def fmt_money(v: Optional[float]) -> str:
    """Format a number as money."""
    if v is None:
        return "N/A"

    try:
        v = float(v)
    except Exception:
        return "N/A"

    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def bar_pct(val: Optional[float], max_val: float = 0.40, width: int = 16) -> str:
    """Horizontal bar scaled to max_val."""
    if val is None:
        return "░" * width

    filled = int(round((min(abs(float(val)), max_val) / max_val) * width))
    return "█" * filled + "░" * (width - filled)


RATING_ALIASES = {
    "aaa": "AAA", "aa": "AA", "a": "A", "bbb": "BBB", "bb": "BB", "b": "B",
    "below_b": "Below B", "us_government": "US Government",
    "other": "Other", "not_rated": "Not Rated",
}

SECTOR_ALIASES = {
    "realestate": "Real Estate",
    "basic_materials": "Basic Materials",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "financial_services": "Financial Services",
    "communication_services": "Communication Services",
    "healthcare": "Healthcare",
}


def fmt_sector(name: str) -> str:
    """Convert Yahoo's sector keys into readable title case."""
    key = str(name)
    if key in SECTOR_ALIASES:
        return SECTOR_ALIASES[key]
    if key in RATING_ALIASES:
        return RATING_ALIASES[key]

    out = re.sub(r"([A-Z])", r" \1", key).replace("_", " ")
    return out.strip().title()


def score_val(
    val: Optional[float],
    good: float,
    bad: float,
    higher: bool = True,
) -> float:
    """Map a raw value to a 1-10 score by linear interpolation."""
    if val is None:
        return 5.0

    try:
        val = float(val)
    except Exception:
        return 5.0

    if higher:
        if val >= good:
            return 10.0
        if val <= bad:
            return 1.0
        return 1.0 + 9.0 * (val - bad) / (good - bad)

    if val <= good:
        return 10.0
    if val >= bad:
        return 1.0
    return 1.0 + 9.0 * (bad - val) / (bad - good)


def score_log(
    val: Optional[float],
    good: float,
    bad: float,
) -> float:
    """
    Score on a log10 scale, for quantities where each order of magnitude
    matters more than each dollar (AUM, traded volume).
    """
    if val is None or val <= 0:
        return None if val is None else 1.0

    return score_val(
        math.log10(val), math.log10(good), math.log10(bad), higher=True
    )


# ---------------------------------------------------------------------------
# ETF TYPE DETECTION
# ---------------------------------------------------------------------------

ETF_TYPE_LABELS = {
    "equity_broad": "Broad Equity",
    "equity_sector": "Sector Equity",
    "equity_factor": "Factor / Smart Beta",
    "equity_thematic": "Thematic Equity",
    "equity_international": "International Equity",
    "fixed_income": "Fixed Income",
    "commodity": "Commodity",
    "real_estate": "Real Estate",
    "leveraged_inverse": "Leveraged / Inverse",
    "multi_asset": "Multi-Asset",
}


def _norm(s: Optional[str]) -> str:
    """Lowercase and reduce to space-separated alphanumeric tokens."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _has(text: str, terms: list[str]) -> bool:
    """Whole-word / whole-phrase match against normalized text."""
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms)


def detect_etf_type(info: dict[str, Any]) -> str:
    """
    Classify ETF type from Yahoo Finance name/category data.

    Order matters. Leveraged is checked first but on a strict signature so
    that ordinary short-duration bond funds ("Ultrashort Bond",
    "Short-Term Bond") are not swept up; commodity is gated on the category
    not being an equity category so that miner funds stay equities.
    """
    name = _norm(info.get("longName") or info.get("shortName"))
    category = _norm(info.get("category"))
    both = f"{name} {category}"

    # --- Leveraged / inverse -------------------------------------------
    # Morningstar files these under "Trading--Leveraged/Inverse <asset>".
    if _has(category, ["trading leveraged", "trading inverse", "leveraged", "inverse"]):
        return "leveraged_inverse"

    if _has(name, ["leveraged", "inverse", "ultrapro", "ultra pro"]):
        return "leveraged_inverse"

    # Explicit multiples: 2x, 3x, -1x, 1.5x
    if re.search(r"\b-?\d(?:\s?\d)?x\b", name):
        return "leveraged_inverse"

    # Daily-reset directional products
    if _has(name, ["daily"]) and _has(name, ["bull", "bear"]):
        return "leveraged_inverse"

    # ProShares reserves "Ultra"/"UltraShort" for its leveraged range
    if _has(name, ["proshares"]) and _has(name, ["ultra", "ultrashort"]):
        return "leveraged_inverse"

    # --- Fixed income ---------------------------------------------------
    bond_kw = [
        "bond", "bonds", "treasury", "treasuries", "fixed income", "credit",
        "inflation protected", "high yield", "muni", "municipal", "tips",
        "government", "aggregate", "securitized", "loan", "mortgage",
        "ultrashort bond", "short term bond", "convertible",
    ]
    if _has(category, bond_kw) or _has(name, bond_kw):
        return "fixed_income"

    if _has(category, ["money market", "cash"]):
        return "fixed_income"

    # --- Commodity ------------------------------------------------------
    # Gate on the category not being an equity category: "Equity Precious
    # Metals" (miners) is an equity fund, "Commodities Focused" is not.
    is_equity_cat = _has(category, ["equity", "stock", "stocks"])

    if _has(category, ["commodities", "commodity"]):
        return "commodity"

    if not is_equity_cat and _has(
        both,
        [
            "gold", "silver", "platinum", "palladium", "crude oil",
            "natural gas", "precious metals", "copper", "wheat", "corn",
            "agriculture", "bullion",
        ],
    ):
        return "commodity"

    # --- Real estate ----------------------------------------------------
    if _has(category, ["real estate"]) or _has(name, ["real estate", "reit", "reits"]):
        return "real_estate"

    # --- International equity -------------------------------------------
    intl_kw = [
        "foreign", "international", "emerging", "world", "global", "europe",
        "european", "asia", "pacific", "china", "japan", "india", "latin america",
        "developed markets", "ex us", "canadian", "canada", "uk", "germany",
        "brazil", "korea", "taiwan", "mexico", "diversified emerging mkts",
    ]
    if _has(category, intl_kw):
        return "equity_international"

    # --- Sector equity ---------------------------------------------------
    sector_kw = [
        "technology", "health", "healthcare", "health care", "financial",
        "financials", "equity energy", "utilities", "consumer cyclical",
        "consumer defensive", "industrials", "materials", "natural resources",
        "communications", "communication", "equity precious metals", "biotech",
        "biotechnology", "semiconductor", "semiconductors", "infrastructure",
        "miscellaneous sector", "miscellaneous region",
    ]
    if _has(category, sector_kw):
        return "equity_sector"

    # --- Thematic --------------------------------------------------------
    thematic_kw = [
        "cloud", "cyber", "cybersecurity", "artificial intelligence", "ai",
        "innovation", "disruptive", "clean energy", "cannabis", "space",
        "robotics", "genomic", "genomics", "electric vehicle", "blockchain",
        "cryptocurrency", "bitcoin", "ethereum", "digital assets", "gaming",
        "metaverse", "solar", "hydrogen", "battery", "esg", "clean tech",
    ]
    if _has(both, thematic_kw):
        return "equity_thematic"

    # --- Multi-asset -------------------------------------------------------
    # Checked before factor: an "...Growth Allocation..." fund would
    # otherwise be caught by the factor name match.
    if _has(category, ["allocation", "balanced", "multi asset", "target date", "target risk"]):
        return "multi_asset"

    if _has(name, ["allocation", "balanced portfolio", "target date", "target risk"]):
        return "multi_asset"

    # --- Factor / smart beta ---------------------------------------------
    factor_kw = [
        "value", "growth", "momentum", "quality", "dividend", "low volatility",
        "minimum volatility", "min vol", "multifactor", "factor", "equal weight",
        "buyback", "high beta", "small value", "small growth", "mid value",
        "mid growth", "large value", "large growth",
    ]
    if _has(category, factor_kw) or _has(name, factor_kw):
        return "equity_factor"

    return "equity_broad"


# ---------------------------------------------------------------------------
# BENCHMARKS
# ---------------------------------------------------------------------------

# Benchmarks are chosen per listing currency. Comparing a CAD-listed fund
# against a USD-priced benchmark folds the entire CAD/USD move into the
# "excess return" and the tracking figure, so each supported currency gets
# its own locally-listed proxies.
BENCHMARKS_BY_CCY: dict[str, dict[str, tuple[str, str]]] = {
    "USD": {
        "equity_broad": ("SPY", "S&P 500"),
        "equity_sector": ("SPY", "S&P 500"),
        "equity_factor": ("SPY", "S&P 500"),
        "equity_thematic": ("QQQ", "Nasdaq-100"),
        "equity_international": ("VEA", "FTSE Dev ex-US"),
        "equity_emerging": ("VWO", "FTSE Emerging"),
        "fixed_income": ("AGG", "US Agg Bond"),
        "commodity": ("DJP", "Bloomberg Commodity"),
        "real_estate": ("VNQ", "US Real Estate"),
        "leveraged_inverse": ("SPY", "S&P 500"),
        "multi_asset": ("AOR", "Growth Allocation"),
    },
    "CAD": {
        "equity_broad": ("XIC.TO", "S&P/TSX Composite"),
        "equity_sector": ("XIC.TO", "S&P/TSX Composite"),
        "equity_factor": ("XIC.TO", "S&P/TSX Composite"),
        "equity_thematic": ("XIC.TO", "S&P/TSX Composite"),
        "equity_international": ("XEF.TO", "MSCI EAFE (CAD)"),
        "equity_emerging": ("XEC.TO", "MSCI EM (CAD)"),
        "fixed_income": ("XBB.TO", "FTSE Canada Universe Bond"),
        "commodity": ("XIC.TO", "S&P/TSX Composite"),
        "real_estate": ("XRE.TO", "S&P/TSX Capped REIT"),
        "leveraged_inverse": ("XIC.TO", "S&P/TSX Composite"),
        "multi_asset": ("XBAL.TO", "Balanced Allocation (CAD)"),
    },
}


def pick_benchmark(
    etf_type: str,
    category: Optional[str],
    currency: Optional[str],
) -> tuple[str, str, bool]:
    """
    Return (ticker, label, currency_matched).

    Emerging-market funds are split out from developed international so a
    Brazil or China fund is not measured against a developed ex-US proxy.
    """
    ccy = (currency or "USD").upper()
    matched = ccy in BENCHMARKS_BY_CCY
    table = BENCHMARKS_BY_CCY.get(ccy, BENCHMARKS_BY_CCY["USD"])

    key = etf_type
    if etf_type == "equity_international" and _has(
        _norm(category), ["emerging", "china", "india", "brazil", "latin america"]
    ):
        key = "equity_emerging"

    ticker, label = table.get(key, table["equity_broad"])
    return ticker, label, matched


# Dimension order:
# cost, liquidity, structure, risk, income, performance, concentration
ETF_WEIGHTS = {
    "equity_broad": (0.28, 0.18, 0.14, 0.12, 0.06, 0.12, 0.10),
    "equity_sector": (0.22, 0.14, 0.12, 0.12, 0.06, 0.14, 0.20),
    "equity_factor": (0.22, 0.14, 0.12, 0.12, 0.08, 0.14, 0.18),
    "equity_thematic": (0.18, 0.13, 0.10, 0.12, 0.04, 0.16, 0.27),
    "equity_international": (0.22, 0.14, 0.16, 0.14, 0.08, 0.14, 0.12),
    "fixed_income": (0.24, 0.15, 0.22, 0.18, 0.10, 0.08, 0.03),
    "commodity": (0.28, 0.16, 0.26, 0.14, 0.04, 0.08, 0.04),
    "real_estate": (0.22, 0.14, 0.14, 0.14, 0.13, 0.12, 0.11),
    "leveraged_inverse": (0.18, 0.24, 0.16, 0.22, 0.04, 0.10, 0.06),
    "multi_asset": (0.22, 0.14, 0.14, 0.15, 0.10, 0.15, 0.10),
}

# Distribution yield is only scored where income is part of the mandate.
# Elsewhere the distribution is already inside total return, and rewarding
# a high yield just rewards a value/dividend tilt.
INCOME_RELEVANT = {
    "fixed_income",
    "real_estate",
    "multi_asset",
    "equity_factor",
    "equity_international",
}

# Single-name concentration is not a meaningful risk for a physically
# backed commodity fund (one asset by design) or for a Treasury fund
# (many CUSIPs, one issuer).
CONCENTRATION_RELEVANT = {
    "equity_broad",
    "equity_sector",
    "equity_factor",
    "equity_thematic",
    "equity_international",
    "real_estate",
    "multi_asset",
}

# The category proxy is only a credible stand-in for the fund's real index
# for these types. Elsewhere the deviation figure is reported but not
# scored — a sector fund is supposed to diverge from the S&P 500.
PROXY_IS_CREDIBLE = {"equity_broad", "fixed_income"}


# ---------------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------------

def _retry(fn, attempts: int = 3, delay: float = 1.0):
    """Call fn with exponential backoff; return None if it never succeeds."""
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                return None
            time.sleep(delay * (2 ** i))
    return None


def get_etf_data(ticker: str, info: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """
    Fetch ETF data from yfinance.

    `info` may be supplied by the exchange probe so the same .info payload
    is not fetched twice.
    """
    print(f"\n  {C}Fetching data for {ticker.upper()}...{X}")

    try:
        t = yf.Ticker(ticker)

        if info is None:
            info = _retry(lambda: t.info) or {}

        if not info or not (info.get("shortName") or info.get("longName")):
            print(f"\n  {R}Ticker '{ticker}' not found.{X}\n")
            return None

        q_type = info.get("quoteType", "")
        if q_type not in ("ETF", "MUTUALFUND", ""):
            print(
                f"\n  {Y}Warning: '{ticker}' may not be an ETF "
                f"(quoteType={q_type}). Continuing anyway.{X}"
            )

        funds_data = _retry(lambda: t.get_funds_data(), attempts=2)

        # auto_adjust=True is explicit: returns must be total returns, and
        # the yfinance default for this has changed across versions.
        hist = _retry(lambda: t.history(period="5y", auto_adjust=True))

        return {
            "ticker": ticker.upper(),
            "info": info,
            "funds_data": funds_data,
            "hist": hist,
            "yf": t,
        }

    except Exception as e:
        print(f"\n  {R}Error fetching data: {e}{X}\n")
        return None


def get_benchmark_data(bm_ticker: str) -> Optional[dict[str, Any]]:
    """Fetch benchmark price history."""
    hist = _retry(
        lambda: yf.Ticker(bm_ticker).history(period="5y", auto_adjust=True),
        attempts=2,
    )

    if hist is None or hist.empty:
        return None

    return {"ticker": bm_ticker, "hist": hist}


def get_risk_free_rate(currency: Optional[str], hist: Any) -> tuple[float, str]:
    """
    Average short rate over the same window as the price history.

    A single spot rate applied to a 5-year window is wrong by however much
    policy rates moved over that window; the 2020-2025 window in particular
    spans near-zero rates. For USD the realized average of the 13-week bill
    is used. Other currencies fall back to a static estimate.
    """
    ccy = (currency or "USD").upper()
    fallback = RFR_FALLBACK.get(ccy, RFR_DEFAULT)

    if ccy != "USD" or hist is None or getattr(hist, "empty", True):
        return fallback, f"{ccy} static estimate"

    irx = _retry(
        lambda: yf.Ticker(RFR_PROXY_USD).history(period="5y", auto_adjust=False),
        attempts=2,
    )

    if irx is None or irx.empty or "Close" not in irx:
        return fallback, "USD static estimate"

    try:
        # ^IRX is quoted in percent.
        series = irx["Close"].dropna()
        start = hist.index[0]
        series = series[series.index >= start]

        if len(series) < 30:
            return fallback, "USD static estimate"

        return float(series.mean()) / 100.0, f"{RFR_PROXY_USD} mean over window"
    except Exception:
        return fallback, "USD static estimate"


# ---------------------------------------------------------------------------
# RETURN / RISK CALCULATIONS
# ---------------------------------------------------------------------------

# A lookback is only reported if history reaches back to within this many
# days of the target date. Prevents a 3-month-old fund reporting a "1Y".
LOOKBACK_TOLERANCE_DAYS = 12


def _lookback_return(prices: Any, years: float) -> Optional[float]:
    """
    Annualized total return over `years`, anchored on calendar dates rather
    than a fixed bar count. Returns None if history does not reach back far
    enough — never a shorter window silently relabelled.
    """
    if prices is None or len(prices) < 2:
        return None

    idx = prices.index
    end = idx[-1]
    target = end - pd.DateOffset(years=int(years)) if float(years).is_integer() \
        else end - pd.Timedelta(days=int(365.25 * years))

    if idx[0] > target + pd.Timedelta(days=LOOKBACK_TOLERANCE_DAYS):
        return None

    pos = idx.searchsorted(target)
    pos = min(max(int(pos), 0), len(prices) - 1)

    start_px = float(prices.iloc[pos])
    end_px = float(prices.iloc[-1])

    if start_px <= 0:
        return None

    total = end_px / start_px

    if years <= 1:
        return total - 1.0

    return total ** (1.0 / years) - 1.0


def _ytd_return(prices: Any) -> Optional[float]:
    """Total return from the last close of the prior year."""
    if prices is None or len(prices) < 2:
        return None

    idx = prices.index
    year_start = pd.Timestamp(year=idx[-1].year, month=1, day=1, tz=idx.tz)

    prior = prices[idx < year_start]
    if prior.empty:
        return None

    base = float(prior.iloc[-1])
    if base <= 0:
        return None

    return float(prices.iloc[-1]) / base - 1.0


def calc_returns(hist: Any, rfr: float = RFR_DEFAULT) -> dict[str, Any]:
    """
    Annualized returns and risk metrics from adjusted price history.

    Sharpe uses the geometric (CAGR) return, not an annualized arithmetic
    mean. The arithmetic mean overstates realized growth by roughly
    sigma^2/2, which is negligible at 10% vol and worth tens of points of
    annual return at 85% vol — precisely the funds a risk score exists to
    flag.
    """
    if hist is None or getattr(hist, "empty", True):
        return {}

    prices = hist["Close"].dropna()
    n = len(prices)

    if n < 2:
        return {}

    result: dict[str, Any] = {}

    result["ytd"] = _ytd_return(prices)
    result["1y"] = _lookback_return(prices, 1)
    result["3y_ann"] = _lookback_return(prices, 3)
    result["5y_ann"] = _lookback_return(prices, 5)

    daily = prices.pct_change().dropna()

    if len(daily) >= 60:
        vol = float(daily.std()) * math.sqrt(252)
        result["ann_vol"] = vol

        span_days = (prices.index[-1] - prices.index[0]).days
        years = max(span_days / 365.25, 1e-9)
        cagr = (float(prices.iloc[-1]) / float(prices.iloc[0])) ** (1.0 / years) - 1.0

        result["cagr"] = cagr
        result["sharpe"] = (cagr - rfr) / vol if vol > 0 else None

    if n >= 60:
        rolling_max = prices.cummax()
        result["max_drawdown"] = float(((prices - rolling_max) / rolling_max).min())

    if "Volume" in hist and "Close" in hist:
        try:
            dv = (hist["Volume"] * hist["Close"]).dropna()
            recent = dv.iloc[-63:] if len(dv) >= 63 else dv
            if len(recent) >= 10:
                result["dollar_volume"] = float(recent.median())
        except Exception:
            pass

    result["history_days"] = int((prices.index[-1] - prices.index[0]).days)

    return {k: v for k, v in result.items() if v is not None}


def calc_tracking_diff(etf_hist: Any, bm_hist: Any) -> Optional[float]:
    """
    Annualized volatility of the return difference versus the category
    proxy.

    This is NOT tracking error in the index-replication sense — that would
    require the fund's own benchmark index, which Yahoo does not expose.
    Against a category proxy a sector fund legitimately shows a large
    figure. Scored only for types where the proxy is a credible stand-in.
    """
    try:
        er = etf_hist["Close"].dropna().pct_change().dropna()
        br = bm_hist["Close"].dropna().pct_change().dropna()
        common = er.index.intersection(br.index)

        if len(common) < 60:
            return None

        return float((er.loc[common] - br.loc[common]).std()) * math.sqrt(252)

    except Exception:
        return None


def calc_nav_premium(info: dict[str, Any]) -> Optional[float]:
    """
    Market price premium/discount to NAV.

    Yahoo publishes no timestamp for navPrice and it commonly lags by a
    session, so a raw comparison against a live price picks up the day's
    move. The caller compares this against the fund's own daily volatility
    before treating it as a real dislocation.
    """
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    nav = info.get("navPrice")

    if price and nav and nav > 0:
        return (price - nav) / nav

    return None


def as_float(v: Any) -> Optional[float]:
    """Coerce to float, passing values through untouched. For returns."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def normalize_rate(v: Optional[float]) -> Optional[float]:
    """
    Coerce a rate to a decimal fraction.

    Yahoo is inconsistent about whether yields and expense ratios come back
    as 0.0132 or 1.32. Anything above 0.5 is treated as percentage points —
    no ETF yields or charges more than 50% of assets.

    Only for yields and fees. Returns must use as_float(): a +80% year is a
    legitimate 0.80 and must not be rescaled.
    """
    if v is None:
        return None

    try:
        v = float(v)
    except Exception:
        return None

    if v < 0:
        return None

    return v / 100.0 if v > 0.5 else v


# ---------------------------------------------------------------------------
# HOLDINGS EXTRACTION
# ---------------------------------------------------------------------------

def _df_lookup(df: Any, row_label: str, symbol: Optional[str] = None) -> Optional[float]:
    """
    Read one value out of a yfinance stats DataFrame.

    equity_holdings / bond_holdings are DataFrames indexed by a human
    label ("Duration", "Price/Earnings") with one column per symbol plus
    "Category Average" — not dicts.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    if row_label not in df.index:
        return None

    row = df.loc[row_label]

    if symbol is not None and symbol in row.index:
        val = row[symbol]
    else:
        cols = [c for c in row.index if c != "Category Average"]
        if not cols:
            return None
        val = row[cols[0]]

    if val is None or (isinstance(val, float) and math.isnan(val)) or val is pd.NA:
        return None

    try:
        return float(val)
    except Exception:
        return None


def _df_category_avg(df: Any, row_label: str) -> Optional[float]:
    """Read the peer-group average for a row, when Yahoo supplies one."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    if row_label not in df.index or "Category Average" not in df.columns:
        return None

    val = df.loc[row_label, "Category Average"]

    if val is None or (isinstance(val, float) and math.isnan(val)) or val is pd.NA:
        return None

    try:
        return float(val)
    except Exception:
        return None


def extract_holdings(funds_data: Any, symbol: Optional[str] = None) -> dict[str, Any]:
    """
    Pull top holdings, sector weights, asset mix and bond/equity aggregates
    out of yfinance funds_data.

    Shapes as of yfinance 1.x:
      top_holdings     DataFrame, index "Symbol", cols "Name",
                       "Holding Percent"
      equity_holdings  DataFrame, index "Average", cols <symbol>,
                       "Category Average"
      bond_holdings    DataFrame, same shape
      sector_weightings / asset_classes / bond_ratings   plain dicts
    """
    out: dict[str, Any] = {
        "top_holdings": [],
        "holdings_reported": 0,
        "sectors": [],
        "equity_pct": None,
        "bond_pct": None,
        "cash_pct": None,
        "other_pct": None,
        "top10_weight": None,
        "bond_details": {},
        "bond_ratings": {},
        "equity_details": {},
        "category_expense_ratio": None,
        "turnover": None,
        "legal_type": None,
    }

    if funds_data is None:
        return out

    # Top holdings
    try:
        th = funds_data.top_holdings
        if isinstance(th, pd.DataFrame) and not th.empty:
            holdings = []

            for sym, row in th.iterrows():
                name = row.get("Name")
                pct = row.get("Holding Percent")

                try:
                    weight = float(pct)
                except (TypeError, ValueError):
                    weight = None

                if weight is None or math.isnan(weight):
                    continue

                holdings.append(
                    {
                        "symbol": "" if sym is None else str(sym).strip(),
                        "name": "" if name is None else str(name).strip(),
                        "pct": weight,
                    }
                )

            holdings.sort(key=lambda h: h["pct"], reverse=True)
            out["top_holdings"] = holdings[:15]
            out["holdings_reported"] = len(holdings)

            # Only a genuine top-10 sum; a fund that disclosed 5 names must
            # not look less concentrated than one that disclosed 10.
            if len(holdings) >= 10:
                out["top10_weight"] = sum(h["pct"] for h in holdings[:10])

    except Exception:
        pass

    # Sector weights
    try:
        sw = funds_data.sector_weightings
        if isinstance(sw, dict) and sw:
            sectors = []
            for k, v in sw.items():
                try:
                    sectors.append({"sector": fmt_sector(k), "pct": float(v)})
                except (TypeError, ValueError):
                    pass
            out["sectors"] = sorted(sectors, key=lambda x: x["pct"], reverse=True)
    except Exception:
        pass

    # Asset class mix
    try:
        ac = funds_data.asset_classes
        if isinstance(ac, dict) and ac:
            out["equity_pct"] = ac.get("stockPosition")
            out["bond_pct"] = ac.get("bondPosition")
            out["cash_pct"] = ac.get("cashPosition")

            other = 0.0
            for k in ("preferredPosition", "convertiblePosition", "otherPosition"):
                v = ac.get(k)
                if v:
                    other += float(v)
            out["other_pct"] = other or None
    except Exception:
        pass

    # Bond aggregates
    try:
        bh = funds_data.bond_holdings
        for key, label in (
            ("Duration", "Eff. Duration (yrs)"),
            ("Maturity", "Eff. Maturity (yrs)"),
            ("Credit Quality", "Avg Credit Quality"),
        ):
            val = _df_lookup(bh, key, symbol)
            if val is not None:
                out["bond_details"][label] = (val, _df_category_avg(bh, key))
    except Exception:
        pass

    # Bond ratings
    try:
        br = funds_data.bond_ratings
        if isinstance(br, dict) and br:
            out["bond_ratings"] = {
                k: float(v) for k, v in br.items() if v is not None
            }
    except Exception:
        pass

    # Equity aggregates
    try:
        eh = funds_data.equity_holdings
        for key, label in (
            ("Price/Earnings", "P/E (aggregate)"),
            ("Price/Book", "P/B (aggregate)"),
            ("Price/Sales", "P/S (aggregate)"),
            ("Median Market Cap", "Median Mkt Cap"),
            ("3 Year Earnings Growth", "EPS Growth (3Y)"),
        ):
            val = _df_lookup(eh, key, symbol)
            if val is not None:
                out["equity_details"][label] = (val, _df_category_avg(eh, key))
    except Exception:
        pass

    # Fund operations — the peer-group expense ratio lives here.
    try:
        fo = funds_data.fund_operations
        cat_er = _df_category_avg(fo, "Annual Report Expense Ratio")
        out["category_expense_ratio"] = normalize_rate(cat_er)
        out["turnover"] = normalize_rate(
            _df_lookup(fo, "Annual Holdings Turnover", symbol)
        )
    except Exception:
        pass

    try:
        fov = funds_data.fund_overview
        if isinstance(fov, dict):
            out["legal_type"] = fov.get("legalType")
    except Exception:
        pass

    return out


# ---------------------------------------------------------------------------
# METRICS
# ---------------------------------------------------------------------------

def calc_metrics(d: dict[str, Any]) -> dict[str, Any]:
    """Build normalized ETF metrics."""
    info = d["info"]
    funds_data = d["funds_data"]
    hist = d["hist"]

    m: dict[str, Any] = {"warnings": [], "notes": []}

    m["ticker"] = d["ticker"]
    m["name"] = info.get("longName") or info.get("shortName")
    m["fund_family"] = info.get("fundFamily") or info.get("issuer")
    m["category"] = info.get("category")
    m["currency"] = (info.get("currency") or "").upper()
    m["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    m["nav"] = info.get("navPrice")
    m["aum"] = info.get("totalAssets")

    m["expense_ratio"] = normalize_rate(
        info.get("annualReportExpenseRatio")
        or info.get("totalExpenseRatio")
        or info.get("netExpenseRatio")
    )

    # 0.0 is a real yield, not a missing one — an "or" chain would discard
    # it and hand a zero-yield fund a neutral score.
    raw_yield = info.get("yield")
    if raw_yield is None:
        raw_yield = info.get("trailingAnnualDividendYield")
    m["yield"] = normalize_rate(raw_yield)

    if info.get("beta3Year") is not None:
        m["beta"] = info.get("beta3Year")
        m["beta_label"] = "Beta (3Y)"
    elif info.get("beta") is not None:
        m["beta"] = info.get("beta")
        m["beta_label"] = "Beta (5Y monthly)"
    else:
        m["beta"] = None
        m["beta_label"] = "Beta"

    m["num_holdings"] = info.get("holdings") or info.get("totalHoldings")

    # Yahoo's own stated returns. Kept strictly separate from the computed
    # series: different methodology and as-of date, so they must never be
    # differenced against a computed benchmark number.
    m["yahoo_returns"] = {
        "YTD": as_float(info.get("ytdReturn")),
        "1Y": as_float(info.get("oneYearTotalReturn")),
        "3Y": as_float(info.get("threeYearAverageReturn")),
        "5Y": as_float(info.get("fiveYearAverageReturn")),
    }
    m["yahoo_returns"] = {k: v for k, v in m["yahoo_returns"].items() if v is not None}

    m["52w_high"] = info.get("fiftyTwoWeekHigh")
    m["52w_low"] = info.get("fiftyTwoWeekLow")

    px = m["price"]
    if px and m["52w_high"] and m["52w_low"] and m["52w_high"] != m["52w_low"]:
        m["pos_52w"] = (px - m["52w_low"]) / (m["52w_high"] - m["52w_low"])
    else:
        m["pos_52w"] = None

    m["etf_type"] = detect_etf_type(info)
    m["nav_premium"] = calc_nav_premium(info)

    rfr, rfr_src = get_risk_free_rate(m["currency"], hist)
    m["rfr"] = rfr
    m["rfr_source"] = rfr_src

    m["returns"] = calc_returns(hist, rfr=rfr)
    m["holdings"] = extract_holdings(funds_data, symbol=d["ticker"])

    if m["holdings"].get("category_expense_ratio"):
        m["category_expense_ratio"] = m["holdings"]["category_expense_ratio"]
    else:
        m["category_expense_ratio"] = None

    build_warnings(m)

    return m


def build_warnings(m: dict[str, Any]) -> None:
    """Attach warnings and notes. Mutates m."""
    r = m.get("returns") or {}

    if m["etf_type"] == "leveraged_inverse":
        m["warnings"].append(
            "LEVERAGED/INVERSE ETF: daily reset creates compounding decay. "
            "Unsuitable for passive long-term holding."
        )

    # NAV premium is only flagged when it exceeds what a single session's
    # move could explain, since navPrice commonly lags by a day.
    nav_prem = m.get("nav_premium")
    if nav_prem is not None:
        vol = r.get("ann_vol")
        daily_vol = (vol / math.sqrt(252)) if vol else 0.0
        threshold = max(0.005, 2.0 * daily_vol)

        if abs(nav_prem) > threshold:
            sign = "+" if nav_prem > 0 else ""
            label = "premium" if nav_prem > 0 else "discount"
            m["warnings"].append(
                f"Trading at {sign}{nav_prem * 100:.2f}% {label} to last published NAV "
                f"(beyond the {threshold * 100:.2f}% a single session's move explains)."
            )
        else:
            m["notes"].append(
                f"Price/NAV gap of {nav_prem * 100:+.2f}% is within one session's "
                "normal move; Yahoo's NAV typically lags by a session."
            )

    aum = m.get("aum")
    if aum:
        if aum < 50_000_000:
            m["warnings"].append(
                f"Small AUM ({fmt_money(aum)}): elevated closure and liquidity risk."
            )
        elif aum < 250_000_000:
            m["notes"].append(
                f"Modest AUM ({fmt_money(aum)}): monitor ETF viability over time."
            )

    dv = r.get("dollar_volume")
    if dv is not None and dv < 1_000_000:
        m["warnings"].append(
            f"Thin trading (median {fmt_money(dv)}/day): expect wider spreads; "
            "use limit orders."
        )

    er = m.get("expense_ratio")
    if er is not None:
        # Ordered most-severe-first; the reverse ordering makes the second
        # branch unreachable.
        if m["etf_type"] == "leveraged_inverse":
            if er > 0.0125:
                m["warnings"].append(
                    f"High expense ratio for a leveraged product ({er * 100:.2f}%)."
                )
        elif er > 0.0100:
            m["warnings"].append(
                f"Very high expense ratio ({er * 100:.2f}%): evaluate whether "
                "returns justify cost."
            )
        elif er > 0.0075:
            m["warnings"].append(
                f"High expense ratio ({er * 100:.2f}%): meaningful compounding "
                "drag on returns."
            )

        cat_er = m.get("category_expense_ratio")
        if cat_er and cat_er > 0:
            ratio = er / cat_er
            if ratio > 1.5:
                m["warnings"].append(
                    f"Costs {ratio:.1f}x its category average "
                    f"({er * 100:.2f}% vs {cat_er * 100:.2f}%) — check for a "
                    "cheaper fund tracking the same exposure."
                )
            elif ratio < 0.6:
                m["notes"].append(
                    f"{1 / ratio:.1f}x cheaper than the category average "
                    f"({er * 100:.2f}% vs {cat_er * 100:.2f}%)."
                )

    hold = m["holdings"]
    th = hold["top_holdings"]
    m["top1_pct"] = th[0]["pct"] if th else None
    m["top10_pct"] = hold["top10_weight"]

    if m["top1_pct"] is not None:
        etype = m["etf_type"]
        top1 = m["top1_pct"]

        if etype == "equity_broad" and top1 > 0.10:
            m["warnings"].append(
                f"Top holding is {top1 * 100:.1f}% of fund — elevated "
                "single-stock concentration."
            )
        elif etype in ("equity_sector", "equity_factor") and top1 > 0.20:
            m["warnings"].append(
                f"Top holding is {top1 * 100:.1f}% of fund — high concentration."
            )
        elif etype == "equity_thematic" and top1 > 0.25:
            m["warnings"].append(
                f"Top holding is {top1 * 100:.1f}% of fund — high concentration "
                "for thematic ETF."
            )

    if m["etf_type"] == "commodity":
        name_lc = _norm(m["name"])
        if _has(name_lc, ["futures", "oil", "gas", "crude"]):
            m["notes"].append(
                "Futures-based commodity ETF: subject to contango/roll cost drag. "
                "Compare to physically-backed alternatives where available."
            )

    legal = (hold.get("legal_type") or "").lower()
    if "partnership" in legal or "commodity pool" in legal:
        m["notes"].append(
            f"Legal structure is '{hold['legal_type']}' — may issue a K-1 "
            "rather than a 1099. Check the tax treatment before buying."
        )

    hist_days = r.get("history_days")
    if hist_days is not None and hist_days < 400:
        m["notes"].append(
            f"Only {hist_days} days of price history: return, volatility and "
            "drawdown figures cover less than a full year."
        )

    if m.get("currency") and m["currency"] != "USD":
        m["notes"].append(
            f"Fund is denominated in {m['currency']}. All returns and risk "
            "figures are in that currency, and the benchmark is a "
            f"{m['currency']}-listed proxy."
        )


# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------
# Every dimension returns (score, coverage). Coverage is the fraction of
# the dimension actually backed by data: 0.0 means "not measured" and the
# weight is redistributed rather than a neutral 5.0 being invented.

Dim = tuple[float, float]


def s_cost(m: dict[str, Any]) -> Dim:
    er = m.get("expense_ratio")

    if er is None:
        return 5.0, 0.0

    t = m["etf_type"]

    if t == "leveraged_inverse":
        absolute = score_val(er, 0.005, 0.020, higher=False)
    elif t == "equity_broad":
        absolute = score_val(er, 0.0003, 0.0075, higher=False)
    elif t == "fixed_income":
        absolute = score_val(er, 0.0005, 0.0075, higher=False)
    else:
        absolute = score_val(er, 0.0010, 0.0100, higher=False)

    # Relative to peers where Yahoo supplies a category average. The
    # actionable question is not "is 0.09% low" but "is anyone delivering
    # this exposure for less".
    cat_er = m.get("category_expense_ratio")
    if cat_er and cat_er > 0:
        relative = score_val(er / cat_er, 0.5, 1.5, higher=False)
        return (absolute + relative) / 2.0, 1.0

    return absolute, 0.75


def s_liquidity(m: dict[str, Any]) -> Dim:
    """
    Fund size and actual traded volume, both on a log scale.

    AUM alone is a size measure, not a liquidity measure, and a linear
    dollar scale makes $100M and $1B nearly indistinguishable from zero.
    """
    scores, weight = [], 0.0

    aum = m.get("aum")
    if aum:
        s = score_log(aum, 10e9, 50e6)
        if s is not None:
            scores.append(s)
            weight += 0.5

    dv = (m.get("returns") or {}).get("dollar_volume")
    if dv:
        s = score_log(dv, 50e6, 250e3)
        if s is not None:
            scores.append(s)
            weight += 0.5

    if not scores:
        return 5.0, 0.0

    return sum(scores) / len(scores), weight


def s_structure(m: dict[str, Any], track_diff: Optional[float]) -> Dim:
    scores, weight = [], 0.0

    nav_premium = m.get("nav_premium")
    if nav_premium is not None:
        # Bands widened from the original 0.1%/1.5%: a stale NAV alone can
        # produce a gap of one session's move.
        scores.append(score_val(abs(nav_premium), 0.003, 0.025, higher=False))
        weight += 0.6

    # Only scored where the category proxy stands in credibly for the
    # fund's real index. A sector fund diverging from the S&P 500 is doing
    # its job, not failing at structure.
    if track_diff is not None and m["etf_type"] in PROXY_IS_CREDIBLE:
        if m["etf_type"] == "fixed_income":
            scores.append(score_val(track_diff, 0.010, 0.060, higher=False))
        else:
            scores.append(score_val(track_diff, 0.005, 0.040, higher=False))
        weight += 0.4

    if not scores:
        return 5.0, 0.0

    return sum(scores) / len(scores), min(weight, 1.0)


def s_risk(m: dict[str, Any]) -> Dim:
    """
    Pure risk: realized volatility and worst drawdown.

    Sharpe is deliberately excluded here — it is a risk-ADJUSTED RETURN
    measure and lives in Performance. Scoring it in both dimensions gave
    one noisy statistic roughly 0.08 of the composite on top of the vol and
    return it already contains.
    """
    r = m.get("returns") or {}
    t = m["etf_type"]
    scores = []

    vol = r.get("ann_vol")
    if vol is not None:
        if t == "leveraged_inverse":
            scores.append(score_val(vol, 0.20, 0.90, higher=False))
        elif t == "fixed_income":
            scores.append(score_val(vol, 0.02, 0.15, higher=False))
        elif t in ("equity_thematic", "commodity"):
            scores.append(score_val(vol, 0.15, 0.55, higher=False))
        else:
            scores.append(score_val(vol, 0.10, 0.45, higher=False))

    md = r.get("max_drawdown")
    if md is not None:
        if t == "fixed_income":
            scores.append(score_val(abs(md), 0.02, 0.20, higher=False))
        else:
            scores.append(score_val(abs(md), 0.05, 0.45, higher=False))

    if not scores:
        return 5.0, 0.0

    return sum(scores) / len(scores), 1.0 if len(scores) == 2 else 0.5


def s_income(m: dict[str, Any]) -> Dim:
    """
    Distribution yield, scored only where income is part of the mandate.

    For a broad equity or thematic fund the distribution is already inside
    total return, and rewarding a high yield just rewards a value tilt.
    """
    t = m["etf_type"]

    if t not in INCOME_RELEVANT:
        return 5.0, 0.0

    yld = m.get("yield")
    if yld is None:
        return 5.0, 0.0

    if t == "fixed_income":
        return score_val(yld, 0.05, 0.00, higher=True), 1.0
    if t == "real_estate":
        return score_val(yld, 0.04, 0.00, higher=True), 1.0

    return score_val(yld, 0.03, 0.00, higher=True), 1.0


def s_performance(
    m: dict[str, Any],
    bm_ret: Optional[dict[str, Any]],
    currency_matched: bool,
) -> Dim:
    """
    Backward-looking. Weighted toward performance RELATIVE to the category
    proxy rather than raw trailing return, because raw trailing return is a
    momentum signal — rating a fund highly for having already gone up.
    """
    r = m.get("returns") or {}
    parts: list[tuple[float, float]] = []  # (score, weight)

    etf_1y = r.get("1y")
    bm_1y = (bm_ret or {}).get("1y")

    if etf_1y is not None:
        parts.append((score_val(etf_1y, 0.20, -0.10, higher=True), 1.0))

        # Excess return is only meaningful if both legs are in the same
        # currency; otherwise it is largely an FX move.
        if bm_1y is not None and currency_matched:
            parts.append((score_val(etf_1y - bm_1y, 0.02, -0.05, higher=True), 2.0))

    sharpe = r.get("sharpe")
    if sharpe is not None:
        parts.append((score_val(sharpe, 1.5, 0.0, higher=True), 1.0))

    if not parts:
        return 5.0, 0.0

    total_w = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_w

    return score, min(total_w / 4.0, 1.0)


def s_concentration(m: dict[str, Any]) -> Dim:
    t = m["etf_type"]

    if t not in CONCENTRATION_RELEVANT:
        return 5.0, 0.0

    top1 = m.get("top1_pct")
    top10 = m.get("top10_pct")
    scores, weight = [], 0.0

    if top1 is not None:
        if t == "equity_broad":
            scores.append(score_val(top1, 0.03, 0.15, higher=False))
        elif t in ("equity_sector", "equity_factor"):
            scores.append(score_val(top1, 0.08, 0.30, higher=False))
        else:
            scores.append(score_val(top1, 0.10, 0.40, higher=False))
        weight += 0.4

    if top10 is not None:
        if t == "equity_broad":
            scores.append(score_val(top10, 0.20, 0.65, higher=False))
        else:
            scores.append(score_val(top10, 0.40, 0.90, higher=False))
        weight += 0.6

    if not scores:
        return 5.0, 0.0

    return sum(scores) / len(scores), weight


DIM_NAMES = [
    "Cost",
    "Liquidity",
    "Structure",
    "Risk",
    "Income",
    "Performance",
    "Concentration",
]


def build_scores(
    m: dict[str, Any],
    track_diff: Optional[float],
    bm_ret: Optional[dict[str, Any]],
    currency_matched: bool = True,
) -> dict[str, Any]:
    """
    Coverage-weighted composite.

    Unmeasured dimensions have their weight redistributed across what was
    measured, rather than contributing an invented neutral 5.0. If total
    coverage falls below MIN_COVERAGE no composite is emitted at all.
    """
    t = m.get("etf_type", "equity_broad")
    weights = ETF_WEIGHTS.get(t, ETF_WEIGHTS["equity_broad"])

    raw: list[Dim] = [
        s_cost(m),
        s_liquidity(m),
        s_structure(m, track_diff),
        s_risk(m),
        s_income(m),
        s_performance(m, bm_ret, currency_matched),
        s_concentration(m),
    ]

    dims: dict[str, Optional[float]] = {}
    for i, name in enumerate(DIM_NAMES):
        score, cov = raw[i]
        dims[name] = round(score, 1) if cov > 0 else None

    effective = [weights[i] * raw[i][1] for i in range(7)]
    total_w = sum(effective)

    # Coverage relative to the full weight vector, so a missing Cost
    # (weight 0.28) costs far more coverage than a missing Income (0.06).
    coverage = total_w / sum(weights) if sum(weights) else 0.0

    if total_w <= 0 or coverage < MIN_COVERAGE:
        return {
            "composite": None,
            "dims": dims,
            "coverage": coverage,
            "rating": "INSUFFICIENT DATA",
            "measured": [DIM_NAMES[i] for i in range(7) if raw[i][1] > 0],
            "missing": [DIM_NAMES[i] for i in range(7) if raw[i][1] <= 0],
        }

    composite = sum(raw[i][0] * effective[i] for i in range(7)) / total_w

    if t == "leveraged_inverse":
        composite = min(composite, 6.5)

    # One decimal: rating bands are a full point wide and the inputs are
    # far noisier than 0.01.
    composite = round(max(0.0, min(10.0, composite)), 1)

    return {
        "composite": composite,
        "dims": dims,
        "coverage": coverage,
        "rating": rate(composite, m),
        "measured": [DIM_NAMES[i] for i in range(7) if raw[i][1] > 0],
        "missing": [DIM_NAMES[i] for i in range(7) if raw[i][1] <= 0],
    }


def rate(comp: Optional[float], m: dict[str, Any]) -> str:
    if comp is None:
        return "INSUFFICIENT DATA"

    if m.get("etf_type") == "leveraged_inverse":
        return "SPECULATIVE — LEVERAGED / INVERSE INSTRUMENT"

    if comp >= 8.5:
        return "EXCELLENT IMPLEMENTATION OF ITS CATEGORY"
    if comp >= 7.5:
        return "STRONG IMPLEMENTATION"
    if comp >= 6.5:
        return "SOLID — MINOR CONCERNS"
    if comp >= 5.5:
        return "ACCEPTABLE — REVIEW CLOSELY"
    if comp >= 4.0:
        return "WEAK — SIGNIFICANT CONCERNS"

    return "POOR — LIKELY BETTER ALTERNATIVES"


# ---------------------------------------------------------------------------
# NATURAL-LANGUAGE SUMMARY
# ---------------------------------------------------------------------------

def generate_summary(
    m: dict[str, Any],
    sc: dict[str, Any],
    bm_ret: Optional[dict[str, Any]],
    bm_tick: str,
    currency_matched: bool,
) -> str:
    t = m["etf_type"]
    label = ETF_TYPE_LABELS.get(t, "ETF").lower()
    er = m.get("expense_ratio")
    aum = m.get("aum")
    comp = sc["composite"]
    r = m.get("returns") or {}

    parts = []

    if comp is None:
        parts.append(
            f"{m['name']} could not be scored: Yahoo Finance supplied only "
            f"{sc['coverage'] * 100:.0f}% of the data the composite needs "
            f"(missing {', '.join(sc['missing'])})."
        )
        parts.append(
            "This reflects data coverage, not fund quality — check the "
            "issuer's factsheet directly."
        )
        return " ".join(parts)

    if comp >= 8.5:
        parts.append(f"{m['name']} is a benchmark-quality {label} ETF.")
    elif comp >= 7.0:
        parts.append(
            f"{m['name']} is a solid {label} ETF with strong overall characteristics."
        )
    elif comp >= 5.5:
        parts.append(
            f"{m['name']} is an acceptable {label} ETF with some concerns "
            "worth monitoring."
        )
    else:
        parts.append(
            f"{m['name']} has material weaknesses that warrant careful consideration."
        )

    if er is not None:
        cat_er = m.get("category_expense_ratio")
        if cat_er and cat_er > 0:
            ratio = er / cat_er
            if ratio < 0.7:
                parts.append(
                    f"At {er * 100:.2f}% it undercuts its category average of "
                    f"{cat_er * 100:.2f}%."
                )
            elif ratio > 1.3:
                parts.append(
                    f"Its {er * 100:.2f}% fee is above the {cat_er * 100:.2f}% "
                    "category average — a cheaper equivalent may exist."
                )
            else:
                parts.append(
                    f"Its {er * 100:.2f}% fee is in line with the category "
                    f"average of {cat_er * 100:.2f}%."
                )
        elif er < 0.0010:
            parts.append(f"The {er * 100:.2f}% expense ratio is exceptionally low.")
        elif er < 0.0040:
            parts.append(f"At {er * 100:.2f}%, the expense ratio is competitive.")
        elif er > 0.0080:
            parts.append(
                f"The {er * 100:.2f}% expense ratio creates a meaningful "
                "performance drag."
            )

    dv = r.get("dollar_volume")
    if aum and aum > 10e9 and dv and dv > 50e6:
        parts.append(
            f"With {fmt_money(aum)} in AUM and {fmt_money(dv)} median daily "
            "turnover, liquidity is excellent."
        )
    elif aum and aum < 250e6:
        parts.append(f"AUM of {fmt_money(aum)} is modest — monitor for viability risk.")
    elif dv is not None and dv < 1e6:
        parts.append(
            f"Median daily turnover of {fmt_money(dv)} is thin; spreads may be wide."
        )

    etf_1y = r.get("1y")
    bm_1y = (bm_ret or {}).get("1y")

    if etf_1y is not None and bm_1y is not None and currency_matched:
        alpha = etf_1y - bm_1y

        if abs(alpha) < 0.005:
            parts.append(f"1Y total return closely tracks {bm_tick}.")
        elif alpha > 0:
            parts.append(
                f"1Y return of {etf_1y * 100:.1f}% outpaces {bm_tick} by "
                f"{alpha * 100:.1f}pp."
            )
        else:
            parts.append(
                f"1Y return of {etf_1y * 100:.1f}% trails {bm_tick} by "
                f"{abs(alpha) * 100:.1f}pp."
            )
    elif etf_1y is not None:
        parts.append(f"1Y total return was {etf_1y * 100:.1f}%.")

    if sc["missing"]:
        parts.append(
            f"Scored on {sc['coverage'] * 100:.0f}% data coverage; "
            f"{', '.join(sc['missing'])} could not be measured."
        )

    if t == "leveraged_inverse":
        parts.append(
            "Daily leverage reset causes compounding decay in sideways markets — "
            "unsuitable as a passive long-term position."
        )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# REPORT PRINTING
# ---------------------------------------------------------------------------

def _print_solo_returns(r: dict[str, Any]) -> None:
    """Return table without a benchmark column."""
    print(f"  {'Period':<14}  {'ETF':>9}")
    print("  " + "-" * 27)
    for label, key in (("YTD", "ytd"), ("1Y", "1y"),
                       ("3Y Ann", "3y_ann"), ("5Y Ann", "5y_ann")):
        val = r.get(key)
        shown = f"{val * 100:+.1f}%" if val is not None else "  N/A "
        print(f"  {label:<14}  {shown:>9}")


def print_report(
    ticker: str,
    m: dict[str, Any],
    sc: dict[str, Any],
    track_diff: Optional[float],
    bm_ret: Optional[dict[str, Any]],
    bm_tick: str,
    bm_label: str,
    currency_matched: bool,
) -> None:
    W = 64

    def rule(ch: str = "=") -> None:
        print("  " + ch * W)

    def h(text: str) -> None:
        print(f"  {B}{text}{X}")

    def row(label: str, val: Any, width: int = 28) -> None:
        print(f"  {label:<{width}}  {val}")

    def wrap(text: str, width: int = 60) -> None:
        line = ""
        for word in text.split():
            if len(line) + len(word) + 1 > width:
                print(f"  {line}")
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            print(f"  {line}")

    print()
    rule()
    print(f"  {B}{C}{m['name']} ({ticker}){X}")

    type_label = ETF_TYPE_LABELS.get(m["etf_type"], "ETF")
    print(
        f"  {type_label}  ·  {m.get('category') or 'N/A'}  ·  "
        f"{m.get('fund_family') or 'N/A'}"
    )

    px_str = f"{m['price']:.2f} {m.get('currency')}" if m.get("price") else "N/A"
    er_str = (
        f"{m['expense_ratio'] * 100:.3f}%" if m.get("expense_ratio") is not None else "N/A"
    )

    print(f"  {px_str}  ·  AUM: {fmt_money(m.get('aum'))}  ·  ER: {er_str}")
    rule()

    comp = sc["composite"]
    cov = sc["coverage"]

    if comp is None:
        print(f"\n  {B}COMPOSITE SCORE{X}   {Y}NOT SCORED{X}")
        print(f"  Rating: {B}{sc['rating']}{X}")
    else:
        print(f"\n  {B}COMPOSITE SCORE{X}   {colour(comp)} / 10   {bar(comp, 18)}")
        print(f"  Rating: {B}{sc['rating']}{X}")

    cov_c = G if cov >= 0.85 else (Y if cov >= MIN_COVERAGE else R)
    print(f"  Data coverage: {cov_c}{cov * 100:.0f}%{X}", end="")
    print(f"   (missing: {', '.join(sc['missing'])})" if sc["missing"] else "")

    print(
        f"  {C}Grades implementation quality within {type_label} — not whether{X}"
    )
    print(f"  {C}that category belongs in your portfolio.{X}")

    if m.get("warnings"):
        print()
        rule("-")
        h("WARNINGS")
        rule("-")
        for warning in m["warnings"]:
            print(f"  {Y}⚠{X}  {warning}")

    print()
    rule("-")
    h("DIMENSION BREAKDOWN")
    rule("-")
    for dim, val in sc["dims"].items():
        if val is None:
            print(f"  {dim:<16}  {bar(None, 14)}  {Y}not measured{X}")
        else:
            print(f"  {dim:<16}  {bar(val, 14)}  {colour(val)}")

    print()
    rule("-")
    h("SUMMARY")
    rule("-")
    wrap(generate_summary(m, sc, bm_ret, bm_tick, currency_matched))

    print()
    rule("-")
    h("ETF OVERVIEW")
    rule("-")

    row("Type", type_label)
    row("Fund Family", m.get("fund_family") or "N/A")
    row("Category", m.get("category") or "N/A")
    row("Legal Structure", m["holdings"].get("legal_type") or "N/A")
    row("AUM", fmt_money(m.get("aum")))
    row("Holdings Count", str(m["num_holdings"]) if m.get("num_holdings") else "N/A")

    cat_er = m.get("category_expense_ratio")
    er_line = er_str
    if cat_er:
        er_line += f"   (category avg {cat_er * 100:.3f}%)"
    row("Expense Ratio", er_line)

    turnover = m["holdings"].get("turnover")
    if turnover is not None:
        row("Annual Turnover", pct_str(turnover, 1))

    row(
        "Distribution Yield",
        pct_str(m.get("yield"), 2) if m.get("yield") is not None else "N/A",
    )

    dv = (m.get("returns") or {}).get("dollar_volume")
    row("Median Daily Volume", f"{fmt_money(dv)}/day" if dv else "N/A")

    nav_str = f"{m['nav']:.2f}" if m.get("nav") else "N/A"
    if m.get("nav_premium") is not None:
        sign = "+" if m["nav_premium"] > 0 else ""
        label = "premium" if m["nav_premium"] > 0 else "discount"
        nav_str += f"  ({sign}{m['nav_premium'] * 100:.2f}% {label}, NAV may lag 1 session)"

    row("NAV", nav_str)
    row(m.get("beta_label", "Beta"), f"{m['beta']:.2f}" if m.get("beta") else "N/A")

    eq_det = m["holdings"].get("equity_details") or {}
    if eq_det:
        print(f"  {B}— Aggregate Fundamentals (fund vs category) —{X}")
        for label, (val, cat) in eq_det.items():
            if "Growth" in label:
                shown = pct_str(val, 1)
                cat_s = f"vs {pct_str(cat, 1)}" if cat is not None else ""
            elif "Cap" in label:
                shown = fmt_money(val)
                cat_s = f"vs {fmt_money(cat)}" if cat is not None else ""
            else:
                shown = f"{val:.2f}"
                cat_s = f"vs {cat:.2f}" if cat is not None else ""
            row(label, f"{shown}  {cat_s}".strip())

    holdings = m["holdings"]
    top_h = holdings.get("top_holdings") or []
    sectors = holdings.get("sectors") or []

    print()
    rule("-")
    h("HOLDINGS BREAKDOWN")
    rule("-")

    if top_h:
        top10_w = holdings.get("top10_weight")
        top10_str = f" ({top10_w * 100:.1f}% of fund)" if top10_w else ""
        shown = min(len(top_h), 10)
        print(f"  {B}Top {shown} Holdings{top10_str}:{X}")
        print(f"  {'#':<4}{'Sym':<8}{'Name':<36}{'Wt':>6}")
        print("  " + "-" * 54)

        for i, hi in enumerate(top_h[:10], 1):
            sym = (hi.get("symbol") or "")[:7]
            name = hi.get("name") or ""
            name = name[:33] + "..." if len(name) > 36 else name
            wt = hi.get("pct") or 0
            print(f"  {i:<4}{sym:<8}{name:<36}{wt * 100:>5.1f}%")

        if holdings.get("holdings_reported", 0) < 10:
            print(
                f"  {Y}Only {holdings['holdings_reported']} holdings disclosed; "
                f"top-10 weight not computed.{X}"
            )
    else:
        print(f"  {Y}Holdings data not available from Yahoo Finance.{X}")

    if sectors:
        print()
        print(f"  {B}Sector / Category Weights:{X}")
        print(f"  {'Sector':<34}  {'Wt':>6}")
        print("  " + "-" * 58)

        scale = max((s.get("pct") or 0) for s in sectors) or 0.40

        for s in sectors[:12]:
            wt = s.get("pct") or 0
            if wt < 0.001:
                continue
            print(
                f"  {(s.get('sector') or ''):<34}  {wt * 100:>5.1f}%  "
                f"{bar_pct(wt, max_val=scale, width=16)}"
            )

    eq = holdings.get("equity_pct")
    bd = holdings.get("bond_pct")
    ca = holdings.get("cash_pct")
    ot = holdings.get("other_pct")

    if any(x is not None for x in [eq, bd, ca, ot]):
        print()
        print(f"  {B}Asset Mix:{X}")
        for name, val, scale in (
            ("Equities", eq, 1.0),
            ("Bonds", bd, 1.0),
            ("Cash", ca, 0.10),
            ("Other", ot, 0.10),
        ):
            if val is not None:
                print(f"  {name:<24}  {val * 100:>5.1f}%  {bar_pct(val, scale, 16)}")

    bond_det = holdings.get("bond_details") or {}
    bond_rat = holdings.get("bond_ratings") or {}

    if bond_det or bond_rat:
        print()
        print(f"  {B}Fixed Income Detail (fund vs category):{X}")

        for label, (val, cat) in bond_det.items():
            cat_s = f"vs {cat:.2f}" if cat is not None else ""
            print(f"  {label:<28}  {val:.2f}  {cat_s}")

        if bond_rat:
            print()
            print(f"  {B}Credit Quality Breakdown:{X}")
            for rating, weight in sorted(
                bond_rat.items(), key=lambda x: x[1], reverse=True
            )[:8]:
                if weight and weight > 0.001:
                    print(
                        f"  {fmt_sector(rating):<14}  {weight * 100:>5.1f}%  "
                        f"{bar_pct(weight, 0.50, 14)}"
                    )

    r = m.get("returns") or {}

    print()
    rule("-")
    h("RISK & PERFORMANCE")
    rule("-")

    vol = r.get("ann_vol")
    sharpe = r.get("sharpe")
    md = r.get("max_drawdown")
    hist_days = r.get("history_days")

    if hist_days:
        print(f"  {'Window':<28}  {hist_days / 365.25:.1f} years of daily history")

    if vol is not None:
        print(f"  {'Volatility (ann.)':<28}  {vol * 100:.1f}%")

    if md is not None:
        md_c = G if abs(md) < 0.10 else (Y if abs(md) < 0.20 else R)
        print(f"  {'Max Drawdown':<28}  {md_c}{md * 100:.1f}%{X}")

    if r.get("cagr") is not None:
        print(f"  {'CAGR over window':<28}  {r['cagr'] * 100:+.1f}%")

    if sharpe is not None:
        sh_c = G if sharpe >= 1.2 else (Y if sharpe >= 0.7 else R)
        print(f"  {'Sharpe (geometric)':<28}  {sh_c}{sharpe:.2f}{X}")
        print(
            f"  {'  risk-free rate used':<28}  {m['rfr'] * 100:.2f}% "
            f"({m['rfr_source']})"
        )

    if track_diff is not None:
        scored = m["etf_type"] in PROXY_IS_CREDIBLE
        td_c = G if track_diff < 0.01 else (Y if track_diff < 0.05 else R)
        note = "" if scored else "  (informational — proxy is not this fund's index)"
        print(
            f"  {'Deviation vs ' + bm_tick + ' (ann.)':<28}  "
            f"{td_c}{track_diff * 100:.2f}%{X}{note}"
        )

    print()
    if not currency_matched:
        print(
            f"  {Y}No {m.get('currency')}-listed benchmark available; comparison "
            f"omitted to avoid FX contamination.{X}"
        )
        _print_solo_returns(r)
    elif bm_ret is None:
        print(f"  {C}No benchmark comparison for this fund.{X}")
        _print_solo_returns(r)
    else:
        print(f"  Total returns, computed from adjusted closes. Benchmark: {bm_label}")
        print(f"  {'Period':<14}  {'ETF':>9}  {bm_tick:>10}  {'Diff':>9}")
        print("  " + "-" * 45)

        def ret_row(label: str, key: str) -> None:
            etf_val = r.get(key)
            bm_val = (bm_ret or {}).get(key)

            e = f"{etf_val * 100:+.1f}%" if etf_val is not None else "  N/A "
            bmk = f"{bm_val * 100:+.1f}%" if bm_val is not None else "  N/A "

            if etf_val is not None and bm_val is not None:
                diff = etf_val - bm_val
                d_c = G if diff > 0.01 else (R if diff < -0.01 else Y)
                d = f"{d_c}{diff * 100:+.1f}%{X}"
            else:
                d = "  N/A "

            print(f"  {label:<14}  {e:>9}  {bmk:>10}  {d:>9}")

        ret_row("YTD", "ytd")
        ret_row("1Y", "1y")
        ret_row("3Y Ann", "3y_ann")
        ret_row("5Y Ann", "5y_ann")

    # Yahoo's own stated returns are shown separately: different
    # methodology and as-of date, so they are never differenced against
    # the computed benchmark figures above.
    yr = m.get("yahoo_returns") or {}
    if yr:
        print()
        print(f"  {B}Yahoo-reported returns{X} (different methodology — not compared):")
        print("  " + "   ".join(f"{k} {v * 100:+.1f}%" for k, v in yr.items()))

    if m.get("52w_high") and m.get("52w_low"):
        print()
        print(f"  {'52W High':<28}  {m['52w_high']:.2f}")
        print(f"  {'52W Low':<28}  {m['52w_low']:.2f}")
        if m.get("pos_52w") is not None:
            print(f"  {'Position in 52W Range':<28}  {m['pos_52w'] * 100:.1f}%")

    if m.get("notes"):
        print()
        rule("-")
        h("NOTES")
        rule("-")
        for note in m["notes"]:
            wrap("• " + note)

    print()
    rule()
    print(f"  {Y}⚠  For informational use only. Not financial advice.{X}")
    rule()
    print()


# ---------------------------------------------------------------------------
# EXCHANGE DISAMBIGUATION
# ---------------------------------------------------------------------------

# Suffix → human label. Order determines probe priority.
EXCHANGE_SUFFIXES = [
    ("", "US (NYSE / NASDAQ / CBOE)"),
    (".TO", "TSX"),
    (".NE", "Cboe Canada / NEO"),
    (".V", "TSX Venture"),
    (".CN", "CSE"),
]


def probe_exchanges(base: str) -> list[dict[str, Any]]:
    """
    Try each known exchange suffix for `base` and return every result that
    resolves to a real security. The full .info payload is retained so the
    caller does not have to fetch it a second time.
    """
    found = []

    for suffix, label in EXCHANGE_SUFFIXES:
        full = base + suffix

        # Single attempt per suffix: this is an existence probe across five
        # exchanges, and retrying each one turns a bad network into a
        # multi-minute hang. Real fetches below still retry.
        info = _retry(lambda f=full: yf.Ticker(f).info, attempts=1)

        if not info or not (info.get("shortName") or info.get("longName")):
            continue

        found.append(
            {
                "ticker": full,
                "name": info.get("longName") or info.get("shortName") or "N/A",
                "exchange": info.get("exchange") or label,
                "label": label,
                "quoteType": info.get("quoteType", ""),
                "currency": info.get("currency", ""),
                "info": info,
            }
        )

    return found


def resolve_ticker(raw: str) -> tuple[Optional[str], Optional[dict]]:
    """
    Accept a raw ticker string, with or without exchange suffix.

    Returns (ticker, info) so a resolved .info payload is reused rather
    than refetched.
    """
    raw = raw.upper().strip()

    if "." in raw:
        return raw, None

    print(f"\n  {C}Probing exchanges for {raw}...{X}")
    candidates = probe_exchanges(raw)

    if not candidates:
        print(f"\n  {R}No results found for '{raw}' on any supported exchange.{X}")
        return None, None

    if len(candidates) == 1:
        c = candidates[0]
        print(f"  Found: {c['name']}  [{c['label']}  ·  {c['ticker']}]")
        return c["ticker"], c["info"]

    print(f"\n  {Y}'{raw}' matches on multiple exchanges:{X}\n")
    print(f"  {'#':<4}{'Ticker':<12}{'Exchange':<32}{'Type':<14}{'Name'}")
    print("  " + "-" * 72)

    for i, c in enumerate(candidates, 1):
        print(
            f"  {i:<4}{c['ticker']:<12}{c['label']:<32}"
            f"{c['quoteType']:<14}{c['name']}"
        )

    print()

    while True:
        try:
            choice = input(
                f"  {B}Select 1-{len(candidates)} (or 'q' to cancel): {X}"
            ).strip()
        except (KeyboardInterrupt, EOFError):
            return None, None

        if choice.lower() in ("q", "quit"):
            return None, None

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]["ticker"], candidates[idx]["info"]
        except ValueError:
            pass

        print(f"  {Y}Enter a number between 1 and {len(candidates)}.{X}")


# ---------------------------------------------------------------------------
# EVALUATE
# ---------------------------------------------------------------------------

def evaluate(raw_ticker: str) -> None:
    ticker, info = resolve_ticker(raw_ticker)

    if not ticker:
        return

    data = get_etf_data(ticker, info=info)

    if not data:
        return

    print(f"  {C}Calculating...{X}")
    m = calc_metrics(data)

    bm_ticker, bm_label, ccy_matched = pick_benchmark(
        m["etf_type"], m.get("category"), m.get("currency")
    )

    if not ccy_matched:
        print(
            f"  {Y}No benchmark listed in {m.get('currency')}; relative "
            f"metrics will be skipped.{X}"
        )
        bm_ret, track_diff = None, None
    elif bm_ticker.upper() == ticker.upper():
        # A fund cannot be measured against itself: it would score a
        # perfect zero deviation.
        print(f"  {C}{ticker} is its own category proxy; skipping comparison.{X}")
        bm_ret, track_diff = None, None
    else:
        print(f"  {C}Fetching benchmark ({bm_ticker} — {bm_label})...{X}")
        bm_data = get_benchmark_data(bm_ticker)
        bm_ret = calc_returns(bm_data["hist"], rfr=m["rfr"]) if bm_data else None
        track_diff = (
            calc_tracking_diff(data["hist"], bm_data["hist"]) if bm_data else None
        )

    sc = build_scores(m, track_diff, bm_ret, currency_matched=ccy_matched)
    print_report(
        ticker, m, sc, track_diff, bm_ret, bm_ticker, bm_label, ccy_matched
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n  {B}{C}╔══════════════════════════════════════╗{X}")
    print(f"  {B}{C}║   ETF Evaluator v2.0                 ║{X}")
    print(f"  {B}{C}╚══════════════════════════════════════╝{X}")
    print(f"  {C}Tip: append exchange suffix to skip disambiguation")
    print(f"       e.g. FINN.TO for TSX, FINN.NE for Cboe Canada{X}")

    if len(sys.argv) > 1:
        evaluate(sys.argv[1])
        return

    while True:
        try:
            ticker = input(f"\n  {B}Enter ETF ticker (or 'q' to quit): {X}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n")
            break

        if ticker.lower() in ("q", "quit", "exit"):
            print("  Goodbye.\n")
            break

        if ticker:
            evaluate(ticker)


if __name__ == "__main__":
    main()

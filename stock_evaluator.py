#!/usr/bin/env python3
"""
Stock Investment Evaluator v5.3 — Compact Risk-Adjusted Edition
Yahoo Finance via yfinance. Optimized for iPhone a-Shell.

v5.3 changes vs v5.2:
- Adds value_screen(): a 52-week-low value flag. When a stock trades in the
  bottom 20% of its 52-week range, it uses the Health and Profitability
  dimension scores to distinguish a potential value entry from a value trap.
- Adds portfolio/batch mode: pass multiple tickers as CLI args to evaluate
  them sequentially, followed by a sector diversification summary that flags
  sector concentration risk.
- Both additions are informational only — they do NOT alter the composite
  score, same pattern as insider conviction and cash runway.

v5 changes vs v4:
- Adds R&D intensity (R&D / revenue), shown for relevant sectors.
- Adds insider conviction summary (6-month net buy/sell from Form 4 data).
- Adds cash runway, gated to cash-burning companies only.
- These three are informational/flags — they do NOT alter the composite
  score, to preserve the v3/v4 scoring calibration.
- New risk flag: heavy insider net selling.

v4 changes vs v3:
- Interest coverage capped at 30x for scoring (display unchanged)
- 52W high/low prefers info.fiftyTwoWeek* over historical
- Expanded sector list: 11 sectors + default
- Graduated valuation penalty when val_s < 6
- Beta-adjusted WACC in DCF (+/- 2% per beta point from 1.0)
- Gross margin warning for Basic Materials/Energy >55%
- Stale data check (annual statements >425d old)
- PEG excluded from scoring when extreme, not penalized
- EBIT normalized for cyclicals in Magic Formula
- CLI mode: python stock_evaluator.py TICKER

Setup: pip install yfinance
Usage: python stock_evaluator.py [TICKER [TICKER ...]]
"""

import math, os, sys
from datetime import datetime
from statistics import mean
import yfinance as yf

# ─── DISPLAY ───────────────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")

G, Y, R, C, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"

def colour(v, t=None):
    t = t if t is not None else f"{v:.2f}"
    return f"{G}{B}{t}{X}" if v >= 7.5 else f"{Y}{t}{X}" if v >= 5.0 else f"{R}{t}{X}"

def bar(s, w=14):
    s = max(0, min(10, s or 0))
    f = int(round((s / 10) * w))
    return "█" * f + "░" * (w - f)

# ─── HELPERS ───────────────────────────────────────────────────────────────
def rv(df, row, col=0):
    """Row value: safely fetch a cell from a yfinance DataFrame."""
    try:
        if df is None or df.empty or row not in df.index:
            return None
        v = df.loc[row].iloc[col]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    except Exception:
        return None

def rf(df, names, col=0):
    """Row first: try multiple row names, return first hit."""
    for n in names:
        v = rv(df, n, col)
        if v is not None:
            return v
    return None

def sd(a, b, default=None):
    try:
        return a / b if a is not None and b not in (None, 0) else default
    except Exception:
        return default

def num(x):
    """Coerce to float, returning None for None/NaN/non-numeric."""
    try:
        if x is None:
            return None
        x = float(x)
        return None if math.isnan(x) else x
    except Exception:
        return None

def avg(values):
    vs = [v for v in values if v is not None]
    return mean(vs) if vs else None

def clamp(x, lo, hi):
    return None if x is None else max(lo, min(hi, x))

def fmt_money(v):
    if v is None: return "N/A"
    a = abs(v)
    if a >= 1e12: return f"${v/1e12:.2f}T"
    if a >= 1e9:  return f"${v/1e9:.1f}B"
    if a >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"

def fmt_sh(v):
    if v is None: return "N/A"
    a = abs(v)
    if a >= 1e9: return f"{v/1e9:.2f}B sh"
    if a >= 1e6: return f"{v/1e6:.2f}M sh"
    if a >= 1e3: return f"{v/1e3:.1f}K sh"
    return f"{v:,.0f} sh"

# ─── SECTOR RULES ──────────────────────────────────────────────────────────
# Weights: (valuation, profitability, growth, health, momentum, frameworks)
W_CYC  = (0.25, 0.20, 0.10, 0.25, 0.10, 0.10)  # cyclicals
W_GROW = (0.18, 0.24, 0.24, 0.18, 0.10, 0.06)  # growth/tech
W_DEF  = (0.20, 0.25, 0.20, 0.20, 0.10, 0.05)  # default/stable
W_FIN  = (0.25, 0.25, 0.10, 0.25, 0.10, 0.05)  # financials
W_REIT = (0.25, 0.15, 0.15, 0.25, 0.10, 0.10)  # real estate
W_UTIL = (0.25, 0.20, 0.10, 0.25, 0.10, 0.10)  # utilities

# Tuple: (pe_g, pe_b, peg_g, peg_b, ev_g, ev_b, ps_g, ps_b, pb_g, pb_b,
#         roe_g, roic_g, growth_cap, cyclical, weights)
SECTORS = {
    "Basic Materials":        (12, 35, 0.8, 4.0,  6, 18, 2.0, 10, 1.5, 5.0, 0.15, 0.12, 0.12, True,  W_CYC),
    "Energy":                 (10, 30, 0.8, 4.0,  5, 16, 1.0,  6, 1.2, 4.0, 0.15, 0.12, 0.10, True,  W_CYC),
    "Financial Services":     (10, 25, 0.8, 3.0,  8, 30, 2.0,  8, 1.0, 3.0, 0.12, 0.08, 0.12, True,  W_FIN),
    "Technology":             (25, 80, 1.0, 5.0, 15, 50, 5.0, 25, 4.0, 18,  0.20, 0.15, 0.25, False, W_GROW),
    "Healthcare":             (18, 50, 1.0, 4.0, 12, 35, 3.0, 15, 3.0, 12,  0.15, 0.12, 0.15, False, W_DEF),
    "Consumer Cyclical":      (15, 40, 1.0, 4.0, 10, 25, 1.5,  8, 2.0,  8,  0.15, 0.12, 0.15, True,  W_CYC),
    "Consumer Defensive":     (18, 35, 1.0, 3.0, 12, 25, 1.5,  6, 3.0,  8,  0.18, 0.12, 0.10, False, W_DEF),
    "Industrials":            (15, 35, 1.0, 4.0, 10, 25, 1.5,  6, 2.5,  8,  0.15, 0.12, 0.15, False, W_DEF),
    "Real Estate":            (20, 60, 1.5, 5.0, 15, 40, 4.0, 15, 1.5,  4,  0.08, 0.06, 0.08, True,  W_REIT),
    "Utilities":              (15, 25, 1.5, 4.0, 10, 18, 2.0,  6, 1.5,  4,  0.10, 0.07, 0.05, False, W_UTIL),
    "Communication Services": (18, 45, 1.0, 4.0, 10, 30, 2.0, 10, 2.0,  8,  0.15, 0.12, 0.15, False, W_DEF),
}
DEFAULT_S = (15, 60, 0.8, 4.0, 8, 30, 1.0, 15, 1.5, 6.0, 0.20, 0.15, 0.20, False, W_DEF)
S_KEYS = ["pe_g","pe_b","peg_g","peg_b","ev_g","ev_b","ps_g","ps_b","pb_g","pb_b",
          "roe_g","roic_g","gcap","cyc","weights"]

# Sectors where R&D intensity is a genuinely meaningful signal
RND_SECTORS = ("Technology", "Healthcare", "Communication Services", "Industrials")

def srules(sector):
    return dict(zip(S_KEYS, SECTORS.get(sector, DEFAULT_S)))

# ─── DATA FETCH ────────────────────────────────────────────────────────────
def get_data(ticker):
    print(f"\n  {C}Fetching data for {ticker.upper()}...{X}")
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if not info or info.get("quoteType") is None or info.get("shortName") is None:
            print(f"\n  {R}Ticker '{ticker}' not found.{X}\n")
            return None

        # Insider data is optional — never let it break the run
        ins_purch = None
        try:
            ins_purch = t.insider_purchases
        except Exception:
            ins_purch = None

        return {"ticker": ticker.upper(), "info": info, "fin": t.financials,
                "bal": t.balance_sheet, "cf": t.cashflow,
                "hist": t.history(period="1y"), "insider_purchases": ins_purch}
    except Exception as e:
        print(f"\n  {R}Error fetching data: {e}{X}\n")
        return None

# ─── METRICS ───────────────────────────────────────────────────────────────
def calc_metrics(d):
    info, fin, bal, cf, hist = d["info"], d["fin"], d["bal"], d["cf"], d["hist"]
    m = {"warnings": [], "notes": []}

    # Identity
    m["name"] = info.get("longName") or info.get("shortName")
    m["sector"] = info.get("sector", "N/A")
    m["industry"] = info.get("industry", "N/A")
    m["exchange"] = info.get("exchange", "N/A")
    m["quote_currency"] = info.get("currency")
    m["financial_currency"] = info.get("financialCurrency")
    m["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    m["mktcap"] = info.get("marketCap")
    m["beta"] = info.get("beta")

    if m["quote_currency"] and m["financial_currency"] and m["quote_currency"] != m["financial_currency"]:
        m["warnings"].append(f"Currency mismatch: quotes in {m['quote_currency']}, financials in {m['financial_currency']}.")

    rules = srules(m["sector"])
    if rules["cyc"]:
        m["warnings"].append("Cyclical/commodity sector: growth metrics normalized.")

    # Stale data check
    try:
        if fin is not None and not fin.empty and hasattr(fin.columns[0], "to_pydatetime"):
            age = (datetime.now() - fin.columns[0].to_pydatetime()).days
            if age > 425:
                m["warnings"].append(f"Most recent annual statements are {age} days old.")
    except Exception:
        pass

    # Valuation
    m["pe"] = info.get("trailingPE")
    m["fwd_pe"] = info.get("forwardPE")
    m["peg"] = info.get("pegRatio")
    m["ps"] = info.get("priceToSalesTrailing12Months")
    m["pb"] = info.get("priceToBook")
    m["ev_ebitda"] = info.get("enterpriseToEbitda")

    if m.get("peg") is not None and (m["peg"] > 10 or m["peg"] < 0):
        m["warnings"].append(f"PEG {m['peg']:.2f} is unreliable; excluded from valuation score.")
        m["peg_excluded"] = True

    # Profitability
    m["gross_margin"] = info.get("grossMargins")
    m["op_margin"] = info.get("operatingMargins")
    m["net_margin"] = info.get("profitMargins")
    m["roe"] = info.get("returnOnEquity")
    m["roa"] = info.get("returnOnAssets")

    if m["sector"] in ("Basic Materials", "Energy") and m.get("gross_margin") and m["gross_margin"] > 0.55:
        m["warnings"].append(f"Gross margin {m['gross_margin']*100:.1f}% high for sector — likely excludes D&A/royalties.")

    # ROIC
    ebit = rf(fin, ["EBIT", "Operating Income"])
    tax_r = info.get("effectiveTaxRate")
    if tax_r is None or tax_r < 0 or tax_r > 0.6: tax_r = 0.21
    equity = rf(bal, ["Stockholders Equity", "Total Stockholders Equity", "Common Stock Equity"])
    debt = rf(bal, ["Total Debt", "Long Term Debt"]) or 0
    cash = rf(bal, ["Cash And Cash Equivalents", "Cash", "Cash Cash Equivalents And Short Term Investments"]) or 0
    inv_cap = (equity or 0) + debt - cash
    m["roic"] = sd(ebit * (1 - tax_r), inv_cap) if ebit and inv_cap > 0 else None
    m["ebit"] = ebit

    # Growth (raw values for display)
    rev0, rev1 = rv(fin, "Total Revenue", 0), rv(fin, "Total Revenue", 1)
    m["revenue"] = rev0
    m["rev_growth_raw"] = sd(rev0 - rev1, abs(rev1)) if rev0 and rev1 else None
    ni0, ni1 = rv(fin, "Net Income", 0), rv(fin, "Net Income", 1)
    m["ni_growth"] = sd(ni0 - ni1, abs(ni1)) if ni0 and ni1 else None
    m["eps"] = info.get("trailingEps")
    m["eps_growth_raw"] = info.get("earningsGrowth")

    # ─── R&D intensity (v5) ────────────────────────────────────────────────
    rnd = rf(fin, ["Research And Development", "Research Development"])
    m["rnd"] = rnd
    m["rnd_intensity"] = sd(rnd, rev0) if rnd and rev0 else None
    if m["rnd_intensity"] is None and m["sector"] in RND_SECTORS:
        m["notes"].append("R&D line not reported separately by Yahoo for this company.")

    # FCF normalization
    fcf_vals = [rv(cf, "Free Cash Flow", i) for i in range(4)]
    fcf0, fcf1 = fcf_vals[0], fcf_vals[1]
    m["fcf_latest"] = fcf0
    m["fcf_avg_3y"] = avg(fcf_vals[:3])
    if rules["cyc"] and m["fcf_avg_3y"] is not None and fcf0 is not None:
        m["fcf_normalized"] = min(fcf0, m["fcf_avg_3y"])
        m["notes"].append("DCF uses normalized FCF for cyclical sector.")
    else:
        m["fcf_normalized"] = fcf0
    m["fcf_growth_raw"] = sd(fcf0 - fcf1, abs(fcf1)) if fcf0 and fcf1 else None

    # Capped growth for cyclicals (used in scoring only)
    cap, cyc = rules["gcap"], rules["cyc"]
    m["rev_growth"] = clamp(m["rev_growth_raw"], -0.30, cap) if cyc else m["rev_growth_raw"]
    m["eps_growth"] = clamp(m["eps_growth_raw"], -0.30, cap) if cyc else m["eps_growth_raw"]
    m["fcf_growth"] = clamp(m["fcf_growth_raw"], -0.30, cap) if cyc else m["fcf_growth_raw"]

    # ─── Cash runway (v5) — only meaningful for cash-burning firms ──────────
    ocf = rf(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                  "Cash Flow From Continuing Operating Activities"])
    cash_pos = rf(bal, ["Cash Cash Equivalents And Short Term Investments",
                        "Cash And Cash Equivalents", "Cash"])
    m["operating_cf"] = ocf
    m["cash_position"] = cash_pos
    burns = []
    if ocf is not None and ocf < 0:  burns.append(-ocf)
    if fcf0 is not None and fcf0 < 0: burns.append(-fcf0)
    annual_burn = max(burns) if burns else None  # most conservative (largest) burn
    m["annual_burn"] = annual_burn
    if annual_burn and cash_pos and cash_pos > 0:
        m["runway_years"] = cash_pos / annual_burn
        m["runway_months"] = m["runway_years"] * 12
        if m["runway_months"] < 12:
            m["warnings"].append(f"Short cash runway: ~{m['runway_months']:.0f} months at current burn rate.")
    else:
        m["runway_years"] = None
        m["runway_months"] = None
        if annual_burn and not cash_pos:
            m["notes"].append("Company is burning cash but cash position could not be read.")

    # Health
    m["curr_ratio"] = info.get("currentRatio")
    m["quick_ratio"] = info.get("quickRatio")
    m["debt_eq"] = info.get("debtToEquity")
    if m["debt_eq"] is not None: m["debt_eq"] /= 100
    int_exp = rv(fin, "Interest Expense")
    if int_exp and int_exp < 0: int_exp = abs(int_exp)
    m["int_coverage"] = sd(ebit, int_exp) if ebit and int_exp else None
    m["fcf_quality"] = sd(fcf0, ni0) if fcf0 and ni0 else None

    # 52W: prefer info.fiftyTwoWeek*, fallback to history
    m["52w_high"] = info.get("fiftyTwoWeekHigh")
    m["52w_low"] = info.get("fiftyTwoWeekLow")
    if (m["52w_high"] is None or m["52w_low"] is None) and hist is not None and not hist.empty:
        prices = hist["Close"].dropna().tolist()
        if prices:
            m["52w_high"] = m["52w_high"] or max(prices)
            m["52w_low"] = m["52w_low"] or min(prices)
    px = m["price"]
    if m["52w_high"] and m["52w_low"] and px and m["52w_high"] != m["52w_low"]:
        m["pos_52w"] = (px - m["52w_low"]) / (m["52w_high"] - m["52w_low"])
    else:
        m["pos_52w"] = None

    m["book_value"] = info.get("bookValue")
    m["shares"] = info.get("sharesOutstanding")
    m["net_income"] = ni0
    return m

# ─── VALUE SCREEN (v5.3) ───────────────────────────────────────────────────
def value_screen(m, dims):
    """
    52-week-low value flag. Triggers when a stock trades in the bottom 20% of
    its 52-week range, then uses the Health and Profitability dimension scores
    (from build_scores) to distinguish a potential value entry from a value
    trap.

    Informational only — NOT folded into the composite score, same pattern as
    insider_conviction and cash runway.
    """
    pos = m.get("pos_52w")
    if pos is not None and pos <= 0.20:
        if dims.get("Health", 0) >= 5.5 and dims.get("Profitability", 0) >= 5.5:
            verdict = "Near 52-week low — fundamentals intact, potential value entry"
        else:
            verdict = "Near 52-week low — check for value trap, fundamentals weak"
        return {"triggered": True, "verdict": verdict}
    return {"triggered": False}

# ─── FRAMEWORKS ────────────────────────────────────────────────────────────
def piotroski(d):
    info, fin, bal, cf = d["info"], d["fin"], d["bal"], d["cf"]
    score, sigs = 0, {}
    def add(label, cond):
        nonlocal score
        sigs[label] = 1 if bool(cond) else 0
        score += sigs[label]

    roa0 = info.get("returnOnAssets")
    roa1 = sd(rv(fin, "Net Income", 1), rv(bal, "Total Assets", 1))
    fcf = rv(cf, "Free Cash Flow", 0)
    cfo = rf(cf, ["Operating Cash Flow", "Total Cash From Operating Activities", "Cash Flow From Continuing Operating Activities"])
    ni0 = rv(fin, "Net Income", 0)
    rev0, rev1 = rv(fin, "Total Revenue", 0), rv(fin, "Total Revenue", 1)
    gp0, gp1 = rv(fin, "Gross Profit", 0), rv(fin, "Gross Profit", 1)
    td0, td1 = rv(bal, "Total Debt", 0), rv(bal, "Total Debt", 1)
    cr0 = info.get("currentRatio")

    add("ROA > 0", roa0 and roa0 > 0)
    add("FCF > 0", fcf and fcf > 0)
    add("ROA improving", roa0 and roa1 and roa0 > roa1)
    add("CFO > Net Income", cfo and ni0 and cfo > ni0)
    add("Debt decreasing", td0 is not None and td1 is not None and td0 < td1)
    add("Current ratio > 1", cr0 and cr0 > 1)
    add("Positive net income", ni0 and ni0 > 0)
    add("Revenue growing", rev0 and rev1 and rev0 > rev1)
    add("Gross profit growing", gp0 and gp1 and gp0 > gp1)

    label = "Strong" if score >= 7 else "Neutral" if score >= 4 else "Weak"
    return {"score": score, "max": 9, "label": label, "signals": sigs}

def altman_z(d):
    info, fin, bal = d["info"], d["fin"], d["bal"]
    ta = rv(bal, "Total Assets", 0)
    if not ta: return None
    ca = rv(bal, "Current Assets", 0) or 0
    cl = rv(bal, "Current Liabilities", 0) or 0
    re = rv(bal, "Retained Earnings", 0) or 0
    ebit = rf(fin, ["EBIT", "Operating Income"]) or 0
    rev = rv(fin, "Total Revenue", 0) or 0
    tl = rf(bal, ["Total Liabilities Net Minority Interest", "Total Liabilities"]) or 0
    mc = info.get("marketCap") or 0
    z = (1.2*sd(ca-cl, ta, 0) + 1.4*sd(re, ta, 0) + 3.3*sd(ebit, ta, 0) +
         0.6*sd(mc, tl, 0) + sd(rev, ta, 0))
    zone = "Safe" if z > 3.0 else "Grey" if z > 1.8 else "Distress"
    warning = None
    if info.get("financialCurrency") and info.get("currency") and info.get("financialCurrency") != info.get("currency"):
        warning = "Z-score may need FX adjustment due to currency mismatch."
    return {"score": round(z, 2), "zone": zone, "warning": warning}

def graham_number(m):
    eps, bvps, px = m.get("eps"), m.get("book_value"), m.get("price")
    if not eps or not bvps or eps <= 0 or bvps <= 0: return None
    gn = math.sqrt(22.5 * eps * bvps)
    mos = sd(gn - px, px) if px else None
    return {"graham": round(gn, 2), "price": px, "mos": round(mos*100, 1) if mos is not None else None}

def magic_formula(d, m):
    """Magic Formula: EBIT/EV. EBIT normalized for cyclicals (min of latest vs 3y avg)."""
    info, fin, bal = d["info"], d["fin"], d["bal"]
    rules = srules(m.get("sector"))

    # EBIT normalization for cyclicals
    if rules["cyc"]:
        ebit_vals = [rf(fin, ["EBIT", "Operating Income"], i) for i in range(3)]
        ebit_avg = avg(ebit_vals)
        ebit_latest = ebit_vals[0] if ebit_vals else None
        ebit = min(ebit_latest, ebit_avg) if ebit_latest and ebit_avg else (ebit_latest or ebit_avg)
    else:
        ebit = m.get("ebit") or rf(fin, ["EBIT", "Operating Income"])

    market_cap = info.get("marketCap")
    debt = rf(bal, ["Total Debt", "Long Term Debt"]) or 0
    cash = rf(bal, ["Cash And Cash Equivalents", "Cash", "Cash Cash Equivalents And Short Term Investments"]) or 0
    roic = m.get("roic")

    if not ebit or not market_cap or not roic:
        ev_eb = m.get("ev_ebitda")
        if ev_eb and ev_eb > 0 and roic:
            return {"ey": round((1/ev_eb)*100, 2), "roic": round(roic*100, 2),
                    "combined": round(((1/ev_eb)+roic)*100, 2),
                    "warning": "EY approximated from 1/EV-EBITDA."}
        return None

    ev = market_cap + debt - cash
    ey = sd(ebit, ev)
    if not ey: return None
    warning = None
    if info.get("financialCurrency") and info.get("currency") and info.get("financialCurrency") != info.get("currency"):
        warning = "Magic Formula may need FX adjustment."
    return {"ey": round(ey*100, 2), "roic": round(roic*100, 2),
            "combined": round((ey+roic)*100, 2), "warning": warning}

def insider_conviction(d):
    """
    Summarize insider buying vs selling over the last 6 months from
    Yahoo's Form-4-derived `insider_purchases` table.

    Informational only — NOT folded into the composite score. Insider
    data is noisy: routine sales and pre-scheduled 10b5-1 plans inflate
    'selling' without signalling a real loss of conviction.
    """
    ip = d.get("insider_purchases")
    try:
        if ip is None or getattr(ip, "empty", True):
            return None
        cols = list(ip.columns)
        if not cols:
            return None
        label_col = cols[0]
        shares_col = cols[1] if len(cols) > 1 else None
        trans_col = cols[2] if len(cols) > 2 else None

        buy_sh = sell_sh = net_sh = pct_net = None
        buy_tx = sell_tx = None
        for _, r in ip.iterrows():
            lbl = str(r[label_col]).strip().lower()
            sh = num(r[shares_col]) if shares_col else None
            tx = num(r[trans_col]) if trans_col else None
            if lbl.startswith("purchase"):
                buy_sh, buy_tx = sh, tx
            elif lbl.startswith("sale"):
                sell_sh, sell_tx = sh, tx
            elif "net shares" in lbl and "%" not in lbl:
                net_sh = sh
            elif lbl.startswith("% net"):
                pct_net = sh

        # Derive net if not directly given
        if net_sh is None and buy_sh is not None and sell_sh is not None:
            net_sh = buy_sh - sell_sh

        # Nothing usable
        if buy_sh is None and sell_sh is None and net_sh is None:
            return None

        if net_sh is not None and net_sh > 0:
            verdict = "Net buying"
        elif net_sh is not None and net_sh < 0:
            verdict = "Net selling"
        else:
            verdict = "Neutral / minimal activity"

        return {"buy_sh": buy_sh, "sell_sh": sell_sh, "net_sh": net_sh,
                "buy_tx": buy_tx, "sell_tx": sell_tx,
                "pct_net": pct_net, "verdict": verdict}
    except Exception:
        return None

def dcf_scenarios(d, m):
    """Bear/base/bull DCF with normalized FCF and beta-adjusted WACC."""
    fcf0 = m.get("fcf_normalized") or m.get("fcf_latest")
    shares = m.get("shares")
    price = m.get("price")
    if not fcf0 or not shares: return None

    rules = srules(m.get("sector"))
    cyc = rules["cyc"]
    beta = m.get("beta") or 1.0
    wacc_adj = (beta - 1) * 0.02  # +/- 2% per beta point from 1.0

    if cyc:
        scenarios = [("bear", 0.03, 0.015, 0.105, 0.75),
                     ("base", 0.06, 0.025, 0.095, 0.90),
                     ("bull", 0.10, 0.030, 0.090, 1.00)]
    else:
        scenarios = [("bear", 0.04, 0.020, 0.105, 0.85),
                     ("base", 0.08, 0.025, 0.095, 1.00),
                     ("bull", 0.13, 0.030, 0.090, 1.10)]

    results = {}
    for name, g, tg, dr, fcf_adj in scenarios:
        dr += wacc_adj
        if dr <= tg: continue
        adj_fcf = fcf0 * fcf_adj
        fwd = [adj_fcf * (1+g)**yr for yr in range(1, 6)]
        tv = fwd[-1] * (1+tg) / (dr-tg)
        pv = sum(f/(1+dr)**i for i, f in enumerate(fwd, 1)) + tv/(1+dr)**5
        iv = sd(pv, shares)
        upside = sd((iv or 0) - price, price) if price else None
        results[name] = {"iv": round(iv, 2) if iv else None,
                         "upside": round(upside*100, 1) if upside is not None else None,
                         "growth": round(g*100, 1), "terminal_growth": round(tg*100, 1),
                         "discount_rate": round(dr*100, 1), "fcf_adjustment": fcf_adj}
    return results

# ─── SCORING ───────────────────────────────────────────────────────────────
def score(val, good, bad, higher=True):
    if val is None: return 5.0
    if higher:
        if val >= good: return 10.0
        if val <= bad:  return 1.0
        return 1 + 9 * (val - bad) / (good - bad)
    else:
        if val <= good: return 10.0
        if val >= bad:  return 1.0
        return 1 + 9 * (bad - val) / (bad - good)

def build_scores(m, pio, alt, gra, mag, dc):
    rules = srules(m.get("sector"))

    # Valuation: exclude PEG when extreme (renormalize weights)
    pe_s  = score(m.get("pe"),        rules["pe_g"],  rules["pe_b"], False)
    fpe_s = score(m.get("fwd_pe"),    rules["pe_g"],  rules["pe_b"], False)
    ev_s  = score(m.get("ev_ebitda"), rules["ev_g"],  rules["ev_b"], False)
    ps_s  = score(m.get("ps"),        rules["ps_g"],  rules["ps_b"], False)
    pb_s  = score(m.get("pb"),        rules["pb_g"],  rules["pb_b"], False)
    if m.get("peg_excluded") or m.get("peg") is None:
        val_s = pe_s*0.25 + fpe_s*0.20 + ev_s*0.25 + ps_s*0.15 + pb_s*0.15
    else:
        peg_s = score(m.get("peg"), rules["peg_g"], rules["peg_b"], False)
        val_s = pe_s*0.22 + fpe_s*0.18 + ev_s*0.22 + ps_s*0.13 + pb_s*0.15 + peg_s*0.10

    # Profitability
    gm_s   = score(m.get("gross_margin"), 0.45, 0.10, True)
    om_s   = score(m.get("op_margin"),    0.22, 0.00, True)
    roe_s  = score(m.get("roe"),  rules["roe_g"],  0.00, True)
    roa_s  = score(m.get("roa"),  0.08, 0.00, True)
    roic_s = score(m.get("roic"), rules["roic_g"], 0.00, True)
    prof_s = (gm_s + om_s + roe_s + roa_s + roic_s) / 5

    # Growth
    cap = rules["gcap"]
    rg_s = score(m.get("rev_growth"), cap, -0.05, True)
    eg_s = score(m.get("eps_growth"), cap, -0.10, True)
    fg_s = score(m.get("fcf_growth"), cap, -0.10, True)
    grow_s = (rg_s + eg_s + fg_s) / 3

    # Health (cap interest coverage at 30x for scoring purposes only)
    de_s = score(m.get("debt_eq"),    0.30, 2.50, False)
    cr_s = score(m.get("curr_ratio"), 2.50, 1.00, True)
    qr_s = score(m.get("quick_ratio"),1.50, 0.50, True)
    ic_capped = min(m["int_coverage"], 30) if m.get("int_coverage") else None
    ic_s = score(ic_capped, 10.0, 1.50, True)
    fq_s = score(m.get("fcf_quality"), 1.2, 0.30, True)
    health_s = (de_s + cr_s + qr_s + ic_s + fq_s) / 5

    # Momentum
    mom_s = score(m.get("pos_52w"), 0.80, 0.20, True)

    # Frameworks
    fw = []
    if pio: fw.append((pio["score"]/9) * 10)
    if alt: fw.append(score(alt["score"], 3.0, 1.8, True))
    if gra and gra["mos"] is not None: fw.append(score(gra["mos"], 40, -30, True))
    if mag: fw.append(score(mag["combined"], 25, 5, True))
    if dc and dc.get("base") and dc["base"].get("upside") is not None:
        fw.append(score(dc["base"]["upside"], 35, -20, True))
    fw_s = sum(fw)/len(fw) if fw else 5.0

    w = rules["weights"]
    composite = val_s*w[0] + prof_s*w[1] + grow_s*w[2] + health_s*w[3] + mom_s*w[4] + fw_s*w[5]

    # Graduated valuation penalty: subtract proportional amount when val_s < 6
    if val_s < 6.0:
        composite -= (6.0 - val_s) * 0.2

    composite = max(0, min(10, composite))

    dims = {"Valuation": round(val_s, 2), "Profitability": round(prof_s, 2),
            "Growth": round(grow_s, 2), "Health": round(health_s, 2),
            "Momentum": round(mom_s, 2), "Frameworks": round(fw_s, 2)}

    rating = rate(composite, dims)
    return {"composite": round(composite, 2), "dims": dims, "rating": rating}

def rate(comp, dims):
    if comp >= 8.0 and dims.get("Valuation", 10) < 6.0:
        return "QUALITY BUY, BUT VALUATION STRETCHED"
    if comp >= 8.5: return "HIGH-CONVICTION BUY"
    if comp >= 7.0: return "BUY / ACCUMULATE"
    if comp >= 5.5: return "HOLD / WATCHLIST"
    if comp >= 4.0: return "SPECULATIVE / WEAK"
    return "AVOID"

def position_guidance(m, sc, ins):
    comp = sc["composite"]
    flags = []
    if (m.get("beta") or 1.0) > 1.3: flags.append("high beta")
    if m.get("sector") in ("Basic Materials", "Energy"): flags.append("commodity/cyclical")
    if m.get("debt_eq") and m["debt_eq"] > 1.0: flags.append("elevated leverage")
    if m.get("pos_52w") and m["pos_52w"] > 0.85: flags.append("near 52-week high")
    if m.get("peg_excluded"): flags.append("unreliable PEG")
    if sc["dims"].get("Valuation", 10) < 5.5: flags.append("valuation not cheap")
    if m.get("runway_months") is not None and m["runway_months"] < 12:
        flags.append("short cash runway")
    if ins and ins.get("verdict") == "Net selling":
        flags.append("insider net selling")

    if comp >= 8.5 and len(flags) <= 1:
        guide = "Core: 3%–5%; up to 8% with diversification."
    elif comp >= 7.0:
        guide = "Accumulate: 2%–4%; add on weakness."
    elif comp >= 5.5:
        guide = "Watchlist: 0%–2% only."
    else:
        guide = "Avoid; research only."
    return {"guide": guide, "risk_flags": flags}

# ─── REPORT ────────────────────────────────────────────────────────────────
def print_report(ticker, m, sc, pio, alt, gra, mag, dc, ins, pos, vs):
    W = 62
    comp = sc["composite"]
    dims = sc["dims"]

    def rule(ch="═"): print("  " + ch * W)
    def h(t): print(f"  {B}{t}{X}")
    def row(label, val, fmt=".2f", suffix=""):
        v = f"{format(val, fmt)}{suffix}" if val is not None else "N/A"
        print(f"  {label:<26}  {v}")

    print()
    rule()
    print(f"  {B}{C}{m['name']} ({ticker.upper()}){X}")
    print(f"  {m['sector']}  ·  {m['industry']}")
    px = f"${m['price']:.2f} {m.get('quote_currency') or ''}" if m.get("price") else "N/A"
    print(f"  {px}  ·  Cap: {fmt_money(m.get('mktcap'))}  ·  Beta: {m.get('beta') or 'N/A'}")
    if m.get("financial_currency") and m.get("quote_currency") and m["financial_currency"] != m["quote_currency"]:
        print(f"  Quote: {m['quote_currency']}  ·  Financials: {m['financial_currency']}")
    rule()

    print(f"\n  {B}COMPOSITE SCORE{X}   {colour(comp)} / 10   {bar(comp, 18)}")
    print(f"  Rating: {B}{sc['rating']}{X}")

    has_warn = m.get("warnings") or (alt and alt.get("warning")) or (mag and mag.get("warning"))
    if has_warn:
        print(); rule("─"); h("WARNINGS / DATA QUALITY"); rule("─")
        for w in m.get("warnings", []):
            print(f"  {Y}⚠{X}  {w}")
        if alt and alt.get("warning"): print(f"  {Y}⚠{X}  {alt['warning']}")
        if mag and mag.get("warning"): print(f"  {Y}⚠{X}  {mag['warning']}")

    print(); rule("─"); h("DIMENSION BREAKDOWN"); rule("─")
    for dim, val in dims.items():
        print(f"  {dim:<14}  {bar(val, 12)}  {colour(val)}")

    print(); rule("─"); h("NAMED FRAMEWORKS"); rule("─")
    if pio:
        sigs_yes = [k for k, v in pio["signals"].items() if v]
        sigs_no  = [k for k, v in pio["signals"].items() if not v]
        print(f"  Piotroski   {pio['score']}/9 — {pio['label']}")
        if sigs_yes: print(f"    {G}✓{X}  {' | '.join(sigs_yes)}")
        if sigs_no:  print(f"    {R}✗{X}  {' | '.join(sigs_no)}")
    if alt:
        zc = G if alt["zone"] == "Safe" else (Y if alt["zone"] == "Grey" else R)
        print(f"  Altman Z    {alt['score']}  [{zc}{alt['zone']}{X}]")
    if gra:
        arrow = "↑" if (gra["mos"] or 0) > 0 else "↓"
        print(f"  Graham      ${gra['graham']}  {arrow}  {gra['mos']}% MoS  (px ${gra['price']})")
    if mag:
        print(f"  Magic Formula  EY {mag['ey']}%  ·  ROIC {mag['roic']}%  ·  Combined {mag['combined']}%")
    if dc:
        print("  DCF Scenarios:")
        for name in ("bear", "base", "bull"):
            s = dc.get(name)
            if s:
                sign = "+" if (s.get("upside") or 0) > 0 else ""
                print(f"    {name.capitalize():<5} ${s['iv']:<8} ({sign}{s['upside']}%)  g={s['growth']}% WACC={s['discount_rate']}%")

    # ─── INSIDER ACTIVITY (v5) ─────────────────────────────────────────────
    print(); rule("─"); h("INSIDER ACTIVITY  (last 6 months)"); rule("─")
    if ins:
        vc = G if ins["verdict"] == "Net buying" else (R if ins["verdict"] == "Net selling" else Y)
        print(f"  Verdict: {vc}{ins['verdict']}{X}")
        if ins.get("buy_sh") is not None:
            tx = f" ({int(ins['buy_tx'])} trans)" if ins.get("buy_tx") else ""
            print(f"  Bought:  {fmt_sh(ins['buy_sh'])}{tx}")
        if ins.get("sell_sh") is not None:
            tx = f" ({int(ins['sell_tx'])} trans)" if ins.get("sell_tx") else ""
            print(f"  Sold:    {fmt_sh(ins['sell_sh'])}{tx}")
        if ins.get("net_sh") is not None:
            print(f"  Net:     {fmt_sh(ins['net_sh'])}")
        print(f"  {Y}Note: insider data is noisy — routine/scheduled (10b5-1){X}")
        print(f"  {Y}sales can show 'selling' without loss of conviction.{X}")
    else:
        print("  No insider transaction data available for this ticker.")

    # ─── CASH RUNWAY (v5) — shown only for cash-burning companies ──────────
    if m.get("annual_burn"):
        print(); rule("─"); h("CASH RUNWAY  (cash-burning company)"); rule("─")
        print(f"  Cash + ST investments   {fmt_money(m.get('cash_position'))}")
        print(f"  Annual cash burn        {fmt_money(-m['annual_burn'])}")
        if m.get("runway_months") is not None:
            rc = R if m["runway_months"] < 12 else (Y if m["runway_months"] < 24 else G)
            yrs = m["runway_years"]
            print(f"  Estimated runway        {rc}~{m['runway_months']:.0f} months ({yrs:.1f} yrs){X}")
        else:
            print("  Estimated runway        N/A (cash position unavailable)")
        print(f"  {Y}Burn = larger of negative operating CF / negative FCF.{X}")

    print(); rule("─"); h("POSITION GUIDANCE"); rule("─")
    print(f"  {pos['guide']}")
    if pos["risk_flags"]:
        print(f"  Risk flags: {', '.join(pos['risk_flags'])}")

    # ─── VALUE SCREEN (v5.3) — informational 52-week-low flag ──────────────
    if vs and vs.get("triggered"):
        print(); rule("─"); h("VALUE SCREEN"); rule("─")
        vc = G if "intact" in vs["verdict"] else Y
        print(f"  {vc}{vs['verdict']}{X}")

    print(); rule("─"); h("KEY METRICS"); rule("─")
    print(f"  {B}— Valuation —{X}")
    row("P/E (trailing)",   m.get("pe"),        ".1f")
    row("P/E (forward)",    m.get("fwd_pe"),    ".1f")
    row("PEG",              m.get("peg"),       ".2f")
    row("EV/EBITDA",        m.get("ev_ebitda"), ".1f")
    row("P/S",              m.get("ps"),        ".2f")
    row("P/B",              m.get("pb"),        ".2f")
    print(f"  {B}— Profitability —{X}")
    row("Gross Margin",     m.get("gross_margin"),".1%")
    row("Operating Margin", m.get("op_margin"),  ".1%")
    row("Net Margin",       m.get("net_margin"), ".1%")
    row("ROE",              m.get("roe"),         ".1%")
    row("ROA",              m.get("roa"),         ".1%")
    row("ROIC",             m.get("roic"),        ".1%")
    # R&D intensity — show for R&D-relevant sectors, or whenever a value exists
    if m.get("rnd_intensity") is not None or m.get("sector") in RND_SECTORS:
        print(f"  {B}— R&D —{X}")
        row("R&D Spend",      m.get("rnd"),           ",.0f")
        row("R&D Intensity",  m.get("rnd_intensity"), ".1%")
    print(f"  {B}— Growth (raw) —{X}")
    row("Revenue Growth",   m.get("rev_growth_raw"),".1%")
    row("EPS Growth",       m.get("eps_growth_raw"),".1%")
    row("FCF Growth",       m.get("fcf_growth_raw"),".1%")
    if m.get("fcf_normalized") != m.get("fcf_latest"):
        row("FCF (normalized)", m.get("fcf_normalized"), ",.0f")
    print(f"  {B}— Health —{X}")
    row("Debt/Equity",      m.get("debt_eq"),     ".2f")
    row("Current Ratio",    m.get("curr_ratio"),  ".2f")
    row("Quick Ratio",      m.get("quick_ratio"), ".2f")
    row("Interest Coverage",m.get("int_coverage"),".1f")
    row("FCF/Net Income",   m.get("fcf_quality"), ".2f")
    print(f"  {B}— Momentum —{X}")
    if m.get("52w_high") is not None: row("52W High",          m["52w_high"], ".2f", " $")
    if m.get("52w_low") is not None:  row("52W Low",           m["52w_low"],  ".2f", " $")
    if m.get("pos_52w") is not None:  row("Position in Range", m["pos_52w"],  ".1%")

    if m.get("notes"):
        print(); rule("─"); h("MODEL NOTES"); rule("─")
        for n in m["notes"]: print(f"  • {n}")

    print(); rule()
    print(f"  {Y}⚠  For informational use only. Not financial advice.{X}")
    rule(); print()

def print_diversification_summary(results):
    """Portfolio-mode sector diversification summary (v5.3). Only shown when
    2+ tickers were evaluated in a single batch run."""
    W = 62

    def rule(ch="═"): print("  " + ch * W)
    def h(t): print(f"  {B}{t}{X}")

    total = len(results)
    by_sector = {}
    for r in results:
        by_sector.setdefault(r["sector"] or "N/A", []).append(r)

    # Sort sectors by count descending
    ordered = sorted(by_sector.items(), key=lambda kv: len(kv[1]), reverse=True)

    print()
    rule()
    print(f"  {B}{C}PORTFOLIO DIVERSIFICATION SUMMARY{X}")
    print(f"  {total} tickers evaluated  ·  {len(by_sector)} sector(s)")
    rule()

    for sector, items in ordered:
        cnt = len(items)
        pct = cnt / total * 100
        print()
        print(f"  {B}{sector}{X}   {cnt}/{total}  ({pct:.0f}%)")
        for it in items:
            print(f"    {it['ticker']:<8}  {colour(it['composite'])} / 10")

    print(); rule("─")
    warned = False
    for sector, items in ordered:
        pct = len(items) / total * 100
        if pct > 40:
            print(f"  {Y}Concentration risk: {pct:.0f}% of candidates are in "
                  f"{sector}. Consider diversifying across sectors.{X}")
            warned = True
    if not warned:
        print(f"  {G}Sector spread looks reasonably diversified.{X}")
    rule(); print()

# ─── MAIN ──────────────────────────────────────────────────────────────────
def evaluate(ticker):
    data = get_data(ticker)
    if not data: return None
    print(f"  {C}Calculating...{X}")
    m = calc_metrics(data)
    pio = piotroski(data)
    alt = altman_z(data)
    gra = graham_number(m)
    mag = magic_formula(data, m)
    ins = insider_conviction(data)
    dc  = dcf_scenarios(data, m)
    sc  = build_scores(m, pio, alt, gra, mag, dc)
    pos = position_guidance(m, sc, ins)
    vs  = value_screen(m, sc["dims"])
    print_report(ticker, m, sc, pio, alt, gra, mag, dc, ins, pos, vs)
    return {"ticker": ticker, "sector": m["sector"], "composite": sc["composite"]}

def main():
    print(f"\n  {B}{C}╔═════════════════════════════════════╗{X}")
    print(f"  {B}{C}║  Stock Evaluator v5.3 Risk-Adjusted ║{X}")
    print(f"  {B}{C}╚═════════════════════════════════════╝{X}")

    # CLI mode: python stock_evaluator.py TICKER [TICKER ...]
    if len(sys.argv) > 1:
        tickers = [t.upper() for t in sys.argv[1:]]
        results = []
        for tk in tickers:
            r = evaluate(tk)
            if r:
                results.append(r)
        if len(results) >= 2:
            print_diversification_summary(results)
        return

    while True:
        try:
            ticker = input(f"\n  {B}Enter ticker (or 'q' to quit): {X}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n"); break
        if ticker.lower() in ("q", "quit", "exit"):
            print("  Goodbye.\n"); break
        if not ticker: continue
        evaluate(ticker.upper())

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Public Sentiment Evaluator — Yahoo Finance + Reddit
v3.0 Calibrated scoring

Purpose:
    Pulls recent public sentiment signals from:
      1) Yahoo Finance news via yfinance
      2) Reddit public JSON search endpoints

    Then prints separate sentiment sections:
      - YAHOO FINANCE PUBLIC SENTIMENT
      - REDDIT PUBLIC SENTIMENT
      - COMBINED PUBLIC SENTIMENT VIEW

What v3.0 changed and why:

    1. Tone is normalised per item and bounded.
       v2 summed keyword hits across the whole corpus without dividing by the
       item count, so the score tracked ARTICLE VOLUME rather than sentiment
       and saturated after ~6 net hits. Tone is now a weighted mean per item
       pushed through tanh, so 20 mildly negative headlines and 3 mildly
       negative headlines read the same.

    2. The composite is centred on 5.0 and spans 0-10.
       v2's formula (tone*0.55 + catalyst*0.20 + macro*0.15 + (10-hype)*0.10)
       scored genuinely neutral coverage at 3.75 -- which its own thresholds
       called "mildly negative" -- and could not reach "positive" on tone
       alone. Catalyst and macro measure TOPIC PREVALENCE, not direction, so
       adding them with a positive sign meant bad news partly scored itself
       back up. They are now reported as separate context gauges and are not
       part of the sentiment number.

    3. A source that returned nothing is excluded from the combined view
       instead of voting a placeholder 5.0, and the "sources disagree" note
       only fires when both sources actually have items.

    4. Reddit relevance establishes identity FIRST. v2 applied its
       ambiguity penalty after awarding the cashtag/company-name bonus, so it
       rejected posts that named the company explicitly. The ambiguity guard
       is also generic now rather than hardcoded to one ticker.

    5. Keyword matching is word-boundary based. v2 used substring matching,
       so "commissioning" scored both positive and negative ("miss"),
       "refinery" scored negative ("fine"), and "capex" scored as hype
       ("ape"). Nested matches are also collapsed so one phrase counts once.

    6. Recency is enforced locally (decay + hard cutoff) rather than trusted
       to Reddit's `t=` parameter, which is ignored for `sort=new`.

    7. Negated keywords flip polarity. v2 read "failed to beat" as a positive
       hit and "lawsuit dismissed" as a negative one. A negator within a few
       tokens now flips the keyword, and the flip is reported so the reader
       can see it happened.

Setup:
    pip install yfinance requests

Usage:
    py public_sentiment.py

Notes:
    - This is keyword/rule-based sentiment, not true AI interpretation.
      Negation is a token-window heuristic, not parsing: it catches
      "failed to beat" and "strike averted", but not sarcasm or negation
      spread across clauses.
    - Reddit's unauthenticated JSON endpoints are frequently blocked (403) or
      rate limited (429). The report says so explicitly rather than silently
      reporting a neutral Reddit reading.
    - For serious investment research, treat this as a screening layer, not a
      final decision engine.
    - No API keys are required.
"""

import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
import yfinance as yf


# ─────────────────────────────────────────────────────────────────────────────
# TUNABLES
# ─────────────────────────────────────────────────────────────────────────────

# Tone: mean net keyword hits per item is multiplied by this before tanh.
# Calibrated so 1 net positive hit per item -> 7.3 ("mildly positive") and
# 2 -> 8.8 ("positive"). Raising this makes a single keyword shout louder.
TONE_SENSITIVITY = 0.5

# Hype does not push sentiment down; it shrinks the reading toward neutral,
# because crowding makes the signal less trustworthy in EITHER direction.
HYPE_SHRINK_MAX = 0.30

# Recency. Items older than the cutoff are dropped outright; the rest decay.
RECENCY_HALF_LIFE_DAYS = 21.0
MAX_AGE_DAYS = 180.0

# Reddit relevance: total rule score needed to keep a post.
RELEVANCE_THRESHOLD = 4

# Reddit is stopped after this many consecutive 403/429 responses.
REDDIT_MAX_CONSECUTIVE_BLOCKS = 2

# Source weights in the combined view (before confidence weighting).
YAHOO_SOURCE_WEIGHT = 0.60
REDDIT_SOURCE_WEIGHT = 0.40


# ─────────────────────────────────────────────────────────────────────────────
# ANSI COLOURS
# ─────────────────────────────────────────────────────────────────────────────

if sys.platform == "win32":
    os.system("color")

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def colour_score(score: Optional[float], text: Optional[str] = None, kind: str = "sentiment") -> str:
    """
    Colour bands match the verdict bands in interpret_sentiment() so the
    colour and the words never disagree.
    """
    if score is None:
        return f"{YELLOW}N/A{RESET}"
    t = text if text is not None else f"{score:.2f}"

    if kind == "gauge":
        # Catalyst/macro are not good-or-bad, so they get no good-or-bad colour.
        return f"{CYAN}{t}{RESET}"
    if kind == "risk":
        if score >= 6.0:
            return f"{RED}{BOLD}{t}{RESET}"
        if score >= 3.5:
            return f"{YELLOW}{t}{RESET}"
        return f"{GREEN}{t}{RESET}"
    if kind == "confidence":
        if score >= 7.0:
            return f"{GREEN}{t}{RESET}"
        if score >= 3.0:
            return f"{YELLOW}{t}{RESET}"
        return f"{RED}{BOLD}{t}{RESET}"

    # sentiment: 7.5 / 6.0 / 4.5 / 3.0, same as interpret_sentiment()
    if score >= 7.5:
        return f"{GREEN}{BOLD}{t}{RESET}"
    if score >= 6.0:
        return f"{GREEN}{t}{RESET}"
    if score >= 4.5:
        return f"{YELLOW}{t}{RESET}"
    if score >= 3.0:
        return f"{RED}{t}{RESET}"
    return f"{RED}{BOLD}{t}{RESET}"


def bar(score: Optional[float], width: int = 18) -> str:
    if score is None:
        return "░" * width
    score = max(0, min(10, score))
    filled = int(round((score / 10) * width))
    return "█" * filled + "░" * (width - filled)


def clamp(x: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, x))


# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD DICTIONARIES
#
# Matching is word-boundary based, so inflections need their own entries
# ("beat" no longer matches "beats"). Nested matches are collapsed at scoring
# time, so "strategic acquisition" counts once, not twice.
# ─────────────────────────────────────────────────────────────────────────────

POSITIVE_KEYWORDS = {
    "earnings": [
        "beat", "beats", "record earnings", "strong earnings", "earnings growth",
        "raises guidance", "raise guidance", "raised guidance", "guidance raised",
        "outperform",
        "better than expected", "above expectations", "profit rises", "profits rise",
        "revenue growth", "margin expansion", "free cash flow growth"
    ],
    "corporate": [
        "strategic acquisition", "acquires", "acquisition", "merger approved",
        "expansion", "new project", "new mine", "production growth",
        "reserve growth", "resource expansion", "new discovery", "high-grade",
        "approval", "permit approved", "development progress", "commissioning"
    ],
    "capital_returns": [
        "dividend increase", "raises dividend", "raise dividend", "buyback",
        "share repurchase",
        "returns capital", "special dividend"
    ],
    "market": [
        "upgrade", "upgrades", "upgraded", "price target raised", "bullish",
        "buy rating", "accumulate", "safe haven", "gold rally", "gold prices rise",
        "central bank demand"
    ],
}

NEGATIVE_KEYWORDS = {
    "earnings": [
        "miss", "misses", "missed expectations", "cuts guidance", "guidance cut",
        "weak earnings", "loss widens", "profit falls", "revenue decline",
        "margin pressure", "cost inflation", "higher costs", "impairment",
        "write-down", "lower production", "production miss"
    ],
    "corporate": [
        "lawsuit", "investigation", "probe", "probes", "fined", "fines",
        "penalty", "strike", "shutdown", "suspended", "delay", "delays",
        "delayed", "permit denied", "environmental violation", "accident",
        "fatality", "resignation", "dilution", "share issuance", "overpaid",
        "integration risk"
    ],
    "market": [
        "downgrade", "downgrades", "downgraded", "sell rating", "price target cut",
        "bearish", "short seller", "debt concern", "liquidity concern",
        "gold prices fall", "gold selloff", "rate hike", "strong dollar"
    ],
}

# A keyword is voided when one of its guard phrases is present, so options
# chatter ("strike price") does not read as a labour strike.
KEYWORD_GUARDS = {
    "strike": ["strike price", "strike prices", "strikes price"],
    "probe": ["probe drill", "drill probe"],
}

CATALYST_KEYWORDS = [
    "earnings", "guidance", "acquisition", "merger", "dividend", "buyback",
    "approval", "permit", "production", "reserves", "resource", "discovery",
    "expansion", "development", "commissioning", "analyst", "upgrade",
    "downgrade", "price target", "board", "ceo", "cfo", "investment",
    "stake", "strategic", "geopolitical", "inflation", "interest rates",
]

HYPE_KEYWORDS = [
    "moon", "mooning", "rocket", "short squeeze", "squeeze", "yolo", "ape",
    "apes", "diamond hands", "to the moon", "10x", "100x", "next tesla",
    "guaranteed", "can't lose", "load up", "all in", "massive upside",
    "undervalued gem", "hidden gem", "easy money", "bagger", "multi-bagger",
    "pump",
]

MACRO_GEOPOLITICAL_KEYWORDS = [
    "geopolitical", "war", "conflict", "sanctions", "tariff", "trade war",
    "central bank", "interest rates", "rate cut", "rate hike", "inflation",
    "deflation", "recession", "dollar", "usd", "currency", "safe haven",
    "china", "russia", "middle east", "ukraine", "oil", "permits",
    "mining law", "royalty", "tax", "government", "election",
]


# ─────────────────────────────────────────────────────────────────────────────
# RELEVANCE CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

STOCK_CONTEXT_WORDS = [
    "stock", "stocks", "share", "shares", "equity", "equities", "invest",
    "investing", "investment", "portfolio", "ticker", "tsx", "nyse", "nasdaq",
    "earnings", "valuation", "dividend", "market cap", "analyst",
    "price target", "buy", "sell", "hold", "long", "short", "calls", "puts",
]

# Sector context replaces v2's hardcoded gold-mining word list. The sector and
# industry strings come from Yahoo, so this generalises to any ticker.
SECTOR_CONTEXT_WORDS = {
    "basic materials": [
        "gold", "silver", "copper", "zinc", "nickel", "miner", "miners",
        "mining", "mine", "mines", "ore", "ounces", "grade", "smelter",
        "refinery", "drilling", "assay", "reserves", "resources", "tailings",
    ],
    "energy": [
        "oil", "gas", "lng", "barrel", "barrels", "drilling", "rig", "rigs",
        "refinery", "pipeline", "wti", "brent", "opec", "upstream", "downstream",
    ],
    "technology": [
        "software", "chip", "chips", "semiconductor", "cloud", "saas", "ai",
        "hardware", "platform", "data center", "foundry", "wafer",
    ],
    "financial services": [
        "bank", "banks", "lending", "loan", "loans", "deposits", "insurance",
        "underwriting", "net interest margin", "credit", "mortgage",
    ],
    "healthcare": [
        "drug", "drugs", "trial", "trials", "fda", "clinical", "patient",
        "therapy", "biotech", "pharma", "approval",
    ],
    "real estate": [
        "reit", "occupancy", "leasing", "property", "properties", "tenant",
        "tenants", "ffo", "cap rate",
    ],
    "consumer cyclical": [
        "retail", "store", "stores", "same-store", "brand", "consumer",
        "footfall", "e-commerce",
    ],
    "consumer defensive": [
        "retail", "grocery", "brand", "consumer", "staples", "volumes",
    ],
    "utilities": [
        "grid", "power", "electricity", "rate base", "regulator", "megawatt",
    ],
    "industrials": [
        "orders", "backlog", "manufacturing", "plant", "plants", "logistics",
        "freight", "aerospace",
    ],
    "communication services": [
        "subscribers", "advertising", "streaming", "arpu", "spectrum",
    ],
}

DEFAULT_SECTOR_CONTEXT = [
    "revenue", "profit", "margin", "guidance", "quarter", "outlook", "demand",
]

TARGET_SUBREDDITS = [
    "stocks",
    "investing",
    "SecurityAnalysis",
    "CanadianInvestor",
    "ValueInvesting",
    "StockMarket",
    "wallstreetbets",
    "pennystocks",
    "Gold",
    "mining",
    "wallstreetbetsnew",
    "tsx",
]


# ─────────────────────────────────────────────────────────────────────────────
# NEGATION
#
# Bag-of-words scoring reads "failed to beat" as positive. These lists let a
# nearby negator flip a keyword's polarity instead.
# ─────────────────────────────────────────────────────────────────────────────

NEGATION_WINDOW = 4            # tokens before a match that may negate it
NEGATION_TRAILING_WINDOW = 2   # tokens after a match that may cancel it

NEGATION_PRECEDING = [
    "not", "no", "never", "without", "nothing", "none",
    "fail to", "fails to", "failed to", "failing to",
    "unable to", "cannot", "can not", "can't", "could not", "couldn't",
    "does not", "doesn't", "did not", "didn't", "do not", "don't",
    "is not", "isn't", "was not", "wasn't", "are not", "aren't",
    "were not", "weren't", "will not", "won't", "would not", "wouldn't",
    "has not", "hasn't", "have not", "haven't", "had not", "hadn't",
    "lacks", "lacking", "absent", "short of", "stops short of",
    "denies", "deny", "denied", "rules out", "ruled out",
    "avoids", "avoid", "avoided", "averts", "averted", "escapes", "escaped",
    "far from", "instead of", "rather than",
]

NEGATION_FOLLOWING = [
    "dismissed", "dropped", "averted", "avoided", "withdrawn", "reversed",
    "overturned", "resolved", "settled", "lifted", "cleared", "scrapped",
    "called off", "abandoned", "rescinded", "thrown out",
]

# Trailing negation is risky next to an earnings phrase ("profit falls,
# dropped 12%"), so it only applies to legal/operational events.
NEGATION_FOLLOWING_APPLIES_TO = {
    "lawsuit", "investigation", "probe", "probes", "strike", "shutdown",
    "penalty", "fined", "fines", "suspended", "permit denied",
    "environmental violation", "integration risk", "debt concern",
    "liquidity concern", "impairment", "delay", "delays", "delayed",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9$']+")


# ─────────────────────────────────────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@lru_cache(maxsize=4096)
def _boundary_pattern(term: str) -> "re.Pattern":
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )


def word_boundary_match(text: str, term: str) -> bool:
    """
    Whole-word/whole-phrase matching. This is what stops "commissioning" from
    matching "miss", "refinery" from matching "fine", and "capex" from
    matching "ape".
    """
    if not term or not text:
        return False
    return _boundary_pattern(term).search(text) is not None


def _token_spans(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def _before_window(text: str, start: int) -> str:
    """
    The few tokens immediately preceding a match, stopping at a sentence
    boundary so "Revenue grew. Not a good quarter" does not negate "grew".
    """
    chunk = text[:start]
    cut = max((chunk.rfind(c) for c in ".!?;"), default=-1)
    if cut != -1:
        chunk = chunk[cut + 1:]
    return " ".join(_token_spans(chunk)[-NEGATION_WINDOW:])


def _after_window(text: str, end: int) -> str:
    chunk = text[end:]
    for c in ".!?;":
        i = chunk.find(c)
        if i != -1:
            chunk = chunk[:i]
    return " ".join(_token_spans(chunk)[:NEGATION_TRAILING_WINDOW])


def is_negated(text: str, start: int, end: int, keyword: str) -> bool:
    """
    True when the match at [start:end] is negated.

    Leading negation applies to any sentiment keyword ("failed to beat",
    "did not raise guidance"). Trailing negation applies only to the
    legal/operational keywords listed in NEGATION_FOLLOWING_APPLIES_TO
    ("lawsuit dismissed", "strike averted"), because a trailing "dropped" or
    "reversed" next to an earnings phrase usually describes the number rather
    than cancelling the event.
    """
    before = _before_window(text, start)
    if before and any(word_boundary_match(before, n) for n in NEGATION_PRECEDING):
        return True

    if keyword.lower() in NEGATION_FOLLOWING_APPLIES_TO:
        after = _after_window(text, end)
        if after and any(word_boundary_match(after, n) for n in NEGATION_FOLLOWING):
            return True

    return False


def find_keyword_spans(
    text: str,
    keywords: List[str],
    apply_guards: bool = True,
) -> List[Tuple[str, int, int]]:
    """
    Every whole-word occurrence as (keyword, start, end), with guarded
    keywords voided and occurrences swallowed by a longer match at the same
    position dropped -- so "strategic acquisition" is one hit, not also
    "acquisition", while a standalone "acquisition" elsewhere still counts.
    """
    if not text:
        return []

    spans: List[Tuple[str, int, int]] = []
    for kw in keywords:
        if apply_guards:
            guards = KEYWORD_GUARDS.get(kw.lower())
            if guards and any(word_boundary_match(text, g) for g in guards):
                continue
        for m in _boundary_pattern(kw).finditer(text):
            spans.append((kw, m.start(), m.end()))

    kept = []
    for kw, start, end in spans:
        swallowed = any(
            s2 <= start and end <= e2 and (e2 - s2) > (end - start)
            for _, s2, e2 in spans
        )
        if not swallowed:
            kept.append((kw, start, end))
    return kept


def contains_any(text: str, keywords: List[str], apply_guards: bool = True) -> List[str]:
    """
    The distinct keywords present in `text` as whole words. One item counts a
    keyword once however often it repeats.
    """
    seen = set()
    out = []
    for kw, _, _ in find_keyword_spans(text, keywords, apply_guards=apply_guards):
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            out.append(kw)
    return out


def classify_polarity(
    text: str,
    positive_words: List[str],
    negative_words: List[str],
) -> Tuple[List[str], List[str], int]:
    """
    Split a text's sentiment keywords into positive and negative, flipping
    negated ones.

    A keyword flips only if EVERY occurrence of it in the text is negated, so
    "they beat on revenue but did not beat on margin" still registers the
    unnegated hit. Flipped hits are labelled so the report shows why a
    positive word landed in the negative column.

    Negation is applied to polarity only. The catalyst/macro/hype gauges
    measure how much a topic is discussed, and "no lawsuit" is still lawsuit
    coverage.
    """
    if not text:
        return [], [], 0

    positive_hits: List[str] = []
    negative_hits: List[str] = []
    flips = 0

    for is_positive, words in ((True, positive_words), (False, negative_words)):
        grouped: Dict[str, List[Tuple[int, int]]] = {}
        for kw, start, end in find_keyword_spans(text, words):
            grouped.setdefault(kw, []).append((start, end))

        for kw, occurrences in grouped.items():
            negated = all(is_negated(text, s, e, kw) for s, e in occurrences)
            if negated:
                flips += 1
                label = f"{kw} (negated)"
                (negative_hits if is_positive else positive_hits).append(label)
            else:
                (positive_hits if is_positive else negative_hits).append(kw)

    return positive_hits, negative_hits, flips


def normalize_company_name(name: Optional[str]) -> str:
    if not name:
        return ""
    name = name.lower()
    remove_terms = [
        "limited", "ltd", "inc", "inc.", "corp", "corporation", "company",
        "plc", "sa", "ag", "nv", "class a", "class b", "common stock",
        "holdings", "group",
    ]
    for term in remove_terms:
        name = re.sub(rf"\b{re.escape(term)}\b", "", name)
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def base_ticker(ticker: str) -> str:
    return ticker.upper().split(".")[0].strip()


def flatten_keyword_dict(d: Dict[str, List[str]]) -> List[str]:
    out = []
    for values in d.values():
        out.extend(values)
    return out


def timestamp_to_epoch(ts: Any) -> Optional[float]:
    """
    Accepts a unix epoch (seconds or milliseconds) or an ISO-8601 string.
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        s = ts.strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except Exception:
            return None
    try:
        val = float(ts)
    except (TypeError, ValueError):
        return None
    if val > 10_000_000_000:
        val /= 1000.0
    return val


def epoch_to_date(epoch: Optional[float]) -> str:
    """UTC, so a Reddit created_utc never lands on the wrong calendar day."""
    if epoch is None:
        return "N/A"
    try:
        return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "N/A"


def age_days(epoch: Optional[float], now_epoch: float) -> Optional[float]:
    if epoch is None:
        return None
    return max(0.0, (now_epoch - epoch) / 86400.0)


def recency_weight(age: Optional[float]) -> float:
    """
    Exponential decay. Unknown-age items are not boosted or dropped; they get
    the weight of an item at one half-life.
    """
    if age is None:
        return 0.5
    return 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)


def most_common(items: List[str], limit: int = 8) -> List[Tuple[str, int]]:
    counts = {}
    for x in items:
        x = x.lower()
        counts[x] = counts.get(x, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# COMPANY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def sector_context_words(sector: Optional[str], industry: Optional[str]) -> List[str]:
    """
    Domain vocabulary for the ticker's own sector, derived from Yahoo metadata
    rather than hardcoded. v2 hardcoded a gold-mining list (and put "agnico"
    and "eagle" in it), which meant one company's name was doing relevance
    work for every other ticker.
    """
    words = []
    key = (sector or "").strip().lower()
    words.extend(SECTOR_CONTEXT_WORDS.get(key, []))

    # Industry strings ("Gold", "Semiconductors") carry useful extra terms.
    for token in re.findall(r"[a-z]{4,}", (industry or "").lower()):
        if token not in words:
            words.append(token)

    if not words:
        words = list(DEFAULT_SECTOR_CONTEXT)
    return words


def fetch_company_info(ticker: str, yf_ticker: Optional[Any] = None) -> Dict[str, Any]:
    """
    Resolve a ticker into a company name, search terms and sector vocabulary.
    """
    info = {}
    try:
        t = yf_ticker if yf_ticker is not None else yf.Ticker(ticker)
        info = t.info or {}
    except Exception:
        info = {}

    company_name = info.get("longName") or info.get("shortName") or ""
    normalized = normalize_company_name(company_name)
    base = base_ticker(ticker)

    terms = []
    if normalized:
        terms.append(normalized)
        words = normalized.split()
        if len(words) >= 2:
            terms.append(" ".join(words[:2]))
        if len(words) >= 3:
            terms.append(" ".join(words[:3]))

    deduped_terms = []
    seen = set()
    for t_ in terms:
        t_ = t_.strip().lower()
        if t_ and t_ not in seen:
            seen.add(t_)
            deduped_terms.append(t_)

    sector = info.get("sector")
    industry = info.get("industry")

    return {
        "ticker": ticker.upper(),
        "base_ticker": base,
        "company_name": company_name,
        "normalized_company_name": normalized,
        "company_terms": deduped_terms,
        "sector": sector,
        "industry": industry,
        "sector_context": sector_context_words(sector, industry),
        "currency": info.get("currency"),
        "exchange": info.get("exchange"),
        "resolved": bool(company_name),
    }


# ─────────────────────────────────────────────────────────────────────────────
# YAHOO FINANCE NEWS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yahoo_news(ticker: str, max_items: int = 20, yf_ticker: Optional[Any] = None) -> List[Dict[str, Any]]:
    try:
        t = yf_ticker if yf_ticker is not None else yf.Ticker(ticker)
        raw_news = t.news or []
    except Exception as e:
        return [{
            "source": "Yahoo Finance",
            "title": f"Error fetching Yahoo Finance news: {e}",
            "publisher": "System",
            "date": "N/A",
            "link": "",
            "error": True,
        }]

    items = []

    for item in raw_news[:max_items]:
        try:
            title = item.get("title")
            publisher = item.get("publisher")
            link = item.get("link")
            published = item.get("providerPublishTime")
            summary = item.get("summary")

            # Newer yfinance shape nests everything under "content".
            if not title and isinstance(item.get("content"), dict):
                content = item.get("content", {})
                title = content.get("title")
                summary = content.get("summary") or content.get("description")
                provider = content.get("provider")
                if isinstance(provider, dict):
                    publisher = provider.get("displayName")
                elif provider:
                    publisher = provider
                canonical = content.get("canonicalUrl")
                if isinstance(canonical, dict):
                    link = canonical.get("url")
                else:
                    link = content.get("url")
                published = content.get("pubDate") or published

            epoch = timestamp_to_epoch(published)

            items.append({
                "source": "Yahoo Finance",
                "title": clean_text(title),
                # v2 never carried a body, so multi-word positives like
                # "better than expected" could only ever match a headline.
                "body": clean_text(summary)[:1000],
                "publisher": clean_text(publisher) or "Yahoo Finance",
                "epoch": epoch,
                "date": epoch_to_date(epoch),
                "link": link or "",
                "raw": item,
            })
        except Exception:
            continue

    return [x for x in items if x.get("title")]


# ─────────────────────────────────────────────────────────────────────────────
# REDDIT SEARCH
# ─────────────────────────────────────────────────────────────────────────────

class RedditBlocked(RuntimeError):
    """Raised once Reddit has refused us; remaining searches are skipped."""


class RedditClient:
    """
    One session, bounded retries, and a hard stop after repeated refusals.

    v2 opened a new connection per request, had no backoff, and kept firing
    all 72 searches after the first refusal.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "public-sentiment-evaluator/3.0 (personal screening tool)",
            "Accept": "application/json",
        })
        self.consecutive_blocks = 0
        self.blocked = False
        self.block_reason = ""
        self.request_count = 0

    def get_json(self, url: str, attempts: int = 3) -> Dict[str, Any]:
        if self.blocked:
            raise RedditBlocked(self.block_reason)

        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                self.request_count += 1
                resp = self.session.get(url, timeout=14)
            except requests.RequestException as e:
                last_error = e
                time.sleep(1.5 * (2 ** attempt))
                continue

            if resp.status_code in (403, 429):
                self.consecutive_blocks += 1
                if self.consecutive_blocks >= REDDIT_MAX_CONSECUTIVE_BLOCKS:
                    self.blocked = True
                    self.block_reason = (
                        f"Reddit refused the request ({resp.status_code}) "
                        f"{self.consecutive_blocks}x in a row. Reddit blocks "
                        "unauthenticated JSON search from many IP ranges. "
                        "Remaining Reddit searches were skipped."
                    )
                    raise RedditBlocked(self.block_reason)
                retry_after = resp.headers.get("retry-after")
                delay = 2.0 * (2 ** attempt)
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(min(delay, 16.0))
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                continue

            resp.raise_for_status()
            self.consecutive_blocks = 0
            return resp.json()

        raise RuntimeError(f"Reddit request failed after {attempts} attempts: {last_error}")


def build_reddit_queries(company_info: Dict[str, Any]) -> List[str]:
    """
    Precise, sector-aware queries. Ticker searches always carry context
    because a bare 3-4 letter token is ambiguous for most tickers.
    """
    base = company_info["base_ticker"]
    company_terms = company_info.get("company_terms", [])
    normalized = company_info.get("normalized_company_name", "")
    sector_terms = [w for w in company_info.get("sector_context", []) if " " not in w][:2]

    queries = []

    for term in company_terms:
        if len(term) >= 6:
            queries.append(f'"{term}"')
            queries.append(f'"{term}" stock')
            queries.append(f'"{term}" earnings')

    if normalized:
        words = normalized.split()
        if len(words) >= 2:
            short_name = " ".join(words[:2])
            queries.append(f'"{short_name}" stock')
            for term in sector_terms:
                queries.append(f'"{short_name}" {term}')

    # Ticker queries: cashtag first, then context-qualified forms.
    queries.extend([
        f'"${base}" stock',
        f'"${base}" investing',
        f'"{base}" stock',
        f'"{base}" earnings stock',
    ])
    for term in sector_terms:
        queries.append(f'"{base}" {term} stock')

    if normalized:
        # Ticker + name is the single most precise query available.
        queries.insert(0, f'"{base}" "{normalized.split()[0]}"')

    seen = set()
    out = []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)

    return out


def fetch_reddit_search_global(
    client: RedditClient,
    query: str,
    max_items: int = 25,
    sort: str = "new",
    time_filter: str = "month",
) -> List[Dict[str, Any]]:
    encoded = urllib.parse.quote(query)
    url = (
        f"https://www.reddit.com/search.json?q={encoded}"
        f"&sort={sort}&t={time_filter}&limit={min(max_items, 100)}"
    )
    return parse_reddit_children(client.get_json(url))


def fetch_reddit_search_subreddit(
    client: RedditClient,
    subreddit: str,
    query: str,
    max_items: int = 10,
    sort: str = "new",
    time_filter: str = "month",
) -> List[Dict[str, Any]]:
    encoded = urllib.parse.quote(query)
    sr = urllib.parse.quote(subreddit)
    url = (
        f"https://www.reddit.com/r/{sr}/search.json?q={encoded}"
        f"&restrict_sr=1&sort={sort}&t={time_filter}&limit={min(max_items, 100)}"
    )
    return parse_reddit_children(client.get_json(url))


def parse_reddit_children(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    posts = []
    children = data.get("data", {}).get("children", [])

    for child in children:
        d = child.get("data", {})
        title = clean_text(d.get("title"))
        selftext = clean_text(d.get("selftext"))
        subreddit = clean_text(d.get("subreddit"))
        epoch = timestamp_to_epoch(d.get("created_utc"))
        permalink = d.get("permalink", "")

        if not title:
            continue

        posts.append({
            "source": "Reddit",
            "title": title,
            "body": selftext[:1000],
            "subreddit": subreddit,
            "epoch": epoch,
            "date": epoch_to_date(epoch),
            "score": d.get("score", 0),
            "comments": d.get("num_comments", 0),
            "link": f"https://www.reddit.com{permalink}" if permalink else "",
            "raw": d,
        })

    return posts


def reddit_relevance_score(post: Dict[str, Any], company_info: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Identity is established FIRST, then the ambiguity guard runs only if no
    identity signal fired.

    v2 awarded +5 for a company-name match and +4 for a cashtag, then
    subtracted 5 for "bare ticker" regardless -- so a post titled
    "$AEM is going to the moon" scored 0 and was thrown away.

    Rules:
      STRONG (kept on its own):
        - cashtag $TICKER
        - company term of 4+ chars as a whole word
        - short company term as a whole word, plus stock or sector context
      MODERATE (bare ticker, no identity signal):
        - kept inside a finance/sector subreddit with stock OR sector context
        - kept outside one only with stock AND sector context
        - otherwise rejected as an ambiguous acronym

    The guard is generic. v2's was hardcoded to one ticker, so every other
    ticker had no acronym protection at all.
    """
    title = clean_text(post.get("title"))
    body = clean_text(post.get("body"))
    subreddit = clean_text(post.get("subreddit"))
    text = f"{title} {body}"

    base = company_info["base_ticker"]
    company_terms = company_info.get("company_terms", [])
    sector_words = company_info.get("sector_context", [])

    reasons: List[str] = []
    score = 0

    stock_context = contains_any(text, STOCK_CONTEXT_WORDS, apply_guards=False)
    sector_context = contains_any(text, sector_words, apply_guards=False)
    in_target_sub = subreddit.lower() in [s.lower() for s in TARGET_SUBREDDITS]

    # ---- identity signals -------------------------------------------------
    strong_identity = False

    if word_boundary_match(text, f"${base}"):
        strong_identity = True
        score += 5
        reasons.append(f"cashtag: ${base}")

    for term in company_terms:
        if not word_boundary_match(text, term):
            continue
        if len(term) >= 4:
            strong_identity = True
            score += 5
            reasons.append(f"company name: {term}")
        elif stock_context or sector_context:
            # A 2-3 char "name" ("3m") is only identity with context, or it
            # matches a 3m HDMI cable.
            strong_identity = True
            score += 4
            reasons.append(f"short company name + context: {term}")
        break

    ticker_present = word_boundary_match(text, base)

    # ---- moderate path: bare ticker, no identity signal -------------------
    if not strong_identity and ticker_present:
        if in_target_sub and (stock_context or sector_context):
            score += 4
            reasons.append(f"ticker in r/{subreddit} with topic context")
        elif not in_target_sub and stock_context and sector_context:
            score += 4
            reasons.append(f"ticker with both stock and {company_info.get('sector') or 'sector'} context")
        else:
            score -= 5
            reasons.append(f"ambiguous bare '{base}' acronym rejected")

    # ---- supporting signals (never enough on their own) -------------------
    if strong_identity:
        if stock_context:
            score += 1
            reasons.append("stock context present")
        if in_target_sub:
            score += 1
            reasons.append(f"finance/sector subreddit: r/{subreddit}")

    return score, reasons


def fetch_reddit_sentiment_items(
    company_info: Dict[str, Any],
    max_items: int = 40,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Company-aware Reddit retrieval. Stops early when Reddit refuses, and
    reports that refusal instead of quietly returning nothing.
    """
    queries = build_reddit_queries(company_info)
    client = RedditClient()

    diagnostics = {
        "queries_built": queries,
        "queries_issued": [],
        "raw_posts_seen": 0,
        "unique_candidates": 0,
        "posts_after_relevance_filter": 0,
        "dropped_stale": 0,
        "rejected_examples": [],
        "errors": [],
        "blocked": False,
        "block_reason": "",
        "requests_made": 0,
    }

    seen = set()
    candidates: List[Dict[str, Any]] = []
    now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()

    def absorb(results: List[Dict[str, Any]]) -> None:
        diagnostics["raw_posts_seen"] += len(results)
        for p in results:
            age = age_days(p.get("epoch"), now_epoch)
            if age is not None and age > MAX_AGE_DAYS:
                diagnostics["dropped_stale"] += 1
                continue
            key = p.get("link") or p.get("title")
            if key and key not in seen:
                seen.add(key)
                candidates.append(p)

    # Global search. v2 sliced this to queries[:12], which silently discarded
    # the ticker/cashtag queries its own docstring advertised.
    for q in queries:
        if client.blocked:
            break
        try:
            diagnostics["queries_issued"].append(q)
            absorb(fetch_reddit_search_global(client, q, max_items=12))
        except RedditBlocked as e:
            diagnostics["blocked"] = True
            diagnostics["block_reason"] = str(e)
            break
        except Exception as e:
            diagnostics["errors"].append(f"global search failed for {q}: {e}")
        time.sleep(0.5)

    # Subreddit-restricted search with the most specific terms available.
    subreddit_queries = [f'"{t}"' for t in company_info.get("company_terms", [])[:2]]
    base = company_info["base_ticker"]
    subreddit_queries.extend([f'"${base}"', f'"{base}" stock'])

    for sr in TARGET_SUBREDDITS:
        if client.blocked or len(candidates) >= max_items * 3:
            break
        for q in subreddit_queries[:4]:
            if client.blocked or len(candidates) >= max_items * 3:
                break
            try:
                absorb(fetch_reddit_search_subreddit(client, sr, q, max_items=5))
            except RedditBlocked as e:
                diagnostics["blocked"] = True
                diagnostics["block_reason"] = str(e)
                break
            except Exception as e:
                if len(diagnostics["errors"]) < 8:
                    diagnostics["errors"].append(f"r/{sr} search failed for {q}: {e}")
            time.sleep(0.25)

    diagnostics["requests_made"] = client.request_count
    diagnostics["unique_candidates"] = len(candidates)

    relevant = []
    rejected = []

    for post in candidates:
        score, reasons = reddit_relevance_score(post, company_info)
        post["relevance_score"] = score
        post["relevance_reasons"] = reasons

        if score >= RELEVANCE_THRESHOLD:
            relevant.append(post)
        elif len(rejected) < 5:
            rejected.append({
                "title": post.get("title"),
                "subreddit": post.get("subreddit"),
                "score": score,
                "reasons": reasons,
            })

    relevant.sort(
        key=lambda p: (
            p.get("relevance_score", 0),
            (p.get("score") or 0) + 2 * (p.get("comments") or 0),
        ),
        reverse=True,
    )

    diagnostics["posts_after_relevance_filter"] = len(relevant)
    diagnostics["rejected_examples"] = rejected

    return relevant[:max_items], diagnostics


# ─────────────────────────────────────────────────────────────────────────────
# SENTIMENT SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _empty_scores(note: str) -> Dict[str, Any]:
    return {
        "overall": None,
        "tone_score": None,
        "catalyst_score": None,
        "hype_risk": None,
        "macro_score": None,
        "confidence": 0.0,
        "positive_hits": [],
        "negative_hits": [],
        "catalyst_hits": [],
        "hype_hits": [],
        "macro_hits": [],
        "item_count": 0,
        "dropped_stale": 0,
        "negation_flips": 0,
        "interpretation": note,
    }


def score_items(
    items: List[Dict[str, Any]],
    source_type: str = "generic",
    now_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Returns a sentiment score plus two NON-DIRECTIONAL context gauges.

    Key differences from v2:
      - tone is a per-item weighted mean pushed through tanh, so it measures
        sentiment rather than article count, and cannot saturate off a
        handful of hits.
      - catalyst/macro/hype are divided by the SUM OF WEIGHTS, not by the raw
        item count, so a heavily upvoted post no longer inflates them.
      - `overall` is centred on 5.0 and spans 0-10.
      - an empty source returns overall=None, not a 5.0 that votes in the
        combined view.
      - confidence reflects recency-decayed volume, source diversity AND
        agreement between items, rather than raw item count alone.
      - negated keywords flip polarity, so "failed to beat" no longer counts
        as a positive hit.
    """
    now_epoch = now_epoch if now_epoch is not None else dt.datetime.now(dt.timezone.utc).timestamp()

    if not items:
        return _empty_scores("No relevant items found. This source contributed nothing.")

    positive_words = flatten_keyword_dict(POSITIVE_KEYWORDS)
    negative_words = flatten_keyword_dict(NEGATIVE_KEYWORDS)

    pos_hits, neg_hits, catalyst_hits, hype_hits, macro_hits = [], [], [], [], []

    weighted_pos = 0.0
    weighted_neg = 0.0
    catalyst_points = 0.0
    hype_points = 0.0
    macro_points = 0.0
    total_weight = 0.0

    per_item_balance: List[float] = []
    distinct_sources = set()
    effective_count = 0.0

    usable_count = 0
    dropped_stale = 0
    negation_flips = 0
    total_engagement = 0.0

    for item in items:
        if item.get("error"):
            continue

        title = clean_text(item.get("title"))
        body = clean_text(item.get("body"))
        combined = f"{title} {body}".strip()
        if not combined:
            continue

        age = age_days(item.get("epoch"), now_epoch)
        if age is not None and age > MAX_AGE_DAYS:
            dropped_stale += 1
            continue

        usable_count += 1
        recency = recency_weight(age)
        effective_count += recency

        if source_type == "reddit":
            engagement = (item.get("score") or 0) + 2 * (item.get("comments") or 0)
            weight = 1.0 + min(max(engagement, 0), 500) / 500
            weight += min(item.get("relevance_score", 0), 10) / 20
            weight *= recency
            total_engagement += max(engagement, 0)
            distinct_sources.add(clean_text(item.get("subreddit")).lower())
        else:
            weight = recency
            distinct_sources.add(clean_text(item.get("publisher")).lower())

        weight = max(weight, 1e-6)

        ph, nh, flips = classify_polarity(combined, positive_words, negative_words)
        negation_flips += flips
        ch = contains_any(combined, CATALYST_KEYWORDS)
        hh = contains_any(combined, HYPE_KEYWORDS)
        mh = contains_any(combined, MACRO_GEOPOLITICAL_KEYWORDS)

        pos_hits.extend(ph)
        neg_hits.extend(nh)
        catalyst_hits.extend(ch)
        hype_hits.extend(hh)
        macro_hits.extend(mh)

        weighted_pos += len(ph) * weight
        weighted_neg += len(nh) * weight
        catalyst_points += min(len(ch), 4) * weight
        hype_points += min(len(hh), 4) * weight
        macro_points += min(len(mh), 4) * weight
        total_weight += weight

        per_item_balance.append(float(len(ph) - len(nh)))

    if usable_count == 0:
        note = "No usable relevant items found. This source contributed nothing."
        if dropped_stale:
            note = (
                f"All {dropped_stale} item(s) were older than {int(MAX_AGE_DAYS)} days "
                "and were dropped. This source contributed nothing."
            )
        out = _empty_scores(note)
        out["dropped_stale"] = dropped_stale
        return out

    # ---- tone: per-item weighted mean, bounded -----------------------------
    mean_balance = (weighted_pos - weighted_neg) / total_weight
    tone_score = clamp(5.0 + 5.0 * math.tanh(mean_balance * TONE_SENSITIVITY))

    # ---- context gauges: weighted mean of per-item hit counts, 0-10 --------
    catalyst_score = clamp((catalyst_points / total_weight) * 2.5)
    hype_risk = clamp((hype_points / total_weight) * 2.5)
    macro_score = clamp((macro_points / total_weight) * 2.5)

    # ---- confidence: volume + source diversity + agreement ----------------
    # Volume is counted in RECENCY-DECAYED items, so 20 stale articles do not
    # buy the same confidence as 20 fresh ones.
    count_term = (min(effective_count, 20.0) / 20.0) * 4.0
    diversity_term = (min(len(distinct_sources), 8) / 8) * 3.0
    if len(per_item_balance) >= 2:
        dispersion = statistics.pstdev(per_item_balance)
        agreement_term = (1.0 - min(dispersion / 2.0, 1.0)) * 2.0
    else:
        agreement_term = 0.0
    confidence = clamp(1.0 + count_term + diversity_term + agreement_term)

    # ---- overall: centred on 5.0, shrunk toward neutral by hype -----------
    # Hype does not make sentiment negative; it makes the reading less
    # trustworthy, so it pulls the result toward neutral in either direction.
    hype_shrink = (hype_risk / 10.0) * HYPE_SHRINK_MAX
    overall = clamp(5.0 + (tone_score - 5.0) * (1.0 - hype_shrink))

    return {
        "overall": round(overall, 2),
        "tone_score": round(tone_score, 2),
        "catalyst_score": round(catalyst_score, 2),
        "hype_risk": round(hype_risk, 2),
        "macro_score": round(macro_score, 2),
        "confidence": round(confidence, 2),
        "positive_hits": most_common(pos_hits),
        "negative_hits": most_common(neg_hits),
        "catalyst_hits": most_common(catalyst_hits),
        "hype_hits": most_common(hype_hits),
        "macro_hits": most_common(macro_hits),
        "item_count": usable_count,
        "effective_item_count": round(effective_count, 2),
        "negation_flips": negation_flips,
        "dropped_stale": dropped_stale,
        "distinct_sources": len(distinct_sources),
        "interpretation": interpret_sentiment(
            overall, tone_score, catalyst_score, hype_risk, macro_score, confidence
        ),
    }


def interpret_sentiment(
    overall: float,
    tone: float,
    catalyst: float,
    hype: float,
    macro: float,
    confidence: float,
) -> str:
    # The direction is always stated. Low confidence qualifies the reading
    # rather than replacing it, so the words never go silent on a score the
    # reader can see printed directly above them.
    if overall >= 7.5:
        base = "Public sentiment appears positive."
    elif overall >= 6.0:
        base = "Public sentiment appears mildly positive."
    elif overall >= 4.5:
        base = "Public sentiment appears mixed or neutral."
    elif overall >= 3.0:
        base = "Public sentiment appears mildly negative."
    else:
        base = "Public sentiment appears negative."

    if confidence < 3:
        base = base.rstrip(".") + ", but this is a low-confidence reading built on thin data."

    notes = []

    # Catalyst and macro are prevalence gauges, so they are phrased as
    # "how much is being discussed", never as good or bad news.
    if catalyst >= 7:
        notes.append("heavy event/catalyst coverage")
    elif catalyst >= 4:
        notes.append("moderate event/catalyst coverage")

    if hype >= 6:
        notes.append("elevated hype/crowding risk, reading shrunk toward neutral")
    elif hype >= 3.5:
        notes.append("some hype/crowding signals")

    if macro >= 6:
        notes.append("heavily macro/geopolitically framed")
    elif macro >= 3.5:
        notes.append("moderate macro/geopolitical framing")

    if tone <= 3.5:
        notes.append("negative tone signals")
    elif tone >= 7:
        notes.append("positive tone signals")

    if notes:
        return base + " Key notes: " + "; ".join(notes) + "."
    return base


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def combine_sources(yahoo_scores: Dict[str, Any], reddit_scores: Dict[str, Any]) -> Dict[str, Any]:
    """
    Only sources that actually returned items get a vote.

    v2 gave an empty source a placeholder 5.0 and a confidence of 1.0, which
    still dragged the combined score toward neutral and could trip the
    "sources disagree" note against a source that had no data at all.

    Confidence is not an average either: two sources that agree should be
    more trustworthy than either alone, which averaging can never express.
    """
    contributing = []
    if yahoo_scores.get("item_count", 0) > 0:
        contributing.append(("yahoo", YAHOO_SOURCE_WEIGHT, yahoo_scores))
    if reddit_scores.get("item_count", 0) > 0:
        contributing.append(("reddit", REDDIT_SOURCE_WEIGHT, reddit_scores))

    if not contributing:
        return {
            "overall": None,
            "confidence": 0.0,
            "contributing_sources": [],
            "interpretation": (
                "No data from either source, so there is no combined reading. "
                "This is an absence of evidence, not a neutral verdict."
            ),
        }

    if len(contributing) == 1:
        name, _, scores = contributing[0]
        return {
            "overall": scores["overall"],
            "confidence": scores["confidence"],
            "contributing_sources": [name],
            "interpretation": interpret_combined(
                scores["overall"], scores["confidence"], yahoo_scores, reddit_scores, [name]
            ),
        }

    total_w = sum(w * (s["confidence"] / 10.0) for _, w, s in contributing)
    if total_w <= 0:
        combined_score = statistics.fmean([s["overall"] for _, _, s in contributing])
    else:
        combined_score = sum(
            s["overall"] * w * (s["confidence"] / 10.0) for _, w, s in contributing
        ) / total_w

    # Corroboration raises confidence above the better source; disagreement
    # does not.
    confidences = [s["confidence"] for _, _, s in contributing]
    gap = abs(yahoo_scores["overall"] - reddit_scores["overall"])
    agreement_bonus = 1.5 * (1.0 - min(gap / 5.0, 1.0))
    combined_confidence = clamp(max(confidences) + agreement_bonus)

    names = [n for n, _, _ in contributing]
    return {
        "overall": round(combined_score, 2),
        "confidence": round(combined_confidence, 2),
        "contributing_sources": names,
        "interpretation": interpret_combined(
            combined_score, combined_confidence, yahoo_scores, reddit_scores, names
        ),
    }


def interpret_combined(
    combined_score: Optional[float],
    confidence: float,
    yahoo_scores: Dict[str, Any],
    reddit_scores: Dict[str, Any],
    contributing: List[str],
) -> str:
    if combined_score is None:
        return "No combined reading is available."

    if combined_score >= 7.5:
        base = "Combined public sentiment is positive."
    elif combined_score >= 6.0:
        base = "Combined public sentiment is mildly positive."
    elif combined_score >= 4.5:
        base = "Combined public sentiment is mixed or neutral."
    elif combined_score >= 3.0:
        base = "Combined public sentiment is mildly negative."
    else:
        base = "Combined public sentiment is negative."

    if confidence < 3:
        base = base.rstrip(".") + ", but source coverage is too thin to rely on."

    if len(contributing) == 1:
        missing = "Reddit" if contributing[0] == "yahoo" else "Yahoo Finance"
        base += (
            f" Only {contributing[0].title()} returned usable items; {missing} "
            "contributed nothing and was excluded rather than counted as neutral."
        )
        return base

    # Both sources have items, so a disagreement note is meaningful.
    if abs(yahoo_scores["overall"] - reddit_scores["overall"]) >= 2.5:
        base += " Yahoo Finance and Reddit disagree materially, so review the underlying items."

    if (reddit_scores.get("hype_risk") or 0) >= 6:
        base += " Reddit hype risk is elevated; do not treat social attention as fundamental confirmation."

    return base


def analyze_public_sentiment_yahoo_reddit(
    ticker: str,
    yahoo_limit: int = 20,
    reddit_limit: int = 40,
) -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()

    # One Ticker object for both the profile and the news lookup.
    try:
        yf_ticker = yf.Ticker(ticker)
    except Exception:
        yf_ticker = None

    company_info = fetch_company_info(ticker, yf_ticker=yf_ticker)

    yahoo_items = fetch_yahoo_news(ticker, max_items=yahoo_limit, yf_ticker=yf_ticker)
    yahoo_scores = score_items(yahoo_items, source_type="yahoo", now_epoch=now_epoch)

    reddit_items, reddit_diagnostics = fetch_reddit_sentiment_items(
        company_info=company_info,
        max_items=reddit_limit,
    )
    reddit_scores = score_items(reddit_items, source_type="reddit", now_epoch=now_epoch)

    return {
        "ticker": ticker,
        "company_info": company_info,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "yahoo": {"items": yahoo_items, "scores": yahoo_scores},
        "reddit": {
            "items": reddit_items,
            "scores": reddit_scores,
            "diagnostics": reddit_diagnostics,
        },
        "combined": combine_sources(yahoo_scores, reddit_scores),
    }


# ─────────────────────────────────────────────────────────────────────────────
# REPORT DISPLAY
# ─────────────────────────────────────────────────────────────────────────────

def print_score_rows(scores: Dict[str, Any]) -> None:
    """
    Sentiment and context are printed as separate blocks, because the context
    gauges are prevalence measures and do not feed the sentiment number.
    """
    if scores.get("item_count", 0) == 0:
        print(f"  {YELLOW}This source returned no usable items — no score is produced.{RESET}")
        return

    sentiment_rows = [
        ("Overall Sentiment", scores.get("overall"), "sentiment"),
        ("Tone Score", scores.get("tone_score"), "sentiment"),
        ("Data Confidence", scores.get("confidence"), "confidence"),
    ]
    for label, value, kind in sentiment_rows:
        print(f"  {label:<30} {bar(value, 14)}  {colour_score(value, kind=kind)} / 10")

    print(f"  {DIM}context gauges below are prevalence, not direction — they do not move the score{RESET}")
    gauge_rows = [
        ("Catalyst/Event Coverage", scores.get("catalyst_score"), "gauge"),
        ("Macro/Geopolitical Framing", scores.get("macro_score"), "gauge"),
        ("Hype/Crowding Risk", scores.get("hype_risk"), "risk"),
    ]
    for label, value, kind in gauge_rows:
        print(f"  {label:<30} {bar(value, 14)}  {colour_score(value, kind=kind)} / 10")

    flips = scores.get("negation_flips", 0)
    if flips:
        print(f"  {DIM}({flips} keyword(s) flipped by negation, e.g. \"failed to beat\"){RESET}")

    stale = scores.get("dropped_stale", 0)
    if stale:
        print(f"  {DIM}({stale} item(s) older than {int(MAX_AGE_DAYS)} days were dropped){RESET}")


def print_hits(title: str, hits: List[Tuple[str, int]]) -> None:
    if not hits:
        return
    readable = ", ".join([f"{term} ({count})" for term, count in hits])
    print(f"  {title:<30} {readable}")


def print_items(items: List[Dict[str, Any]], source_type: str, max_show: int = 7) -> None:
    clean_items = [x for x in items if not x.get("error") and x.get("title")]

    if not clean_items:
        errors = [x for x in items if x.get("error")]
        if errors:
            print(f"  {RED}Data issue:{RESET} {errors[0].get('title')}")
        else:
            print(f"  {YELLOW}No relevant items found.{RESET}")
        return

    for idx, item in enumerate(clean_items[:max_show], 1):
        title = item.get("title", "Untitled")
        date = item.get("date", "N/A")

        if source_type == "reddit":
            subreddit = item.get("subreddit", "N/A")
            score = item.get("score", 0)
            comments = item.get("comments", 0)
            relevance = item.get("relevance_score", "N/A")
            reasons = "; ".join(item.get("relevance_reasons", [])[:3])
            print(f"  {idx}. [{date}] r/{subreddit} — {title}")
            print(f"     Reddit score: {score} · Comments: {comments} · Relevance: {relevance}")
            if reasons:
                print(f"     Matched because: {reasons}")
        else:
            publisher = item.get("publisher", "N/A")
            print(f"  {idx}. [{date}] {publisher} — {title}")


def print_reddit_diagnostics(diag: Dict[str, Any]) -> None:
    if diag.get("blocked"):
        print(f"  {RED}{BOLD}Reddit unavailable:{RESET} {diag.get('block_reason')}")
        print(f"  {YELLOW}The Reddit section below is an ABSENCE of data, not a neutral verdict.{RESET}")

    rows = [
        ("Reddit requests made", diag.get("requests_made", 0)),
        ("Search hits returned (with dupes)", diag.get("raw_posts_seen", 0)),
        ("Unique posts after dedupe", diag.get("unique_candidates", 0)),
        (f"Dropped as stale (>{int(MAX_AGE_DAYS)}d)", diag.get("dropped_stale", 0)),
        ("Passed relevance filter", diag.get("posts_after_relevance_filter", 0)),
    ]
    for label, value in rows:
        print(f"  {label:<36} {value}")

    issued = diag.get("queries_issued", [])
    built = diag.get("queries_built", [])
    if built:
        print(f"  {'Search queries built / issued':<36} {len(built)} / {len(issued)}")
        preview = ", ".join(issued[:6]) if issued else "none"
        if len(issued) > 6:
            preview += ", ..."
        print(f"  {'Queries issued':<36} {preview}")

    errors = diag.get("errors", [])
    if errors:
        print(f"  {YELLOW}Reddit search notes:{RESET} {errors[0]}")
        if len(errors) > 1:
            print(f"  {DIM}Additional Reddit endpoint errors suppressed: {len(errors) - 1}{RESET}")


def print_sentiment_report(result: Dict[str, Any]) -> None:
    W = 76

    def rule(ch: str = "═") -> None:
        print("  " + ch * W)

    def header(text: str) -> None:
        rule("─")
        print(f"  {BOLD}{text}{RESET}")
        rule("─")

    ticker = result.get("ticker")
    company_info = result.get("company_info", {})
    company_name = company_info.get("company_name") or "N/A"

    print()
    rule()
    print(f"  {BOLD}{CYAN}PUBLIC SENTIMENT EVALUATOR — YAHOO FINANCE + REDDIT v3.0{RESET}")
    print(f"  {company_name} ({ticker})")
    if not company_info.get("resolved"):
        print(f"  {YELLOW}Company name could not be resolved from Yahoo — "
              f"Reddit search fell back to ticker-only queries.{RESET}")
    if company_info.get("normalized_company_name"):
        print(f"  Reddit search identity: {company_info.get('normalized_company_name')}")
    if company_info.get("sector"):
        print(f"  Sector/industry: {company_info.get('sector')} / {company_info.get('industry')}")
    print(f"  Generated: {result.get('generated_at')}")
    rule()

    # Yahoo section
    yahoo_scores = result["yahoo"]["scores"]
    yahoo_items = result["yahoo"]["items"]

    print()
    header("YAHOO FINANCE PUBLIC SENTIMENT")
    print_score_rows(yahoo_scores)
    print()
    print(f"  Interpretation: {yahoo_scores.get('interpretation')}")
    print()
    print_hits("Positive signals", yahoo_scores.get("positive_hits", []))
    print_hits("Negative signals", yahoo_scores.get("negative_hits", []))
    print_hits("Catalyst/event signals", yahoo_scores.get("catalyst_hits", []))
    print_hits("Macro/geopolitical signals", yahoo_scores.get("macro_hits", []))
    print_hits("Hype/crowding signals", yahoo_scores.get("hype_hits", []))
    print()
    print(f"  {BOLD}Recent Yahoo Finance items:{RESET}")
    print_items(yahoo_items, source_type="yahoo")

    # Reddit section
    reddit_scores = result["reddit"]["scores"]
    reddit_items = result["reddit"]["items"]
    reddit_diag = result["reddit"]["diagnostics"]

    print()
    header("REDDIT PUBLIC SENTIMENT")
    print_score_rows(reddit_scores)
    print()
    print(f"  Interpretation: {reddit_scores.get('interpretation')}")
    print()
    print_reddit_diagnostics(reddit_diag)
    print()
    print_hits("Positive signals", reddit_scores.get("positive_hits", []))
    print_hits("Negative signals", reddit_scores.get("negative_hits", []))
    print_hits("Catalyst/event signals", reddit_scores.get("catalyst_hits", []))
    print_hits("Macro/geopolitical signals", reddit_scores.get("macro_hits", []))
    print_hits("Hype/crowding signals", reddit_scores.get("hype_hits", []))
    print()
    print(f"  {BOLD}Recent relevant Reddit items:{RESET}")
    print_items(reddit_items, source_type="reddit")

    # Combined section
    combined = result["combined"]

    print()
    header("COMBINED PUBLIC SENTIMENT VIEW")
    contributing = combined.get("contributing_sources", [])
    if not contributing:
        print(f"  {YELLOW}No source returned usable items — no combined score.{RESET}")
    else:
        print(f"  {'Combined Score':<30} {bar(combined.get('overall'), 14)}  "
              f"{colour_score(combined.get('overall'))} / 10")
        print(f"  {'Combined Confidence':<30} {bar(combined.get('confidence'), 14)}  "
              f"{colour_score(combined.get('confidence'), kind='confidence')} / 10")
        print(f"  {DIM}Contributing sources: {', '.join(contributing)}{RESET}")
    print()
    print(f"  Interpretation: {combined.get('interpretation')}")

    print()
    rule()
    print(f"  {YELLOW}⚠  Sentiment is a screening layer only. It is not financial advice and should not override fundamentals.{RESET}")
    print(f"  {YELLOW}⚠  Negation is handled by a {NEGATION_WINDOW}-token window, not by parsing. Sarcasm and complex clauses still fool it.{RESET}")
    print(f"  {YELLOW}⚠  Scores are not comparable across tickers without a peer baseline.{RESET}")
    rule()
    print()


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT JSON OPTION
# ─────────────────────────────────────────────────────────────────────────────

def export_json(result: Dict[str, Any], filename: Optional[str] = None) -> str:
    ticker = result.get("ticker", "ticker").replace(".", "_")
    if not filename:
        filename = f"sentiment_{ticker}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    def simplify_item(item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in item.items() if k != "raw"}

    exportable = {
        **result,
        "yahoo": {
            "scores": result["yahoo"]["scores"],
            "items": [simplify_item(x) for x in result["yahoo"]["items"]],
        },
        "reddit": {
            "scores": result["reddit"]["scores"],
            "items": [simplify_item(x) for x in result["reddit"]["items"]],
            "diagnostics": result["reddit"]["diagnostics"],
        },
    }

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(exportable, f, indent=2, ensure_ascii=False)

    return filename


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n  {BOLD}{CYAN}╔════════════════════════════════════════════════════════════╗{RESET}")
    print(f"  {BOLD}{CYAN}║   Public Sentiment Evaluator — Yahoo + Reddit v3.0        ║{RESET}")
    print(f"  {BOLD}{CYAN}╚════════════════════════════════════════════════════════════╝{RESET}")
    print(f"  {DIM}Company-aware Reddit search. No API keys required.{RESET}")

    while True:
        try:
            ticker = input(f"\n  {BOLD}Enter ticker (or 'q' to quit): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n")
            break

        if ticker.lower() in ("q", "quit", "exit"):
            print("  Goodbye.\n")
            break

        if not ticker:
            continue

        print(f"\n  {CYAN}Resolving company identity and fetching sentiment for {ticker.upper()}...{RESET}")
        result = analyze_public_sentiment_yahoo_reddit(ticker)

        print_sentiment_report(result)

        try:
            save = input(f"  {BOLD}Export this sentiment report to JSON? (y/n): {RESET}").strip().lower()
            if save in ("y", "yes"):
                filename = export_json(result)
                print(f"  {GREEN}Saved JSON report: {filename}{RESET}")
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.\n")
            break


if __name__ == "__main__":
    main()

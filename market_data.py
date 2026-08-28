#!/usr/bin/env python3
r"""
market_data.py - shared market-data fetch layer for the stocks project.

Every other script in this project imports from here. Nothing else should
call yfinance directly: this module owns the HTTP session, the on-disk
cache, the retry/backoff policy, and ticker identity resolution, so those
policies stay in one place and Yahoo sees one well-behaved client.

What you get:

    get_session()                       reusable curl_cffi/requests session
    cached_fetch(key, fn, ttl)          JSON cache on disk, TTL per call
    fetch_with_backoff(fn, *a, **kw)    exponential backoff, returns None
    dedupe_tickers(tickers)             collapse dual-class / cross-listings

    get_info / get_price_history / get_avg_volume / get_company_name
    cached_screener(name, fn)

Failure contract: nothing in here raises on network failure. When the
network is down, calls return None (or fall back to a stale cache entry if
one exists), and the caller is expected to handle None.

Setup:
    pip install yfinance curl_cffi requests

Usage:
    py market_data.py            # self-test (cache hit/miss + name rules)
    py market_data.py -v         # same, with debug logging
    py market_data.py --dedupe   # also demo dedupe_tickers() (more calls)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
import yfinance as yf

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

# Project root. On the machine this was written for that resolves to
# C:\Users\joey\stocks, because this file lives there - the cache always
# sits next to the code. Override with STOCKS_DIR / STOCKS_CACHE_DIR.
BASE_DIR  = Path(os.environ.get("STOCKS_DIR") or Path(__file__).resolve().parent)
CACHE_DIR = Path(os.environ.get("STOCKS_CACHE_DIR") or (BASE_DIR / "cache"))

# Cache subfolders, one per kind of data. cached_fetch() writes into one of
# these; anything unrecognized falls back to DEFAULT_CACHE_TYPE.
CACHE_TYPES = ("financials", "prices", "screener")
DEFAULT_CACHE_TYPE = "financials"

# Named TTLs (seconds). Fundamentals move quarterly, prices and screens daily.
TTL_FINANCIALS = 7 * 24 * 60 * 60   # 7 days
TTL_PRICE      = 1 * 24 * 60 * 60   # 1 day
TTL_SCREENER   = 1 * 24 * 60 * 60   # 1 day

# Browser fingerprint for curl_cffi. Yahoo blocks plain-python TLS profiles.
CURL_IMPERSONATE = os.environ.get("STOCKS_IMPERSONATE", "chrome")

# Set STOCKS_FORCE_REQUESTS=1 where TLS impersonation can't get out (some
# corporate proxies / sandboxes reset curl_cffi connections).
FORCE_REQUESTS_SESSION = os.environ.get("STOCKS_FORCE_REQUESTS", "").lower() in ("1", "true", "yes")

# STOCKS_DEBUG=1 turns on the [market_data] cache/retry trace.
DEBUG = os.environ.get("STOCKS_DEBUG", "").lower() in ("1", "true", "yes")

REQUEST_TIMEOUT = 20

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Set by cached_fetch() on every call; read it via was_cache_hit().
LAST_CACHE_HIT: Optional[bool] = None


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[market_data] {msg}")


def _warn(msg: str) -> None:
    print(f"[market_data] {msg}", file=sys.stderr)


# -------------------------------------------------------------------------
# SESSION
# -------------------------------------------------------------------------

_SESSION = None
_SESSION_LOCK = threading.Lock()


def get_session():
    """Return the process-wide HTTP session, building it on first use.

    curl_cffi (browser TLS fingerprint) when available, otherwise a plain
    requests.Session with a browser User-Agent. Connection pooling is the
    point: repeated calls reuse sockets instead of reopening TLS every time.
    Both types are accepted by yfinance.
    """
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = _build_session()
    return _SESSION


def _build_session():
    if curl_requests is not None and not FORCE_REQUESTS_SESSION:
        try:
            session = curl_requests.Session(impersonate=CURL_IMPERSONATE, timeout=REQUEST_TIMEOUT)
            _log(f"session: curl_cffi (impersonate={CURL_IMPERSONATE})")
            return session
        except Exception as e:
            _warn(f"curl_cffi session unavailable ({e}); falling back to requests")

    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    _log("session: requests.Session")
    return session


def reset_session():
    """Drop the pooled session (call after a long idle or a proxy change)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception:
                pass
        _SESSION = None


def get_ticker(symbol: str):
    """yfinance Ticker bound to the shared session. The one place yf.Ticker
    is constructed - other scripts go through the helpers below."""
    return yf.Ticker(symbol, session=get_session())


# -------------------------------------------------------------------------
# DISK CACHE
# -------------------------------------------------------------------------

def ensure_cache_dirs() -> None:
    """Create cache\\ and its per-type subfolders. Idempotent."""
    for name in CACHE_TYPES:
        (CACHE_DIR / name).mkdir(parents=True, exist_ok=True)


_SLUG_RE = re.compile(r"[^A-Za-z0-9._=-]+")


def _slug(text: str) -> str:
    """Make a cache key safe as a Windows filename ('BRK-B', 'SHOP.TO' survive)."""
    return _SLUG_RE.sub("_", str(text)).strip("_") or "unnamed"


def cache_path(cache_key: str, cache_type: Optional[str] = None) -> Path:
    """Resolve a cache key to a file path.

    The type comes from the explicit `cache_type`, else from a
    'prices/AAPL_1y'-style prefix on the key, else DEFAULT_CACHE_TYPE.
    """
    key = str(cache_key)
    if cache_type is None and "/" in key:
        head, _, tail = key.partition("/")
        if head in CACHE_TYPES:
            cache_type, key = head, tail
    if cache_type not in CACHE_TYPES:
        cache_type = DEFAULT_CACHE_TYPE
    key = key.replace("/", "_")
    directory = CACHE_DIR / cache_type
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(key)}.json"


def _json_default(obj):
    """Last-resort encoder: pandas/numpy scalars and timestamps -> JSON."""
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    item = getattr(obj, "item", None)          # numpy scalar
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    to_dict = getattr(obj, "to_dict", None)    # pandas Series/DataFrame
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    return str(obj)


def _read_cache_entry(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        return entry if isinstance(entry, dict) else None
    except Exception as e:
        _log(f"unreadable cache file {path.name}: {e}")
        return None


def _entry_age(entry: Optional[dict]) -> Optional[float]:
    """Age in seconds of a cache entry, or None if it has no usable timestamp."""
    if not entry:
        return None
    try:
        stamp = dt.datetime.fromisoformat(entry["timestamp"])
    except Exception:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return (dt.datetime.now() - stamp).total_seconds()


def _write_cache_entry(path: Path, data: Any) -> None:
    """Atomic write (tmp file + replace) so a killed run never leaves a
    half-written cache file behind."""
    entry = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "data": data}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2, ensure_ascii=False, default=_json_default)
        os.replace(tmp_path, path)
    except Exception as e:
        _warn(f"could not write cache {path.name}: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def cached_fetch(cache_key: str,
                 fetch_fn: Callable[[], Any],
                 ttl_seconds: float,
                 cache_type: Optional[str] = None,
                 force_refresh: bool = False) -> Any:
    """Return cached data if it is younger than ttl_seconds, else refetch.

    fetch_fn is a zero-argument callable (use a lambda or functools.partial).
    Wrap it in fetch_with_backoff() if it hits the network - the helpers in
    this module already do.

    Cache files are JSON: {"timestamp": <ISO datetime>, "data": <result>}.

    Returns None only when there is no fresh cache AND the fetch failed AND
    no stale entry exists to fall back on. Never raises for a fetch failure.
    """
    global LAST_CACHE_HIT

    path = cache_path(cache_key, cache_type)
    entry = _read_cache_entry(path)
    age = _entry_age(entry)

    if entry is not None and age is not None and age < ttl_seconds and not force_refresh:
        LAST_CACHE_HIT = True
        _log(f"HIT  {path.parent.name}/{path.name} (age {age:.0f}s < ttl {ttl_seconds:.0f}s)")
        return entry.get("data")

    LAST_CACHE_HIT = False
    reason = "forced" if force_refresh else ("stale" if entry is not None else "absent")
    _log(f"MISS {path.parent.name}/{path.name} ({reason}) - fetching")

    try:
        data = fetch_fn()
    except Exception as e:
        # fetch_with_backoff already swallows failures; this catches callers
        # that pass a raw fetch_fn, so cached_fetch keeps its no-raise promise.
        _warn(f"fetch failed for {cache_key}: {e}")
        data = None

    if data is None:
        if entry is not None:
            _warn(f"fetch failed for {cache_key}; serving stale cache (age {age if age is None else round(age)}s)")
            return entry.get("data")
        return None

    _write_cache_entry(path, data)
    return data


def was_cache_hit() -> Optional[bool]:
    """True if the most recent cached_fetch() was served from disk."""
    return LAST_CACHE_HIT


def clear_cache(cache_key: Optional[str] = None, cache_type: Optional[str] = None) -> int:
    """Delete one cache file, or a whole type's folder, or everything.
    Returns the number of files removed."""
    if cache_key is not None:
        path = cache_path(cache_key, cache_type)
        if path.exists():
            path.unlink()
            return 1
        return 0

    targets = [cache_type] if cache_type in CACHE_TYPES else list(CACHE_TYPES)
    removed = 0
    for name in targets:
        for path in (CACHE_DIR / name).glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# -------------------------------------------------------------------------
# BACKOFF
# -------------------------------------------------------------------------

_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limit")


def _status_code(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _looks_rate_limited(exc: Exception) -> bool:
    if _status_code(exc) == 429:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Honour a Retry-After header when the server sends one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def fetch_with_backoff(fetch_fn: Callable[..., Any],
                       *args,
                       max_retries: int = 4,
                       base_delay: float = 2,
                       **kwargs) -> Any:
    """Call fetch_fn(*args, **kwargs), retrying with exponential backoff.

    Up to max_retries attempts. Between attempts it sleeps
    base_delay * (2 ** attempt) seconds plus a little jitter (doubled for a
    429, or the server's Retry-After if longer), so parallel callers don't
    retry in lockstep.

    After the last failure it returns None rather than raising - callers
    must handle None. Only Exception is caught, so Ctrl-C still interrupts.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            return fetch_fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt >= max_retries - 1:
                break

            delay = base_delay * (2 ** attempt)
            if _looks_rate_limited(e):
                delay *= 2
            retry_after = _retry_after_seconds(e)
            if retry_after is not None:
                delay = max(delay, retry_after)
            delay += random.uniform(0, min(1.0, base_delay))

            _log(f"attempt {attempt + 1}/{max_retries} failed ({e}); retrying in {delay:.1f}s")
            time.sleep(delay)

    _warn(f"giving up after {max_retries} attempts: {last_error}")
    return None


# -------------------------------------------------------------------------
# DATA HELPERS  (the only code in the project that touches yfinance)
# -------------------------------------------------------------------------

def get_info(ticker: str, ttl: float = TTL_FINANCIALS, force_refresh: bool = False) -> Optional[dict]:
    """Yahoo's .info dict for a ticker, cached under cache\\financials\\."""
    def _fetch():
        info = get_ticker(ticker).info
        if not info:
            raise ValueError(f"empty info payload for {ticker}")
        return info

    return cached_fetch(f"{ticker}_info",
                        lambda: fetch_with_backoff(_fetch),
                        ttl,
                        cache_type="financials",
                        force_refresh=force_refresh)


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _history_to_rows(hist) -> list:
    """DataFrame -> JSON-safe list of row dicts, newest last."""
    rows = []
    for index, row in hist.iterrows():
        rows.append({
            "date": index.isoformat() if hasattr(index, "isoformat") else str(index),
            "open": _num(row.get("Open")),
            "high": _num(row.get("High")),
            "low": _num(row.get("Low")),
            "close": _num(row.get("Close")),
            "volume": _num(row.get("Volume")),
        })
    return rows


def get_price_history(ticker: str,
                      period: str = "1y",
                      interval: str = "1d",
                      ttl: float = TTL_PRICE,
                      force_refresh: bool = False) -> Optional[list]:
    """OHLCV rows for a ticker, cached under cache\\prices\\.

    Returns list[dict] with keys date/open/high/low/close/volume, or None.
    """
    def _fetch():
        hist = get_ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        if hist is None or hist.empty:
            raise ValueError(f"empty price history for {ticker} ({period}/{interval})")
        return _history_to_rows(hist)

    return cached_fetch(f"{ticker}_{period}_{interval}",
                        lambda: fetch_with_backoff(_fetch),
                        ttl,
                        cache_type="prices",
                        force_refresh=force_refresh)


def get_avg_volume(ticker: str,
                   info: Optional[dict] = None,
                   period: str = "3mo") -> Optional[float]:
    """Average daily volume. Prefers the (already cached) info fields, and
    only falls back to price history if none of them are present."""
    if info is None:
        info = get_info(ticker)

    if isinstance(info, dict):
        for key in ("averageDailyVolume3Month", "averageVolume",
                    "averageDailyVolume10Day", "averageVolume10days"):
            value = _num(info.get(key))
            if value and value > 0:
                return value

    rows = get_price_history(ticker, period=period) or []
    volumes = [row["volume"] for row in rows if _num(row.get("volume"))]
    return sum(volumes) / len(volumes) if volumes else None


def get_company_name(ticker: str, info: Optional[dict] = None) -> Optional[str]:
    """longName, falling back to shortName / displayName. None if unknown."""
    if info is None:
        info = get_info(ticker)
    if not isinstance(info, dict):
        return None
    for key in ("longName", "shortName", "displayName"):
        name = info.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def cached_screener(name: str,
                    fetch_fn: Callable[[], Any],
                    ttl: float = TTL_SCREENER,
                    force_refresh: bool = False) -> Any:
    """Entry point for screener scripts: caches under cache\\screener\\ and
    wraps the fetch in backoff, so a screen re-runs at most once a day."""
    return cached_fetch(name,
                        lambda: fetch_with_backoff(fetch_fn),
                        ttl,
                        cache_type="screener",
                        force_refresh=force_refresh)


# -------------------------------------------------------------------------
# TICKER IDENTITY
# -------------------------------------------------------------------------

# Legal-form suffixes only. Words like "holdings", "group" or "international"
# are deliberately NOT stripped - they distinguish real companies, and the
# duplicates we are after (dual-class, cross-listings) share a name anyway.
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "ltda", "limited", "plc", "llc", "lp", "llp", "sa", "sab", "se",
    "ag", "nv", "bv", "ab", "as", "asa", "oyj", "oy", "spa", "sas", "gmbh",
    "kk", "pte", "pty", "kgaa", "cv", "sarl",
}

# Share-class / listing noise that trails a name.
_SHARE_NOISE = {
    "class", "cl", "series", "ser", "common", "com", "ordinary", "ord",
    "shares", "share", "stock", "new", "registered", "reg", "adr", "ads",
    "sponsored", "unsponsored", "depositary", "receipt", "receipts",
    "units", "unit", "voting", "subordinate", "subordinated", "restricted",
}

_DROP_TOKENS = _LEGAL_SUFFIXES | _SHARE_NOISE

# "Class A", "cl. B", "Series C" anywhere in the name.
_CLASS_RE = re.compile(r"\b(?:class|cl|series|ser)\.?\s*[a-z0-9]{1,3}\b")
_PARENS_RE = re.compile(r"\([^)]*\)")


def normalize_company_name(name: Optional[str]) -> str:
    """Reduce a company name to a comparison key.

    'Alphabet Inc. Class A', 'Alphabet Inc. Class C' and 'ALPHABET INC'
    all collapse to 'alphabet'. Returns '' for an unusable name.
    """
    if not isinstance(name, str) or not name.strip():
        return ""

    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = _PARENS_RE.sub(" ", text)
    text = text.replace("&", " and ")
    text = _CLASS_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)

    tokens = text.split()
    while tokens and tokens[0] == "the":
        tokens.pop(0)

    # Trailing legal/class noise, and bare class letters left behind by it.
    trimmed = list(tokens)
    while trimmed and (trimmed[-1] in _DROP_TOKENS or len(trimmed[-1]) == 1):
        trimmed.pop()

    return " ".join(trimmed or tokens)


def dedupe_tickers(ticker_list: Iterable[str],
                   ttl: float = TTL_FINANCIALS) -> tuple:
    """Collapse tickers that are the same underlying company.

    Dual-class listings (GOOGL/GOOG, BRK-A/BRK-B) and cross-listings
    (SHOP/SHOP.TO) share a company name once normalized. Within a group the
    ticker with the highest average daily volume is kept - ties break toward
    the primary listing (no exchange suffix), then the shorter symbol, then
    input order.

    Tickers whose name cannot be fetched are always kept: a network failure
    must never silently shrink the universe.

    Returns (deduped_list, dropped_list). deduped_list is the surviving
    tickers in input order; dropped_list holds dicts of
    {ticker, kept, reason, name, normalized, avg_volume, kept_avg_volume}
    so the caller can log exactly what went and why.
    """
    ordered: list = []
    dropped: list = []
    seen: dict = {}

    for raw in ticker_list or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        symbol = raw.strip().upper()
        if symbol in seen:
            dropped.append({"ticker": raw.strip(), "kept": seen[symbol],
                            "reason": "duplicate_symbol", "name": None,
                            "normalized": None, "avg_volume": None,
                            "kept_avg_volume": None})
            continue
        seen[symbol] = symbol
        ordered.append(symbol)

    position = {symbol: i for i, symbol in enumerate(ordered)}
    groups: dict = {}
    names: dict = {}
    infos: dict = {}
    unresolved: list = []

    for symbol in ordered:
        info = get_info(symbol, ttl=ttl)
        infos[symbol] = info
        name = get_company_name(symbol, info=info)
        names[symbol] = name
        key = normalize_company_name(name)
        if not key:
            # No name (network failure, delisted, odd instrument) - keep it.
            _log(f"{symbol}: no usable name, keeping unconditionally")
            unresolved.append(symbol)
            continue
        groups.setdefault(key, []).append(symbol)

    keep = set(unresolved)

    for key, members in groups.items():
        if len(members) == 1:
            keep.add(members[0])
            continue

        volumes = {symbol: (get_avg_volume(symbol, info=infos.get(symbol)) or 0.0)
                   for symbol in members}

        def rank(symbol):
            return (volumes[symbol],
                    0 if "." not in symbol else -1,
                    -len(symbol),
                    -position[symbol])

        winner = max(members, key=rank)
        keep.add(winner)
        for symbol in members:
            if symbol == winner:
                continue
            dropped.append({
                "ticker": symbol,
                "kept": winner,
                "reason": "duplicate_company",
                "name": names.get(symbol),
                "normalized": key,
                "avg_volume": volumes.get(symbol),
                "kept_avg_volume": volumes.get(winner),
            })

    deduped = [symbol for symbol in ordered if symbol in keep]
    return deduped, dropped


# -------------------------------------------------------------------------
# SELF-TEST
# -------------------------------------------------------------------------

_NAME_CASES = [
    ("Alphabet Inc.",                  "alphabet"),
    ("Alphabet Inc. Class C",          "alphabet"),
    ("ALPHABET INC CL A",              "alphabet"),
    ("Berkshire Hathaway Inc. New",    "berkshire hathaway"),
    ("Shopify Inc.",                   "shopify"),
    ("The Coca-Cola Company",          "coca cola"),
    ("Coca-Cola Co",                   "coca cola"),
    ("Nestle S.A. (ADR)",              "nestle"),
    ("Constellation Software Inc.",    "constellation software"),
    ("Brookfield Corporation Class A", "brookfield"),
    ("",                               ""),
]


def _self_test_names() -> bool:
    print("\n-- name normalization --")
    ok = True
    for raw, expected in _NAME_CASES:
        got = normalize_company_name(raw)
        flag = "ok  " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  {flag} {raw!r:36} -> {got!r}")
    return ok


def _self_test_cache() -> bool:
    print("\n-- cached_fetch (AAPL info, twice) --")
    key = "AAPL_info"
    clear_cache(key, cache_type="financials")   # make the first call a real miss

    def fetch():
        info = get_ticker("AAPL").info
        if not info:
            raise ValueError("empty info payload for AAPL")
        return info

    started = time.perf_counter()
    first = cached_fetch(key, lambda: fetch_with_backoff(fetch), TTL_FINANCIALS,
                         cache_type="financials")
    first_elapsed = time.perf_counter() - started
    first_hit = was_cache_hit()
    print(f"  call 1: {first_elapsed:7.3f}s  cache_hit={first_hit}  "
          f"name={(first or {}).get('longName') if isinstance(first, dict) else None}")

    if first is None:
        print("  network unavailable after all retries - cached_fetch returned None "
              "(no crash, which is the contract). Skipping the cache-hit check.")
        return True

    started = time.perf_counter()
    second = cached_fetch(key, lambda: fetch_with_backoff(fetch), TTL_FINANCIALS,
                          cache_type="financials")
    second_elapsed = time.perf_counter() - started
    second_hit = was_cache_hit()
    print(f"  call 2: {second_elapsed:7.3f}s  cache_hit={second_hit}")

    faster = second_elapsed < max(first_elapsed / 2, 0.001)
    print(f"  cache file: {cache_path(key, 'financials')}")
    print(f"  verdict: second call was a {'CACHE HIT' if second_hit else 'MISS'} "
          f"({'faster' if faster else 'not faster'}, {first_elapsed:.3f}s -> {second_elapsed:.3f}s)")

    ok = (first_hit is False) and (second_hit is True) and (second == first)
    if not ok:
        print("  FAIL: expected miss-then-hit with identical data")
    return ok


def _self_test_dedupe() -> None:
    universe = ["GOOGL", "GOOG", "SHOP", "SHOP.TO", "BRK-A", "BRK-B", "AAPL", "aapl"]
    print(f"\n-- dedupe_tickers({universe}) --")
    deduped, dropped = dedupe_tickers(universe)
    print(f"  kept:    {deduped}")
    for row in dropped:
        volume = row["avg_volume"]
        kept_volume = row["kept_avg_volume"]
        print(f"  dropped: {row['ticker']:8} -> kept {row['kept']:8} "
              f"({row['reason']}"
              + (f", vol {volume:,.0f} vs {kept_volume:,.0f}" if volume and kept_volume else "")
              + ")")


def main() -> int:
    parser = argparse.ArgumentParser(description="market_data self-test")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--dedupe", action="store_true",
                        help="also run the dedupe demo (several more fetches)")
    args = parser.parse_args()

    global DEBUG
    DEBUG = DEBUG or args.verbose

    ensure_cache_dirs()
    session = get_session()
    print(f"cache dir: {CACHE_DIR}")
    print(f"subfolders: {', '.join(CACHE_TYPES)}")
    print(f"session:   {type(session).__module__}.{type(session).__name__}")
    print(f"TTLs:      financials={TTL_FINANCIALS}s  price={TTL_PRICE}s  screener={TTL_SCREENER}s")

    names_ok = _self_test_names()
    cache_ok = _self_test_cache()

    if args.dedupe:
        _self_test_dedupe()

    print(f"\nself-test: {'PASS' if names_ok and cache_ok else 'FAIL'}")
    return 0 if names_ok and cache_ok else 1


if __name__ == "__main__":
    sys.exit(main())

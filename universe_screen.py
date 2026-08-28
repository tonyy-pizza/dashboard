#!/usr/bin/env python3
r"""
universe_screen.py - Stage 0 of the scan pipeline: build the candidate universe.

A deliberately WIDE net. The job here is to get from "every listed equity" to
"a few thousand plausible names" with a handful of loose, cheap filters.
Fine-grained, sector-aware judgement belongs downstream in stock_evaluator.py,
not here - if a filter in this file feels clever, it is in the wrong file.

All network access goes through market_data.py (session, disk cache, backoff,
ticker identity). yfinance is imported only for EquityQuery, which builds the
query object offline; the actual screen call lives in market_data.screen_page.

How the pull is shaped:

  - Yahoo's screener payload has no sector field, so the screen is run once
    per sector and the sector tag comes from the query itself. That also
    partitions the universe into chunks well under Yahoo's paging limits.
  - Each partition is paged 250 rows at a time until Yahoo runs out.
  - Nothing in this script sorts or compares price or market cap across
    tickers: with Canada included those numbers are in mixed currencies.
    Rows are tagged with their currency and passed through; normalization is
    a downstream problem. Sorting is alphabetical by ticker throughout.

Output: <stocks>\data\candidates.json (see write_candidates for the schema).
An empty result set is written like any other - no matches is a legitimate
market state. Every partition failing is not, and leaves the previous file
alone (see total_failure).

Setup:
    pip install yfinance curl_cffi requests

Usage:
    py universe_screen.py                     # US, default thresholds
    py universe_screen.py --include-canada
    py universe_screen.py --min-volume 500000 --min-market-cap 1e9
    py universe_screen.py --sector Technology --sector Energy -v
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import market_data as md
from yfinance import EquityQuery   # query construction only - no network here


# -------------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------------

# Sits next to market_data.py, so C:\Users\joey\stocks\data\candidates.json.
DATA_DIR = Path(os.environ.get("STOCKS_DATA_DIR") or (md.BASE_DIR / "data"))
OUTPUT_PATH = DATA_DIR / "candidates.json"

# Loose by design. Every one of these is a "obviously not worth analysing"
# cutoff, not an investment opinion.
DEFAULT_MAX_PE         = 40.0          # loose ceiling; no floor by default
DEFAULT_MIN_AVG_VOLUME = 200_000       # shares/day - enough to actually trade
DEFAULT_MIN_MARKET_CAP = 300_000_000   # drops nano/micro-cap shells

PAGE_SIZE = md.SCREEN_PAGE_MAX         # 250, Yahoo's per-call cap
DEFAULT_MAX_PAGES = 8                  # 2000 rows per partition, then stop
REQUEST_PAUSE = 0.25                   # polite gap between real (uncached) pages

# Yahoo region codes. US only unless --include-canada.
REGION_US = "us"
REGION_CA = "ca"

# Fallback if yfinance stops exposing its sector vocabulary.
FALLBACK_SECTORS = [
    "Basic Materials", "Communication Services", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Financial Services", "Healthcare",
    "Industrials", "Real Estate", "Technology", "Utilities",
]

# Suffix -> currency, used only when Yahoo omits the currency field.
_SUFFIX_CURRENCY = {".TO": "CAD", ".V": "CAD", ".CN": "CAD", ".NE": "CAD"}


def yahoo_sectors() -> list:
    """The sector values Yahoo's screener accepts, straight from yfinance."""
    try:
        values = EquityQuery("eq", ["region", REGION_US]).valid_values["sector"]
        if isinstance(values, dict):
            values = set().union(*values.values())
        sectors = sorted(str(v) for v in values)
        return sectors or list(FALLBACK_SECTORS)
    except Exception as e:
        md._log(f"could not read sector vocabulary ({e}); using fallback list")
        return list(FALLBACK_SECTORS)


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# -------------------------------------------------------------------------
# QUERY
# -------------------------------------------------------------------------

def build_query(region: str,
                sector: Optional[str] = None,
                max_pe: Optional[float] = DEFAULT_MAX_PE,
                min_pe: Optional[float] = None,
                min_avg_volume: Optional[float] = DEFAULT_MIN_AVG_VOLUME,
                min_market_cap: Optional[float] = DEFAULT_MIN_MARKET_CAP) -> EquityQuery:
    """The loose Stage 0 net for one region (and optionally one sector).

    Built offline - no request is made until the query reaches
    market_data.screen_page().
    """
    operands = [EquityQuery("eq", ["region", region])]

    if max_pe is not None:
        operands.append(EquityQuery("lt", ["peratio.lasttwelvemonths", max_pe]))
    if min_pe is not None:
        operands.append(EquityQuery("gt", ["peratio.lasttwelvemonths", min_pe]))
    if min_avg_volume:
        operands.append(EquityQuery("gt", ["avgdailyvol3m", min_avg_volume]))
    if min_market_cap:
        operands.append(EquityQuery("gt", ["intradaymarketcap", min_market_cap]))
    if sector:
        operands.append(EquityQuery("eq", ["sector", sector]))

    if len(operands) == 1:
        return operands[0]
    return EquityQuery("and", operands)


def _params_digest(params: dict) -> str:
    """Short hash of the thresholds, so changing one doesn't reuse yesterday's
    cached pages for a different screen."""
    shape = json.dumps({k: params[k] for k in sorted(params)}, sort_keys=True, default=str)
    return hashlib.md5(shape.encode("utf-8")).hexdigest()[:8]


# -------------------------------------------------------------------------
# SCREEN
# -------------------------------------------------------------------------

def screen_partition(region: str,
                     sector: Optional[str],
                     params: dict,
                     max_pages: int = DEFAULT_MAX_PAGES,
                     force_refresh: bool = False) -> dict:
    """Page through one region/sector partition.

    Returns {"quotes", "total", "pages", "truncated", "failed"}. A partition
    that fails outright comes back with failed=True and whatever rows it got -
    a dead partition is logged, never raised.
    """
    query = build_query(region, sector,
                        max_pe=params["max_pe"],
                        min_pe=params["min_pe"],
                        min_avg_volume=params["min_avg_volume"],
                        min_market_cap=params["min_market_cap"])
    label = f"{region}/{sector or 'all sectors'}"
    cache_key = f"{region}_{md._slug(sector or 'all')}_{params['digest']}"

    quotes, seen = [], set()
    total = None
    pages = 0
    truncated = False
    failed = False

    for page in range(max_pages):
        offset = page * PAGE_SIZE
        payload = md.screen_page(query,
                                 offset=offset,
                                 size=PAGE_SIZE,
                                 sort_field="ticker",   # never sort on money
                                 sort_asc=True,
                                 ttl=md.TTL_SCREENER,
                                 cache_key=cache_key,
                                 force_refresh=force_refresh)
        was_cached = md.was_cache_hit()
        pages += 1

        if payload is None:
            print(f"  [{label}] page {page + 1} failed after retries - partition incomplete")
            failed = True
            break

        page_quotes = payload.get("quotes") or []
        total = payload.get("total", total)

        for quote in page_quotes:
            symbol = str(quote.get("symbol") or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                quotes.append(quote)

        if len(page_quotes) < PAGE_SIZE:
            break
        if isinstance(total, int) and offset + len(page_quotes) >= total:
            break
        if page == max_pages - 1:
            truncated = True
            print(f"  [{label}] hit the {max_pages}-page cap "
                  f"({len(quotes)} of {total} rows) - raise --max-pages for the rest")
        elif not was_cached:
            time.sleep(REQUEST_PAUSE)

    return {"quotes": quotes, "total": total, "pages": pages,
            "truncated": truncated, "failed": failed}


def run_screen(params: dict,
               regions: list,
               sectors: list,
               max_pages: int = DEFAULT_MAX_PAGES,
               force_refresh: bool = False) -> tuple:
    """Run every region x sector partition. Returns (rows_by_symbol, stats).

    rows_by_symbol maps SYMBOL -> (quote, sector). First partition to claim a
    symbol keeps it, so a name is never counted twice.
    """
    rows_by_symbol: dict = {}
    stats: list = []

    for region in regions:
        for sector in sectors:
            result = screen_partition(region, sector, params,
                                      max_pages=max_pages,
                                      force_refresh=force_refresh)
            added = 0
            for quote in result["quotes"]:
                symbol = str(quote.get("symbol") or "").strip().upper()
                if not symbol or symbol in rows_by_symbol:
                    continue
                rows_by_symbol[symbol] = (quote, sector)
                added += 1

            stats.append({
                "region": region,
                "sector": sector,
                "returned": len(result["quotes"]),
                "new": added,
                "total_available": result["total"],
                "pages": result["pages"],
                "truncated": result["truncated"],
                "failed": result["failed"],
            })
            available = stats[-1]["total_available"]
            of_total = "" if available is None else f" of {available}"
            print(f"  {region}/{sector or 'all sectors':<24} "
                  f"{len(result['quotes']):>5} rows{of_total}  (+{added} new)")

    return rows_by_symbol, stats


# -------------------------------------------------------------------------
# CANDIDATES
# -------------------------------------------------------------------------

def currency_of(symbol: str, quote: dict) -> Optional[str]:
    """USD/CAD tag straight off the quote, with a suffix fallback. Tag only -
    nothing in this script compares values across currencies."""
    currency = quote.get("currency") or quote.get("financialCurrency")
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()
    for suffix, code in _SUFFIX_CURRENCY.items():
        if symbol.upper().endswith(suffix):
            return code
    return None


def avg_volume_of(quote: dict) -> Optional[float]:
    for key in ("averageDailyVolume3Month", "averageDailyVolume10Day", "regularMarketVolume"):
        volume = _num(quote.get(key))
        if volume:
            return volume
    return None


def to_candidate(symbol: str, quote: dict, sector: Optional[str]) -> dict:
    return {
        "ticker": symbol,
        "sector": sector or quote.get("sector") or None,
        "market_cap": _num(quote.get("marketCap")),
        "currency": currency_of(symbol, quote),
        "avg_volume": avg_volume_of(quote),
    }


def total_failure(stats: list, candidates: list) -> bool:
    """True when nothing came back and every partition errored out - i.e. the
    run failed, as opposed to the market simply having no matches."""
    return bool(stats) and not candidates and all(s["failed"] for s in stats)


def previous_run_stamp(output_path: Path) -> Optional[str]:
    """generated_at of the file already on disk, for the abort message."""
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            return json.load(f).get("generated_at")
    except Exception:
        return None


def write_candidates(candidates: list,
                     query_params: dict,
                     dropped: list,
                     output_path: Path = OUTPUT_PATH) -> Path:
    """Write the Stage 0 output file:

        {"generated_at": <ISO>, "query_params": {...},
         "dropped_duplicates": [...], "candidates": [{...}]}

    Written atomically (tmp + replace) so a downstream reader never sees a
    half-written file. An empty candidates list is written like any other.
    """
    document = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "query_params": query_params,
        "dropped_duplicates": dropped,
        "candidates": candidates,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


# -------------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Stage 0: pull a loose candidate universe into data/candidates.json")
    parser.add_argument("--include-canada", action="store_true",
                        help="also screen Canadian listings (tagged CAD)")
    parser.add_argument("--max-pe", type=float, default=DEFAULT_MAX_PE,
                        help=f"trailing P/E ceiling (default {DEFAULT_MAX_PE:g}, 0 to disable)")
    parser.add_argument("--min-pe", type=float, default=None,
                        help="optional trailing P/E floor (default: none, loss-makers allowed)")
    parser.add_argument("--min-volume", type=float, default=DEFAULT_MIN_AVG_VOLUME,
                        help=f"minimum 3-month average daily volume (default {DEFAULT_MIN_AVG_VOLUME:,.0f})")
    parser.add_argument("--min-market-cap", type=float, default=DEFAULT_MIN_MARKET_CAP,
                        help=f"minimum market cap in listing currency (default {DEFAULT_MIN_MARKET_CAP:,.0f})")
    parser.add_argument("--sector", action="append", dest="sectors", metavar="NAME",
                        help="restrict to one sector (repeatable); default is all of them")
    parser.add_argument("--no-sector-split", action="store_true",
                        help="one query per region instead of one per sector "
                             "(faster, but candidates come back with sector=null)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"page cap per partition (default {DEFAULT_MAX_PAGES}, {PAGE_SIZE} rows each)")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="skip dual-class/cross-listing dedupe")
    parser.add_argument("--force-write", action="store_true",
                        help="write the output file even if every partition failed "
                             "(default: keep the previous file and exit 1)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached screen pages and refetch")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH,
                        help=f"output path (default {OUTPUT_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="market_data cache/retry trace")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.verbose:
        md.DEBUG = True

    regions = [REGION_US] + ([REGION_CA] if args.include_canada else [])

    if args.no_sector_split:
        sectors = [None]
    elif args.sectors:
        known = {s.lower(): s for s in yahoo_sectors()}
        sectors = []
        for requested in args.sectors:
            match = known.get(requested.strip().lower())
            if match is None:
                print(f"unknown sector {requested!r}; valid: {', '.join(sorted(known.values()))}",
                      file=sys.stderr)
                return 2
            sectors.append(match)
    else:
        sectors = yahoo_sectors()

    params = {
        "max_pe": args.max_pe if args.max_pe else None,
        "min_pe": args.min_pe,
        "min_avg_volume": args.min_volume if args.min_volume else None,
        "min_market_cap": args.min_market_cap if args.min_market_cap else None,
    }
    params["digest"] = _params_digest(params)

    def _threshold(label, value, fmt=",.0f"):
        return "" if value is None else f", {label} {value:{fmt}}"

    print("== universe screen (stage 0) ==")
    print(f"regions:  {', '.join(regions)}")
    print("filters: " + ("  (none)" if not any(params[k] is not None for k in
          ("max_pe", "min_pe", "min_avg_volume", "min_market_cap")) else
          (_threshold("P/E <", params["max_pe"], "g")
           + _threshold("P/E >", params["min_pe"], "g")
           + _threshold("avg vol >", params["min_avg_volume"])
           + _threshold("mkt cap >", params["min_market_cap"])
           + " (listing currency)").lstrip(", ")))
    print(f"partitions: {len(regions)} region(s) x {len(sectors)} sector(s)")

    rows_by_symbol, stats = run_screen(params, regions, sectors,
                                       max_pages=args.max_pages,
                                       force_refresh=args.refresh)

    symbols = sorted(rows_by_symbol)   # alphabetical: never ordered by money
    dropped: list = []

    if args.no_dedupe:
        kept = symbols
    else:
        # Hand dedupe the names and volumes the screener already returned, so
        # collapsing a few thousand tickers costs no extra requests.
        names = {}
        volumes = {}
        for symbol, (quote, _sector) in rows_by_symbol.items():
            name = quote.get("longName") or quote.get("shortName") or quote.get("displayName")
            if isinstance(name, str) and name.strip():
                names[symbol] = name.strip()
            volume = avg_volume_of(quote)
            if volume is not None:
                volumes[symbol] = volume
        kept, dropped = md.dedupe_tickers(symbols, names=names, volumes=volumes)

    candidates = [to_candidate(symbol, *rows_by_symbol[symbol]) for symbol in kept]

    if dropped:
        print(f"\ndedupe dropped {len(dropped)}:")
        for row in dropped:
            volume, kept_volume = row.get("avg_volume"), row.get("kept_avg_volume")
            detail = ""
            if volume and kept_volume:
                detail = f", vol {volume:,.0f} vs {kept_volume:,.0f}"
            print(f"  {row['ticker']:<10} -> kept {row['kept']:<10} "
                  f"({row['reason']}{detail}) {row.get('name') or ''}")

    query_params = {
        "max_pe": params["max_pe"],
        "min_pe": params["min_pe"],
        "min_avg_volume": params["min_avg_volume"],
        "min_market_cap": params["min_market_cap"],
        "market_cap_note": "threshold applies in each listing's own currency; not normalized",
        "regions": regions,
        "include_canada": bool(args.include_canada),
        "sectors": sectors if sectors != [None] else None,
        "sector_split": not args.no_sector_split,
        "page_size": PAGE_SIZE,
        "max_pages_per_partition": args.max_pages,
        "sort_field": "ticker",
        "screen_ttl_seconds": md.TTL_SCREENER,
        "deduped": not args.no_dedupe,
        "partitions": stats,
    }

    incomplete = [s for s in stats if s["failed"] or s["truncated"]]
    print(f"\nraw tickers:  {len(symbols)}")
    print(f"candidates:   {len(candidates)}")
    print(f"dropped dups: {len(dropped)}")
    if incomplete:
        print(f"incomplete:   {len(incomplete)} partition(s) truncated or failed "
              f"(see query_params.partitions)")

    # A market with nothing in it is a real state and gets written like any
    # other. Every partition failing is not: that is the network, not the
    # market, and overwriting a good universe with an empty one would break
    # the pipeline for a reason that has nothing to do with stocks.
    if total_failure(stats, candidates) and args.output.exists() and not args.force_write:
        previous = previous_run_stamp(args.output)
        print(f"\nABORT: all {len(stats)} partition(s) failed - this is a fetch failure, "
              f"not an empty market.")
        print(f"       kept the existing {args.output}"
              + (f" (generated {previous})" if previous else "")
              + " instead of emptying it.")
        print("       rerun when the network is back, or pass --force-write to "
              "overwrite it with an empty universe.")
        return 1

    output_path = write_candidates(candidates, query_params, dropped, args.output)

    if not candidates:
        # Legitimate market state, not an error: the file is still written.
        print("note: zero candidates matched - wrote an empty candidate list")
    print(f"wrote:        {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

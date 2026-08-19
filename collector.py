#!/usr/bin/env python3
"""
Dashboard Collector — joputer edition

Headless data gatherer. Runs standalone (no persistent process), triggered:
  - on demand by the widget's ↻ button (spawned as a subprocess)
  - on the widget's background refresh timer (calendar/market freshness)

Everything it gathers goes into ONE cache file, written atomically (tmp file +
rename) so the widget's QFileSystemWatcher never sees a half-written file.

It owns the *read-only* panels only — Weather, Greed Index, Sector Analysis
and Calendar. To Do, Habit Tracker, Journal and Rough Notes are written by the
widget straight to their own files and never appear in cache.json.

No Ollama. Local inference used to lock the machine up for minutes on every
refresh; the panels that depended on it (Daily Briefing, AI-inferred To Do,
Project Status) are gone, along with the News RSS panel. Nothing here talks to
localhost:11434 any more.

Setup:
    pip install yfinance requests
    pip install icalendar recurring-ical-events                 # calendar

Usage:
    py collector.py            # gather everything, write cache.json
"""

import datetime as dt
import sys
import time

import requests
import yfinance as yf

import calendar_feed
from paths import CACHE_PATH, prepare as prepare_directories
from storage import load_json, save_json

# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# Vancouver, BC — update if needed
WEATHER_LAT = 49.2827
WEATHER_LON = -123.1207

# CNN Fear & Greed index (unofficial JSON endpoint used by cnn.com itself)
GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

SP500_SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLI": "Industrials", "XLV": "Health Care", "XLY": "Cons. Discretionary",
    "XLP": "Cons. Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication",
}
TSX_SECTOR_ETFS = {
    "XIT.TO": "Technology", "XFN.TO": "Financials", "XEG.TO": "Energy",
    "XMA.TO": "Materials", "XST.TO": "Cons. Staples", "XUT.TO": "Utilities",
    "XRE.TO": "Real Estate",
}

# Yahoo throttles, and 18 tickers with no bound can outlast the 300s the
# widget gives the whole run. Past this, keep the previous run's numbers.
SECTOR_DEADLINE_SECONDS = 120


# ─────────────────────────────────────────────────────────────────────────
# WEATHER (Open-Meteo — no API key required)
# ─────────────────────────────────────────────────────────────────────────

def collect_weather():
    last_error = None
    # 3 attempts with backoff. Connection resets (WinError 10054) usually mean
    # something between us and open-meteo (AV, firewall, flaky wifi) killed the
    # socket — a fresh attempt a few seconds later almost always goes through.
    # A browser-style User-Agent also helps: some middleboxes reset the default
    # "python-requests/x" client on sight.
    for attempt in range(3):
        try:
            if attempt:
                time.sleep(2 * attempt)
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": WEATHER_LAT,
                    "longitude": WEATHER_LON,
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "hourly": "temperature_2m",
                    "forecast_days": 1,
                    "temperature_unit": "celsius",
                    "timezone": "America/Vancouver",
                },
                headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            cur = data.get("current", {})
            daily = data.get("daily", {})
            hourly = data.get("hourly", {})
            print("[weather] OK")
            return {
                "temp_c": cur.get("temperature_2m"),
                "wind_kph": cur.get("wind_speed_10m"),
                "weather_code": cur.get("weather_code"),
                "high_c": (daily.get("temperature_2m_max") or [None])[0],
                "low_c": (daily.get("temperature_2m_min") or [None])[0],
                # Today's hourly curve, drawn as the sparkline under the
                # current conditions in the widget's Weather panel.
                "hourly_temps": (hourly.get("temperature_2m") or [])[:24],
            }
        except Exception as e:
            last_error = e
            print(f"[weather] Attempt {attempt + 1} failed: {e}")
    return {"error": str(last_error)}


# ─────────────────────────────────────────────────────────────────────────
# GREED INDEX (CNN Fear & Greed)
# ─────────────────────────────────────────────────────────────────────────

def collect_greed():
    last_error = None
    # Same retry/backoff/User-Agent story as collect_weather() — CNN's edge
    # also rejects the default python-requests client outright.
    for attempt in range(3):
        try:
            if attempt:
                time.sleep(2 * attempt)
            resp = requests.get(
                GREED_URL,
                headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"},
                timeout=30,
            )
            resp.raise_for_status()
            fg = resp.json().get("fear_and_greed", {})
            score = fg.get("score")
            if score is None:
                raise ValueError("no 'score' in fear_and_greed response")
            prev = fg.get("previous_close")
            print("[greed] OK")
            return {
                "score": round(float(score)),
                "rating": fg.get("rating", ""),
                "previous_close": round(float(prev)) if prev is not None else None,
            }
        except Exception as e:
            last_error = e
            print(f"[greed] Attempt {attempt + 1} failed: {e}")
    return {"error": str(last_error)}


# ─────────────────────────────────────────────────────────────────────────
# SECTOR ANALYSIS
# ─────────────────────────────────────────────────────────────────────────

def collect_sectors(previous=None, deadline=None):
    """One row per sector ETF.

    `previous` is the last run's sector block: when the deadline passes, its
    numbers are carried over rather than blanked, since a day-change figure
    from an hour ago beats an empty row.
    """
    carried_over = {}
    for key in ("sp500", "tsx"):
        for row in ((previous or {}).get(key) or []):
            if row.get("ticker"):
                carried_over[row["ticker"]] = row

    out = {"sp500": [], "tsx": []}
    stale = 0
    for key, table in (("sp500", SP500_SECTOR_ETFS), ("tsx", TSX_SECTOR_ETFS)):
        for ticker, label in table.items():
            if deadline is not None and time.monotonic() > deadline:
                out[key].append(carried_over.get(ticker) or
                                {"ticker": ticker, "label": label, "change_pct": None})
                stale += 1
                continue
            out[key].append(_sector_row(ticker, label))

    # Auto-sort by day change, strongest movers first (nulls sort last)
    for key in ("sp500", "tsx"):
        out[key].sort(key=lambda r: (r["change_pct"] is None, -(r["change_pct"] or 0)))

    print(f"[sectors] OK{f' — {stale} carried over, out of time' if stale else ''}")
    return out


def _sector_row(ticker, label):
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist is None or hist.empty:
            return {"ticker": ticker, "label": label, "change_pct": None}
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return {"ticker": ticker, "label": label, "change_pct": None}
        change = (closes.iloc[-1] / closes.iloc[-2] - 1) * 100
        return {"ticker": ticker, "label": label, "change_pct": round(float(change), 2)}
    except Exception:
        return {"ticker": ticker, "label": label, "change_pct": None}


# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────

def run():
    started = time.monotonic()
    print(f"[{dt.datetime.now()}] Collector starting...")
    for name, destination in prepare_directories():
        print(f"[paths] moved {name} → {destination}")

    # Start from what's already cached, so anything this run fails to refresh
    # keeps its last known value instead of disappearing.
    previous = load_json(CACHE_PATH, {})
    cache = dict(previous) if isinstance(previous, dict) else {}

    # The quick sources first, then write immediately. The market fetch below
    # can take minutes when Yahoo throttles, and the widget kills the whole
    # run at 300 seconds — with a single write at the end, one slow source
    # meant the calendar never reached the dashboard at all.
    cache.update({
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "weather": collect_weather(),
        "greed": collect_greed(),
        "calendar": calendar_feed.collect_calendar(),
    })
    # Atomic write (tmp file + rename), so the widget's file watcher never
    # sees a partially-written cache.
    save_json(CACHE_PATH, cache)
    print(f"[cache] weather, greed and calendar written "
          f"({time.monotonic() - started:.0f}s)")

    cache["sectors"] = collect_sectors(
        previous=cache.get("sectors"),
        deadline=time.monotonic() + SECTOR_DEADLINE_SECONDS)
    cache["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_json(CACHE_PATH, cache)

    print(f"[{dt.datetime.now()}] Cache written to {CACHE_PATH} "
          f"({time.monotonic() - started:.0f}s total)")


def main():
    if "--auth" in sys.argv[1:]:
        # The calendar used to need an OAuth consent run. It doesn't any more,
        # and this note beats a silent no-op for anyone with the old shortcut.
        print("The calendar no longer uses OAuth — it reads a secret iCal URL.")
        for i, step in enumerate(calendar_feed.SETUP_STEPS, 1):
            print(f"  {i}. {step}")
        return
    run()


if __name__ == "__main__":
    main()

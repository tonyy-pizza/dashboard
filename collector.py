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
    pip install google-api-python-client google-auth-oauthlib   # calendar

Usage:
    py collector.py            # gather everything, write cache.json
    py collector.py --auth     # one-time Google Calendar OAuth consent
"""

import datetime as dt
import json
import os
import sys
import tempfile
import time

import requests
import yfinance as yf

import gcal
from paths import CACHE_PATH

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

def collect_sectors():
    out = {"sp500": [], "tsx": []}
    for ticker, label in SP500_SECTOR_ETFS.items():
        out["sp500"].append(_sector_row(ticker, label))
    for ticker, label in TSX_SECTOR_ETFS.items():
        out["tsx"].append(_sector_row(ticker, label))

    # Auto-sort by day change, strongest movers first (nulls sort last)
    for key in ("sp500", "tsx"):
        out[key].sort(key=lambda r: (r["change_pct"] is None, -(r["change_pct"] or 0)))

    print("[sectors] OK")
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
    print(f"[{dt.datetime.now()}] Collector starting...")

    cache = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "weather": collect_weather(),
        "greed": collect_greed(),
        "sectors": collect_sectors(),
        "calendar": gcal.collect_calendar(),
    }

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: tmp file + rename, so the widget's file watcher never
    # sees a partially-written cache.
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CACHE_PATH)

    print(f"[{dt.datetime.now()}] Cache written to {CACHE_PATH}")


def main():
    if "--auth" in sys.argv[1:]:
        # Interactive: opens a browser for Google's consent screen, then
        # caches the token so every later headless run can refresh silently.
        try:
            print(gcal.authorize())
        except Exception as e:
            print(f"[calendar] Authorization failed: {e}")
            print("Setup steps:")
            for i, step in enumerate(gcal.SETUP_STEPS, 1):
                print(f"  {i}. {step}")
            sys.exit(1)
        return
    run()


if __name__ == "__main__":
    main()

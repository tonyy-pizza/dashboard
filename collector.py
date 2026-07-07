#!/usr/bin/env python3
"""
Dashboard Collector — joputer edition
Replaces jobox's dashboard.service + notetaker.service.

Runs standalone (no persistent process). Meant to be triggered:
  - On-demand via the widget's ↻ button (spawned as a subprocess)

Gathers everything into ONE cache file, written atomically (tmp file +
rename) so the widget's QFileSystemWatcher never sees a half-written file.

Setup:
    pip install yfinance requests feedparser

Usage:
    py collector.py
"""

import calendar
import json
import os
import sys
import tempfile
import time
import datetime as dt
from pathlib import Path

import requests
import yfinance as yf

try:
    import feedparser
except ImportError:
    feedparser = None


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

DOOMNOTES_DIR = Path(r"C:\Users\joey\doomnotes")
CACHE_PATH    = Path(r"C:\Users\joey\dashboard-project-files\cache.json")

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3:8b"

# Vancouver, BC — update if needed
WEATHER_LAT = 49.2827
WEATHER_LON = -123.1207

# CNN Fear & Greed index (unofficial JSON endpoint used by cnn.com itself)
GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

NEWS_FEEDS = [
    ("MarketWatch",  "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance","https://finance.yahoo.com/news/rssindex"),
    ("FT Markets",   "https://www.ft.com/markets?format=rss"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
]
NEWS_MAX_PER_FEED = 6

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

NOTETAKER_SKIP_HEADER = "#+NOTETAKER: skip"


# ─────────────────────────────────────────────────────────────────────────
# OLLAMA
# ─────────────────────────────────────────────────────────────────────────

def check_ollama_connection() -> bool:
    """Ping Ollama before doing anything else, and print a clear status line.
    This is what you should watch when debugging from a terminal."""
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=10)
        resp.raise_for_status()
        models = [m.get("name", "?") for m in resp.json().get("models", [])]
        print(f"[ollama] Connected. Installed models: {models}")
        if not any(OLLAMA_MODEL in m for m in models):
            print(f"[ollama] WARNING: '{OLLAMA_MODEL}' not found in installed models. "
                  f"Run: ollama pull {OLLAMA_MODEL}")
            return False
        return True
    except requests.exceptions.ConnectionError:
        print(f"[ollama] FAILED: could not connect to http://localhost:11434 — "
              f"is 'ollama serve' running? Try: ollama list")
        return False
    except Exception as e:
        print(f"[ollama] FAILED: {e}")
        return False


def call_ollama(prompt: str, timeout: int = 90) -> str:
    """Single-shot Ollama call. keep_alive=0 forces immediate VRAM unload
    so it doesn't fight with gaming/AI workloads between runs."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "keep_alive": "0",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"[ollama] Call failed: {e}")
        return f"[Ollama unavailable: {e}]"


# ─────────────────────────────────────────────────────────────────────────
# ORG FILES — TODOS + PROJECT STATUS + BRIEFING
# ─────────────────────────────────────────────────────────────────────────

def find_org_files():
    if not DOOMNOTES_DIR.exists():
        return []
    files = []
    for p in DOOMNOTES_DIR.rglob("*.org"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # cheaper check: only scan first few lines for the skip header
        head = "\n".join(text.splitlines()[:5])
        if NOTETAKER_SKIP_HEADER in head:
            continue
        files.append((p, text))
    return files


def extract_todos(filename: str, content: str) -> list:
    if not content.strip():
        return []
    prompt = (
        f"Extract concrete action items / TODOs from this org-mode note "
        f"(file: {filename}). Return ONLY a plain bullet list, one per line, "
        f"starting each line with '- '. If there are none, return exactly "
        f"'none'.\n\n{content[:6000]}"
    )
    result = call_ollama(prompt)
    if result.lower().strip() in ("none", "- none", ""):
        return []
    items = []
    for line in result.splitlines():
        line = line.strip().lstrip("-").strip()
        if line:
            items.append(line)
    return items


def summarize_project_status(filename: str, content: str) -> str:
    if not content.strip():
        return "No content."
    prompt = (
        f"In 1-2 short sentences, summarize the current status of this "
        f"project note (file: {filename}). Be concrete, no fluff.\n\n"
        f"{content[:6000]}"
    )
    return call_ollama(prompt)


def generate_daily_briefing(project_summaries: list) -> str:
    if not project_summaries:
        return "No active project notes found."
    joined = "\n".join(f"- {p['file']}: {p['status']}" for p in project_summaries)
    prompt = (
        "Write a short (3-5 sentence), plain-spoken morning briefing summarizing "
        "the state of these ongoing projects. No headers, no bullet points, "
        f"just a natural narrative paragraph.\n\n{joined}"
    )
    return call_ollama(prompt)


def collect_notes():
    files = find_org_files()
    todos, projects = [], []

    for path, content in files:
        rel_name = str(path.relative_to(DOOMNOTES_DIR))
        for item in extract_todos(rel_name, content):
            todos.append({"text": item, "source_file": rel_name})
        status = summarize_project_status(rel_name, content)
        # File mtime feeds the "LAST COMPLETED" timestamp column in the
        # widget's Project Status panel.
        try:
            updated = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")
        except Exception:
            updated = None
        projects.append({"file": rel_name, "status": status, "updated": updated})

    # Most recently touched note first — the widget highlights row one.
    projects.sort(key=lambda p: p["updated"] or "", reverse=True)

    briefing = generate_daily_briefing(projects)
    return todos, projects, briefing


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
# NEWS
# ─────────────────────────────────────────────────────────────────────────

def _entry_published(entry):
    """Entry publish time as a local-time ISO string, or None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return dt.datetime.fromtimestamp(calendar.timegm(st)).isoformat(timespec="minutes")
    except Exception:
        return None


def collect_news():
    if feedparser is None:
        return [{"error": "feedparser not installed"}]
    items = []
    for label, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:NEWS_MAX_PER_FEED]:
                items.append({
                    "source": label,
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "published": _entry_published(entry),
                })
        except Exception as e:
            items.append({"source": label, "title": f"[fetch error: {e}]",
                          "link": "", "published": None})
    # Newest first for the widget's live-feed layout (undated items last,
    # keeping their original feed order).
    items.sort(key=lambda n: n.get("published") or "", reverse=True)
    return items


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
    check_ollama_connection()

    todos, projects, briefing = collect_notes()
    weather = collect_weather()
    greed = collect_greed()
    news = collect_news()
    sectors = collect_sectors()

    cache = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "briefing": briefing,
        "todos": todos,
        "projects": projects,
        "weather": weather,
        "greed": greed,
        "news": news,
        "sectors": sectors,
    }

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: tmp file + rename, so the widget's file watcher never
    # sees a partially-written cache.
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CACHE_PATH)

    print(f"[{dt.datetime.now()}] Cache written to {CACHE_PATH}")


if __name__ == "__main__":
    run()

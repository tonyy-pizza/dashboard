# Dashboard — joputer edition (retro CRT)

Two-piece desktop dashboard:

- **`collector.py`** — headless, standalone data gatherer. Pulls notes-derived
  to-dos + project status + daily briefing (Ollama, `keep_alive: "0"` so the
  model unloads from VRAM immediately), weather (Open-Meteo), CNN Fear & Greed
  index, market news (RSS), and sector ETF performance (yfinance). Writes
  everything **atomically** to `cache.json` (tmp file + rename).
- **`dashboard_widget.py`** — frameless PyQt6 window, retro CRT theme.
  **Never fetches data itself.** It watches `cache.json` with a
  `QFileSystemWatcher` (no polling) and re-renders on change. The ↻ button
  spawns `collector.py` as a subprocess via `CollectorRunner` — that is the
  intended refresh mechanism.

## Market data layer

- **`market_data.py`** — shared fetch layer for the stocks scripts. Owns the
  HTTP session, the on-disk JSON cache (`cache\financials`, `cache\prices`, `cache\screener`),
  the retry/backoff policy and ticker identity resolution. **Nothing else
  should call yfinance directly** — import `get_info`, `get_price_history`,
  `get_avg_volume`, `screen_page` or `dedupe_tickers` from here. Network
  failures come back as `None` (or a stale cache entry), never as an
  exception. Run `py market_data.py` for its self-test.
- **`universe_screen.py`** — Stage 0 of the scan pipeline. Runs a deliberately
  loose `EquityQuery` (P/E ceiling, volume floor, market-cap floor, US by
  default, `--include-canada` for TSX) once per sector, pages through the
  results 250 at a time, collapses cross-listings and dual-class shares with
  `dedupe_tickers()`, and writes `data\candidates.json`. Fine-grained filtering
  is a downstream job; this file only casts the net. Rows are tagged with their
  listing currency and never sorted or compared across currencies.

## Setup

```
pip install PyQt6 yfinance requests feedparser curl_cffi
```

Paths (cache location, notes dir, collector script) are configured at the top
of each file.

## Fonts

The design uses **VT323** (display/numerals) and **IBM Plex Mono** (body),
both free on Google Fonts (OFL). Either install them system-wide, or drop the
`.ttf` files into a `fonts/` folder next to `dashboard_widget.py` — they're
loaded automatically at startup. Without them the widget falls back to
JetBrains Mono / Cascadia / Consolas.

- https://fonts.google.com/specimen/VT323
- https://fonts.google.com/specimen/IBM+Plex+Mono

## Run

```
py collector.py            # populate/refresh cache.json
pythonw.exe dashboard_widget.py   # launch the widget (startup shortcut target)
```

## Panels

| Panel | Data source |
|---|---|
| Clock | local wall clock (UI-only timer; never touches data) |
| Weather | Open-Meteo current + daily H/L + hourly sparkline |
| Daily briefing | Ollama narrative over project note summaries |
| To do | Ollama-extracted action items from org notes (toggle is UI-local) |
| Project status | Ollama per-note summaries, ordered by note file mtime |
| Greed index | CNN Fear & Greed (score, zone gauge, Δ vs. yesterday) |
| Sector analysis | S&P 500 + TSX sector ETFs, 1-day change, diverging bars |
| News | Market RSS feeds, newest first, click to open |

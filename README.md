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

## Setup

```
pip install PyQt6 yfinance requests feedparser
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

## ETF evaluator

`etf.py` is a standalone CLI (not part of the widget) that scores a single
ETF across seven dimensions — cost, liquidity, structure, risk, income,
performance, concentration — with type-aware weights.

```
python etf.py SPY
python etf.py XIC.TO      # exchange suffix skips the disambiguation probe
python etf.py             # interactive
```

What the score means: it grades **how well a fund implements its own
category** (fees against peers, liquidity, index fidelity, concentration).
It does not say whether that category belongs in your portfolio.

Dimensions are scored only where Yahoo actually supplies the data. Weights
are renormalized over what was measured and the report prints a coverage
figure; below 55% coverage no composite is emitted, so a fund with thin
data reads as *unmeasured* rather than *mediocre*.

`test_etf.py` is an offline regression suite — it reconstructs yfinance's
data shapes and synthesises price series with known analytic answers, so it
needs no network:

```
python test_etf.py
```

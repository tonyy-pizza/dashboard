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

## Calendars

The calendar panel is built from published iCal (`.ics`) addresses — no OAuth,
no app registration, no tokens. **One address covers exactly one calendar**,
never a whole account. That is why a shared calendar that sits happily in your
Google Calendar sidebar does not appear on the dashboard: the sidebar is a view
over many calendars, the feed is one of them. Each calendar you want shown
needs its own address listed alongside the others.

List them one per line in `calendar_url.txt` (gitignored — these URLs grant
read access to the calendar), optionally naming each one:

```
# one calendar per line; "Label = URL" to name it
Personal = https://calendar.google.com/calendar/ical/…/private-…/basic.ics
Family   = https://calendar.google.com/calendar/ical/…/private-…/basic.ics
https://outlook.office365.com/owa/calendar/…/reachcalendar.ics
```

Events from every feed merge into one grid. An invite that lands on both your
own and a shared calendar is drawn once, and the label shows in the event
popup under `calendar`. If one feed is unreachable the others still render —
the panel reports the failure instead of going blank.

Where to find each address:

| Calendar | Where |
|---|---|
| One you own (Google) | Settings → *Settings for my calendars* → the calendar → Integrate calendar → **Secret address in iCal format** |
| Shared, owned by someone else | Google shows **no** secret address for it. Ask the owner for theirs (it is per-calendar, not per-account), or have them tick *Make available to public* and use the public iCal address |
| Outlook / Exchange | Settings → Calendar → Shared calendars → *Publish a calendar* → *Can view all details* → copy the link ending `.ics` |

Check what the dashboard can actually see:

```
py calendar_feed.py
```

It fetches each configured feed in turn, reports per-calendar event counts,
prints this week day by day, and never prints the secret part of a URL.

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

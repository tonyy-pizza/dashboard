# Dionysus — joputer dashboard

Frameless PyQt6 desktop dashboard. Two pieces, two very different data flows.

- **`collector.py`** — headless, standalone gatherer for the *read-only*
  panels: weather (Open-Meteo), CNN Fear & Greed, sector ETF performance
  (yfinance) and this week's Google Calendar. Writes everything **atomically**
  to `cache.json` (tmp file + rename).
- **`dashboard_widget.py`** — the window. It **never fetches remote data**. It
  watches `cache.json` with a `QFileSystemWatcher` (no polling) and re-renders
  on change; the ↻ button spawns `collector.py` through `CollectorRunner`.

The interactive panels break that flow on purpose. To Do, Habit Tracker,
Journal and Rough Notes are written **straight from the widget to their own
files** — a click has to show up immediately, and `cache.json` stays
collector-owned. Nothing about those panels goes through the watcher.

No Ollama anywhere. Local inference used to lock the machine up for ~5 minutes
on every refresh, so the Daily Briefing, AI-inferred To Do and Project Status
panels are gone, and so is the News RSS panel.

## Setup

```
pip install PyQt6 orgparse yfinance requests
pip install google-api-python-client google-auth-oauthlib    # calendar only
```

Paths live in `paths.py`. They default to the real joputer locations and can
be pointed elsewhere with `DASHBOARD_DATA_DIR` / `DASHBOARD_NOTES_DIR` (that's
how the tests run off-machine).

## Google Calendar — one-time setup, by hand

No OAuth credentials exist yet. Until they do, the Calendar panel shows these
steps instead of a week grid and nothing else is affected:

1. console.cloud.google.com → new project
2. APIs & Services → enable **Google Calendar API**
3. OAuth consent screen → External → add your own account as a test user
4. Credentials → Create OAuth client ID → **Desktop app**
5. Download the JSON to
   `C:\Users\joey\dashboard-project-files\credentials.json`
6. Run `py collector.py --auth` once — it opens a browser, you approve, and
   the token is cached to `token.json`

After that every headless collector run refreshes the token silently.

## Files

| File | Owner | Notes |
|---|---|---|
| `cache.json` | collector | weather, greed, sectors, calendar |
| `todo.json` | widget | text, priority tier, done flag, manual order |
| `habits.json` | widget | name, goal, archived flag, completed days by ISO date |
| `journal.org` | widget **and Emacs** | org datetree in `doomnotes/`, stays hand-editable |
| `rough_notes.txt` | widget | freeform scratch panel, autosaved |
| `credentials.json` / `token.json` | you / Google | calendar OAuth |

### journal.org

Standard Doom `file+datetree` structure — the same one the `j` capture
template produces, so entries written here and entries written in Emacs are
the same thing:

```org
* 2026
** 2026-08 August
*** 2026-08-16 Sunday
**** Entry
:PROPERTIES:
:RATING: 7.3
:END:
body text…
```

Parsing goes through `orgparse`, and days are matched on the ISO date prefix
of the heading plus the nesting — not on org's full generated date string, so
`2026-08-16 Sunday`, `2026-08-16 Sun` and a bare `2026-08-16` all resolve to
the same day. Writing splices into the existing lines: today's entry is
updated in place rather than appended twice, and other captures, extra
properties and sub-headings under a day survive untouched.

## Panels

| Group | Panel | Source |
|---|---|---|
| today's items | Weather | Open-Meteo, current + H/L + hourly sparkline |
| today's items | Calendar | Google Calendar, this week, read-only |
| today's items | To Do | `todo.json` — add, complete, delete, drag within a tier |
| today's items | Habit Tracker | `habits.json` — click any day in the year grid |
| today's items | Journal | `journal.org` — one entry/day, rating 1.0–10.0 |
| at a glance | Greed Index | CNN Fear & Greed |
| at a glance | Sector Analysis | S&P 500 + TSX sector ETFs, 1-day change |
| freeform | Rough Notes | `rough_notes.txt`, autosaved |

Weather and Calendar share the top row at roughly 30/70 — the calendar is the
anchor of the group and gets the height and width to match. To Do and the
habit tracker share the next row, so the habit grid uses small dots sized to
fit a full year in a half-width column.

The wall clock lives in the title bar rather than in a panel of its own.

## Look

Deep navy ground (`#0B1120`), panels `#141B2E` on `#232E47` borders, cream
text (`#F0E6D2`), white highlights, hard corners, lowercase panel titles.
**Fraunces** for titles, **Inter** for everything else, with tabular numerals
wherever numbers need to line up. Drop the `.ttf` files into a `fonts/` folder
next to `dashboard_widget.py` (Google Fonts, OFL) and they load at startup;
without them the widget falls back to installed serif/sans faces.

- https://fonts.google.com/specimen/Inter
- https://fonts.google.com/specimen/Fraunces

The app runs under Qt's Fusion style with a dark palette, set in
`theme.apply_app_style`. Windows' native style only half-honours stylesheets
on complex widgets — the rating spinner and the month dropdown come out with
light chrome otherwise.

## Run

```
py collector.py                      # populate/refresh cache.json
py collector.py --auth               # one-time Google Calendar consent
pythonw.exe dashboard_widget.py      # launch the widget (shell:startup target)
```

Windows quirks that are known to matter: the firewall stays disabled across
all three profiles after reboot, scripts are invoked with `py` (not
`python`), and autostart uses `pythonw` so nothing flashes a console.

## Tests

```
python -m pytest
```

102 tests, no display needed (Qt runs offscreen via `tests/conftest.py`).
They cover the org datetree round-trip, the JSON stores, the calendar shaping
and the widget's wiring — panels build, clicks reach the right file, and a
`cache.json` renders without blowing up.

## Refresh behaviour

The collector is re-run every 20 minutes in the background so the calendar and
market data don't go stale (toggleable in the ⚙ menu). That is a *data*
refresh, not a poll: the widget still only repaints when `QFileSystemWatcher`
sees a new `cache.json`. The other timers are the title-bar clock and the
Rough Notes autosave debounce.

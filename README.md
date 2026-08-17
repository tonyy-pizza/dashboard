# Dionysus — joputer dashboard

Frameless PyQt6 desktop dashboard. Two pieces, two very different data flows.

- **`collector.py`** — headless, standalone gatherer for the *read-only*
  panels: weather (Open-Meteo), CNN Fear & Greed, sector ETF performance
  (yfinance) and this week's calendar (a secret iCal feed). Writes everything
  **atomically** to `cache.json` (tmp file + rename).
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
py -m pip install PyQt6 orgparse yfinance requests
py -m pip install icalendar recurring-ical-events   # calendar only
```

`orgparse` reads and writes the journal datetree; `icalendar` and
`recurring-ical-events` parse the calendar feed. Both are optional in the same
way: if a package is missing, its panel says so and the rest of the dashboard
still starts.

Paths live in `paths.py`. They default to the real joputer locations and can
be pointed elsewhere with `DASHBOARD_DATA_DIR` / `DASHBOARD_NOTES_DIR` (that's
how the tests run off-machine).

## Calendar — point it at your secret iCal address

No Google API, no OAuth, no consent screen. The collector fetches one `.ics`
file over HTTPS, so this works with any provider that publishes a private
iCal address. Until a URL is set, the Calendar panel shows these steps instead
of a week grid and nothing else is affected:

1. Google Calendar → Settings → click your calendar in the left sidebar
2. Scroll to **Integrate calendar** → copy **Secret address in iCal format**
   (it ends in `/basic.ics`)
3. Put it in **one** of two places:
   - `ICAL_URL` at the top of `calendar_feed.py`, replacing
     `PASTE_YOUR_SECRET_ICAL_URL_HERE`, or
   - the first line of
     `C:\Users\joey\dashboard-project-files\calendar_url.txt`

**Anyone with that URL can read your calendar.** If this folder is a git
repo, use `calendar_url.txt` — it's gitignored, so the URL can't be committed
by accident. `calendar_feed.py` is tracked, so a URL pasted there would be.
Regenerate the address from the same settings page if it ever leaks.

Recurring events, cancelled occurrences and multi-day all-day events are all
handled — `recurring-ical-events` expands the RRULEs for the week being
displayed.

## Google ↔ Outlook two-way sync (`calendar_sync.py`)

Standalone from the dashboard. The Calendar panel above *reads* an ICS feed
for display; this script *writes* to both calendars, so it has its own
credentials, its own schedule, and its own log.

```
py -m pip install msal google-api-python-client google-auth-oauthlib

py calendar_sync.py --auth-google      # one browser sign-in
py calendar_sync.py --auth-outlook     # one device code
py calendar_sync.py --dry-run          # ← always do this first
py calendar_sync.py                    # for real
```

**What it does.** Primary calendar on each side. A rolling window of 7 days
back to 120 days forward, recomputed every run; anything outside it is left
alone entirely. Creates and edits propagate both ways. Edited on both sides
since the last run → the most recent edit wins, compared on Graph's
`lastModifiedDateTime` against Google's `updated`.

**What it deliberately doesn't do.**

- **Deletions never propagate.** Delete an event and its mirror stays put for
  you to remove by hand. A sync bug that deletes real calendar entries is
  worse than a stale copy.
- **Attendees are mirrored as text in the description, not as real
  attendees.** Everything travels — names, addresses, subject, description,
  location — but a mirrored copy never sends its own invitations. Without
  this, every work meeting would re-invite clients from your personal Gmail
  and every personal event would invite people from the work account.
- **Recurring events are synced as individual occurrences**, since both APIs
  expand them for us (`calendarView` on Graph, `singleEvents=True` on
  Google). Consequence: "this and all future events" isn't a single edit
  here — each affected occurrence updates itself on the next run instead.

**Setup — Microsoft.** You have Global Admin, so this is self-service:

1. portal.azure.com → Microsoft Entra ID → App registrations → New registration
2. Accounts in this organizational directory only; no redirect URI
3. Authentication → Advanced → **Allow public client flows: Yes**
4. API permissions → Microsoft Graph → Delegated → **Calendars.ReadWrite**
5. API permissions → **Grant admin consent for Agrotek**
6. Overview → put the ids in `graph_app.json`:
   `{"client_id": "…", "tenant_id": "…"}`
7. `py calendar_sync.py --auth-outlook` and enter the code it prints

It's a public client, so there is no client secret to store anywhere. The
refresh token is cached in `graph_token_cache.json` for unattended runs.

**Setup — Google.** A separate OAuth client from anything else here, because
this needs the read/write `calendar` scope: enable the Calendar API, create a
**Desktop app** OAuth client, save it as `gcal_sync_credentials.json`, then
`py calendar_sync.py --auth-google`.

**First run.** Nothing is tagged yet, so a plain first run would create a
second copy of every event already sitting in both calendars. Use
`--reconcile`, which links events that already exist on both sides —
identical title, identical start, unambiguous 1:1 — instead of duplicating
them. Ambiguous matches are skipped and sync normally.

```
py calendar_sync.py --dry-run --reconcile     # read the log before going on
py calendar_sync.py --reconcile               # once only
py calendar_sync.py                           # every run after that
```

Read the dry-run log properly before the second command. Every `link` line is
a claim that two events are the same thing; a wrong link welds two unrelated
events together, and unlike a duplicate it won't be obvious later. `--reconcile`
is only needed once — after the first real run everything carries markers, and
the scheduled task runs without it.

**Scheduling.** Task Scheduler on joputer, every 10 minutes:

```
schtasks /create /tn "Calendar sync" /sc minute /mo 10 ^
  /tr "pyw C:\Users\joey\dashboard-project-files\calendar_sync.py"
```

`pyw`, not `py` — the console launcher pops a terminal window on every run,
which on a 10-minute timer is maddening. `pyw` runs it windowless; the run
still lands in `calendar_sync.log` either way, so nothing is lost by not
seeing the console. To change the interval later:
`schtasks /change /tn "Calendar sync" /ri 5`, or fix an existing task's
window problem with
`schtasks /change /tn "Calendar sync" /tr "pyw C:\Users\joey\dashboard-project-files\calendar_sync.py"`.

Polling only — nothing listens on a port, nothing is exposed to the internet.

**Output.** `calendar_sync.log` gets one timestamped line per action
(created / updated / linked / failed, with the reason). `calendar_sync_status.json`
carries `last_run_utc`, `last_success_utc`, `last_error` and
`events_synced_count`; the dashboard's Calendar panel shows a
"synced 4m ago" line from it, picked up by the same `QFileSystemWatcher` as
everything else. A failed run keeps the previous `last_success_utc`, so the
panel can show both.

## Files

| File | Owner | Notes |
|---|---|---|
| `cache.json` | collector | weather, greed, sectors, calendar |
| `todo.json` | widget | text, priority tier, done flag, manual order |
| `habits.json` | widget | name, goal, archived flag, completed days by ISO date |
| `journal.org` | widget **and Emacs** | org datetree in `doomnotes/`, stays hand-editable |
| `rough_notes.txt` | widget | freeform scratch panel, autosaved |
| `calendar_url.txt` | you | optional home for the secret iCal URL, gitignored |
| `gcal_sync_credentials.json` / `gcal_sync_token.json` | you / Google | sync OAuth, read-write scope, gitignored |
| `graph_app.json` / `graph_token_cache.json` | you / Microsoft | sync app ids + token cache, gitignored |
| `calendar_sync_status.json` | calendar_sync | last run, last success, error, count |
| `calendar_sync.log` | calendar_sync | one line per action |

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
| today's items | Calendar | secret iCal feed, this week, read-only |
| today's items | To Do | `todo.json` — add, complete, delete, drag within a tier |
| today's items | Habit Tracker | `habits.json` — `[✓] today` per habit, or click any day in the year grid to backfill |
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
pythonw.exe dashboard_widget.py      # launch the widget (shell:startup target)
```

Windows quirks that are known to matter: the firewall stays disabled across
all three profiles after reboot, scripts are invoked with `py` (not
`python`), and autostart uses `pythonw` so nothing flashes a console.

**If the window doesn't appear at all**, `pythonw` has swallowed a startup
error. Run `py dashboard_widget.py` from a terminal to see it, or read
`dashboard_crash.log` next to the script — the widget writes the traceback
there and shows a dialog before giving up. The usual cause is a half-updated
folder: these modules import each other, so copy the whole set across rather
than only the file that changed.

## Tests

```
python -m pytest
```

234 tests, no display needed (Qt runs offscreen via `tests/conftest.py`).
They cover the org datetree round-trip, the JSON stores, iCal parsing
(recurrence, all-day spans, timezones), the sync engine (loop prevention,
conflict resolution, dry runs, first-run reconciliation) and the widget's
wiring — panels build, clicks reach the right file, and a `cache.json`
renders without blowing up. The sync tests use fake calendar sides, so they
never touch a real account.

## Refresh behaviour

The collector is re-run in the background on three occasions, all of them
*data* refreshes rather than polling — the widget still only repaints when
`QFileSystemWatcher` sees a new `cache.json`:

- **On a timer**, every `AUTO_REFRESH_MINUTES` (default 20), toggleable in
  the ⚙ menu.
- **At launch**, so a reboot doesn't leave last night's data on screen.
- **On wake**, so a machine that slept through the afternoon catches up
  immediately instead of waiting out the timer.

The last two only fire if `cache.json` is older than `STALE_CACHE_MINUTES`
(15), so restarting the widget twice in a row, or a two-minute nap, doesn't
kick off a pointless run. The status line names the reason — `refreshing
(after wake)…`.

Wake is detected by watching the wall clock in the existing one-second clock
tick: Qt timers don't fire while Windows is suspended, so a gap larger than
`WAKE_GAP_SECONDS` (90) means time passed without us running. That covers
sleep, hibernate and lid-close without hooking `WM_POWERBROADCAST`, and it
also catches a corrected system clock, which wants a refresh anyway.

**The sync is separate** — it's a scheduled task, so Windows decides when it
runs. After a reboot or a long sleep it simply resumes its 10-minute cadence,
which means up to one interval of delay. Two optional Task Scheduler settings
close that gap: tick **Run task as soon as possible after a scheduled start is
missed** (task → Settings tab), and/or add a logon trigger:

```
schtasks /create /tn "Calendar sync (logon)" /sc onlogon ^
  /tr "pyw C:\Users\joey\dashboard-project-files\calendar_sync.py"
```

The other timers in the widget are the title-bar clock and the Rough Notes
autosave debounce.

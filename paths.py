"""Every file the dashboard reads or writes, in one place.

The Windows paths are the real targets on joputer. Both roots can be pointed
somewhere else with DASHBOARD_DATA_DIR / DASHBOARD_NOTES_DIR, which is how the
stores get exercised on a non-Windows machine and in the tests.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get(
    "DASHBOARD_DATA_DIR", r"C:\Users\joey\dashboard-project-files"))
DOOMNOTES_DIR = Path(os.environ.get(
    "DASHBOARD_NOTES_DIR", r"C:\Users\joey\doomnotes"))

# Collector-owned, read-only for the widget.
CACHE_PATH = DATA_DIR / "cache.json"
COLLECTOR_SCRIPT = DATA_DIR / "collector.py"

# Dashboard-owned, written directly by the widget.
TODO_PATH = DATA_DIR / "todo.json"
HABITS_PATH = DATA_DIR / "habits.json"
ROUGH_NOTES_PATH = DATA_DIR / "rough_notes.txt"
JOURNAL_PATH = DOOMNOTES_DIR / "journal.org"

# Optional home for the secret iCal address, as an alternative to pasting it
# into calendar_feed.py. Gitignored, so the URL can't be committed by accident.
ICAL_URL_PATH = DATA_DIR / "calendar_url.txt"

#!/usr/bin/env python3
"""
Google Calendar ↔ Outlook two-way sync — joputer edition

Standalone. Nothing here is part of the Dionysus dashboard's Calendar panel,
which reads a one-way ICS feed for display only. This script *writes* to both
calendars, so it lives on its own schedule and its own credentials.

    py calendar_sync.py --dry-run     # show what would happen, write nothing
    py calendar_sync.py               # do it
    py calendar_sync.py --auth-google
    py calendar_sync.py --auth-outlook

What it does, and deliberately doesn't:

  * Primary calendars only, both sides.
  * A rolling window: 7 days back, 120 days forward, recomputed each run.
    Anything outside it is ignored — never synced, never touched.
  * Creates and edits propagate. **Deletions do not.** Deleting an event
    leaves its mirror in place for manual cleanup; a sync bug that deletes
    real calendar entries is worse than a stale copy.
  * Edited on both sides since the last run → most recent edit wins.
  * Recurring events are mirrored as individual occurrences, because both
    APIs expand them for us. Consequence: "this and all future events" is not
    a single edit here — each occurrence updates on its own next run.
  * Attendees are mirrored as text in the description, never as real
    attendees, so a copy never sends invitations. See sync_model.py.

Every mirrored event is tagged with markers naming the side it came from and
the id it came from, which is what keeps a copy from being treated as an
original and bounced back and forth.
"""

import argparse
import datetime as dt
import logging
import sys

from paths import SYNC_LOG_PATH, SYNC_STATUS_PATH
from storage import load_json, save_json
from sync_model import GOOGLE, OUTLOOK, plan, sync_window

log = logging.getLogger("calendar_sync")

DAYS_PAST = 7
DAYS_FUTURE = 120


def configure_logging(verbose=False):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    line = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")
    try:
        SYNC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        to_file = logging.FileHandler(SYNC_LOG_PATH, encoding="utf-8")
        to_file.setFormatter(line)
        log.addHandler(to_file)
    except OSError as e:
        print(f"[sync] could not open log file: {e}")
    to_console = logging.StreamHandler()
    to_console.setFormatter(line)
    log.addHandler(to_console)


# ── applying a plan ──────────────────────────────────────────────────

def apply_actions(actions, sides, dry_run=False) -> int:
    """Run the planned writes. Returns how many succeeded (0 on a dry run —
    nothing was written)."""
    done = 0
    for action in actions:
        if dry_run:
            log.info("would %s", action.describe())
            continue
        side = sides[action.target]
        try:
            if action.kind == "create":
                uid = side.create(action.source_event, action.link_source,
                                  action.link_source_id, action.fingerprint)
                log.info("created on %s (%s) — %s", action.target, uid,
                         _title(action))
            elif action.kind == "update":
                side.update(action.target_uid, action.source_event)
                if action.link_source:
                    side.set_markers(action.target_uid, action.link_source,
                                     action.link_source_id, action.fingerprint)
                log.info("updated on %s — %s [%s]", action.target,
                         _title(action), action.reason)
            else:                                   # relink / link
                side.set_markers(action.target_uid, action.link_source,
                                 action.link_source_id, action.fingerprint)
                log.info("%s on %s — %s [%s]", action.kind, action.target,
                         _title(action), action.reason)
            done += 1
        except Exception as e:
            # One bad event shouldn't abandon the rest of the run.
            log.error("failed to %s on %s (%s): %s", action.kind, action.target,
                      _title(action), e)
    return done


def _title(action) -> str:
    event = action.source_event
    return f"{(event.summary if event else '') or '(no title)'} @ " \
           f"{event.start if event else '?'}"


# ── status file (the dashboard reads this) ───────────────────────────

def write_status(started, synced_count, error=None, dry_run=False,
                 planned=0, skipped=0):
    previous = load_json(SYNC_STATUS_PATH, {})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    status = {
        "last_run_utc": now,
        "last_success_utc": (previous.get("last_success_utc") if error
                             else now),
        "last_error": str(error) if error else None,
        "events_synced_count": synced_count,
        "events_planned_count": planned,
        "events_in_sync_count": skipped,
        "dry_run": bool(dry_run),
        "duration_seconds": round(
            (dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
    }
    try:
        save_json(SYNC_STATUS_PATH, status)
    except OSError as e:
        log.error("could not write status file: %s", e)
    return status


# ── the run ──────────────────────────────────────────────────────────

def run(dry_run=False, reconcile=False, days_past=DAYS_PAST,
        days_future=DAYS_FUTURE, sides=None, now=None):
    started = dt.datetime.now(dt.timezone.utc)
    window_start, window_end = sync_window(now, days_past, days_future)
    log.info("sync window %s → %s%s", window_start.date(), window_end.date(),
             " (dry run)" if dry_run else "")

    try:
        sides = sides or _build_sides()
        google_events = sides[GOOGLE].list_events(window_start, window_end)
        outlook_events = sides[OUTLOOK].list_events(window_start, window_end)
    except Exception as e:
        log.error("could not read calendars: %s", e)
        write_status(started, 0, error=e, dry_run=dry_run)
        return 1

    log.info("read %d google events, %d outlook events",
             len(google_events), len(outlook_events))

    actions = plan(google_events, outlook_events, reconcile=reconcile)
    if not actions:
        log.info("everything already in sync")
    elif reconcile:
        links = sum(1 for a in actions if a.kind == "link")
        log.info("%d planned action(s), %d of them first-run links",
                 len(actions), links)
    else:
        log.info("%d planned action(s)", len(actions))

    synced = apply_actions(actions, sides, dry_run=dry_run)
    in_sync = len(google_events) + len(outlook_events) - len(actions)
    status = write_status(started, synced, dry_run=dry_run,
                          planned=len(actions), skipped=max(in_sync, 0))
    log.info("done — %d written, %d planned%s", synced, len(actions),
             " (dry run: nothing written)" if dry_run else "")
    return 0 if status["last_error"] is None else 1


def _build_sides():
    from google_side import GoogleSide
    from outlook_side import OutlookSide
    return {GOOGLE: GoogleSide(), OUTLOOK: OutlookSide()}


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Two-way sync between the primary Google and Outlook calendars.")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would change without writing anything")
    parser.add_argument("--reconcile", action="store_true",
                        help="first run only: link events that already exist on "
                             "both sides (same title and start) instead of "
                             "creating a second copy of each")
    parser.add_argument("--days-past", type=int, default=DAYS_PAST)
    parser.add_argument("--days-future", type=int, default=DAYS_FUTURE)
    parser.add_argument("--auth-google", action="store_true",
                        help="one-time Google sign-in (opens a browser)")
    parser.add_argument("--auth-outlook", action="store_true",
                        help="one-time Microsoft sign-in (prints a device code)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    if args.auth_google:
        return _authorize("google")
    if args.auth_outlook:
        return _authorize("outlook")

    return run(dry_run=args.dry_run, reconcile=args.reconcile,
               days_past=args.days_past, days_future=args.days_future)


def _authorize(which) -> int:
    if which == "google":
        import google_side as module
        side = module.GoogleSide
    else:
        import outlook_side as module
        side = module.OutlookSide
    try:
        print(side.authorize())
        return 0
    except Exception as e:
        print(f"[{which}] authorization failed: {e}")
        print("Setup steps:")
        for i, step in enumerate(module.SETUP_STEPS, 1):
            print(f"  {i}. {step}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

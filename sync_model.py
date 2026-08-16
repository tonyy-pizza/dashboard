"""The neutral event shape both calendars are translated into.

Google and Graph disagree about almost everything — field names, all-day
representation, timezone handling — so each side converts to `SyncEvent` and
the sync engine only ever reasons about these.

The important piece here is `fingerprint`. It is the *content* of an event
reduced to a stable string, computed identically for an original and for its
mirrored copy, so "has anything actually changed?" is one comparison rather
than a field-by-field diff across two API dialects.

Attendees are mirrored as **text inside the description**, never as real
attendee objects on the copy. Copying live attendees would make each mirror
send its own invitations — work meetings re-inviting clients from a personal
Gmail, and vice versa. The names and addresses still travel (that is the
point), they just don't generate mail. `split_attendee_block` reads them back
out on the next run so both sides fingerprint the same way.
"""

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, replace

# Marker keys written onto mirrored copies, on both platforms.
SOURCE_KEY = "dionysus_sync_source"
ID_KEY = "dionysus_sync_id"
FINGERPRINT_KEY = "dionysus_sync_fp"

GOOGLE = "google"
OUTLOOK = "outlook"

ATTENDEE_HEADER = "— mirrored attendees (not invited) —"
_HEADER_RE = re.compile(r"\n*" + re.escape(ATTENDEE_HEADER) + r"\n?", re.MULTILINE)


@dataclass
class SyncEvent:
    """One occurrence, on one side. Recurring series are never handled as
    series — both APIs expand them for us, so every event here is a single
    materialized occurrence."""

    uid: str
    summary: str = ""
    description: str = ""
    location: str = ""
    start: str = ""              # "2026-08-11T16:00:00Z", or "2026-08-11" all-day
    end: str = ""
    all_day: bool = False
    attendees: tuple = ()
    updated: dt.datetime = None  # last modified, UTC-aware
    # Present only on mirrored copies:
    source: str = None           # which side the original lives on
    source_id: str = None        # the original's uid
    base_fingerprint: str = None # content fingerprint at the last sync

    @property
    def is_copy(self) -> bool:
        return bool(self.source and self.source_id)


def clean(text) -> str:
    """Collapse the whitespace differences the two platforms introduce on
    their own — trailing blanks, \\r\\n — so they don't read as edits."""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def split_attendee_block(description):
    """(description without the mirrored block, attendees found in it)."""
    text = clean(description)
    if ATTENDEE_HEADER not in text:
        return text, ()
    head, _, tail = text.partition(ATTENDEE_HEADER)
    attendees = tuple(line.strip() for line in tail.strip().split("\n") if line.strip())
    return clean(head), attendees


def render_description(event: SyncEvent) -> str:
    """What actually gets written to the other side: the description with the
    attendee block re-appended. Idempotent — an event that already carries a
    block renders to the same string, so mirroring can't stack blocks."""
    body, _ = split_attendee_block(event.description)
    if not event.attendees:
        return body
    listed = "\n".join(event.attendees)
    return f"{body}\n\n{ATTENDEE_HEADER}\n{listed}".strip()


def normalize(event: SyncEvent) -> SyncEvent:
    """Pull any mirrored attendee block out of the description so an original
    (real attendees) and its copy (attendees as text) compare equal."""
    body, parsed = split_attendee_block(event.description)
    return replace(event,
                   summary=clean(event.summary),
                   description=body,
                   location=clean(event.location),
                   attendees=tuple(event.attendees) or parsed)


def fingerprint(event: SyncEvent) -> str:
    """Content hash. Identical for an event and a faithful mirror of it."""
    event = normalize(event)
    parts = [
        clean(event.summary),
        render_description(event),
        clean(event.location),
        event.start or "",
        event.end or "",
        "all-day" if event.all_day else "timed",
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def utc_iso(moment) -> str:
    """Datetime → the canonical string used for start/end comparisons."""
    if moment is None:
        return ""
    if isinstance(moment, dt.datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.isoformat()          # a plain date, for all-day events


def parse_utc(value):
    """Google's `updated` and Graph's `lastModifiedDateTime` → aware UTC."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    # Graph sends 7-digit fractional seconds; fromisoformat wants at most 6.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def sync_window(now=None, days_past=7, days_future=120):
    """Rolling window, recomputed from `now` on every run."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now - dt.timedelta(days=days_past), now + dt.timedelta(days=days_future)


@dataclass
class Action:
    """One planned write. `plan()` produces these; `apply()` performs them."""

    kind: str                    # create | update | relink | link
    target: str                  # which side it happens on
    source_event: SyncEvent = None
    target_uid: str = None
    fingerprint: str = ""
    reason: str = ""
    # For relink/link: the copy that needs its markers written.
    link_source: str = None
    link_source_id: str = None

    def describe(self) -> str:
        title = (self.source_event.summary if self.source_event else "") or "(no title)"
        when = self.source_event.start if self.source_event else ""
        return f"{self.kind} on {self.target}: {title!r} @ {when} — {self.reason}"


def match_key(event: SyncEvent):
    """Key for first-run reconciliation: same title, same start. Deliberately
    strict — a wrong link silently welds two unrelated events together."""
    return (clean(event.summary).casefold(), event.start, event.all_day)


def plan(google_events, outlook_events, reconcile=False):
    """Work out every write needed to bring the two sides together.

    Deletions are never planned: an event removed on one side leaves its
    mirror in place, on purpose.
    """
    sides = {
        GOOGLE: _split(google_events, other=OUTLOOK),
        OUTLOOK: _split(outlook_events, other=GOOGLE),
    }
    actions = []

    if reconcile:
        actions += _reconcile(sides)

    for name, other in ((GOOGLE, OUTLOOK), (OUTLOOK, GOOGLE)):
        originals, _ = sides[name]
        _, copies = sides[other]
        for original in originals:
            copy = copies.get(original.uid)
            if copy is None:
                actions.append(Action(
                    kind="create", target=other, source_event=original,
                    fingerprint=fingerprint(original),
                    link_source=name, link_source_id=original.uid,
                    reason="no mirror yet"))
            else:
                actions += _reconcile_pair(original, copy, name, other)
    return actions


def _split(events, other):
    """(originals, {source_id: copy}) — copies are the ones we made."""
    originals, copies = [], {}
    for event in events:
        event = normalize(event)
        if event.is_copy and event.source == other:
            copies[event.source_id] = event
        elif event.is_copy:
            # A marker naming this same side means something odd (a duplicated
            # copy, or a hand-moved event). Leave it alone rather than
            # treating it as an original and mirroring it back.
            continue
        else:
            originals.append(event)
    return originals, copies


def _reconcile_pair(original, copy, origin_side, copy_side):
    """Both halves of a linked pair, three-way merged against the fingerprint
    stored when they were last synced."""
    original_fp = fingerprint(original)
    copy_fp = fingerprint(copy)
    if original_fp == copy_fp:
        return []

    base = copy.base_fingerprint
    original_changed = original_fp != base
    copy_changed = copy_fp != base

    if original_changed and copy_changed:
        # Edited on both sides since the last run — newest edit wins.
        original_newer = _newer(original, copy)
        winner, loser_side = ((original, copy_side) if original_newer
                              else (copy, origin_side))
        reason = ("edited on both sides, "
                  f"{origin_side if original_newer else copy_side} is newer")
    elif copy_changed:
        winner, loser_side, reason = copy, origin_side, "mirror edited"
    else:
        winner, loser_side, reason = original, copy_side, "original edited"

    new_fp = fingerprint(winner)
    writing_to_copy = loser_side == copy_side
    target_uid = copy.uid if writing_to_copy else original.uid
    actions = [Action(
        kind="update", target=loser_side, source_event=winner,
        target_uid=target_uid, fingerprint=new_fp, reason=reason,
        # Markers live on the copy, so they're rewritten with its content.
        link_source=origin_side if writing_to_copy else None,
        link_source_id=original.uid if writing_to_copy else None)]
    if not writing_to_copy:
        # The base fingerprint lives on the copy, so it needs refreshing even
        # though the copy's own content didn't change.
        actions.append(Action(kind="relink", target=copy_side,
                              target_uid=copy.uid, fingerprint=new_fp,
                              link_source=origin_side, link_source_id=original.uid,
                              source_event=winner, reason="refresh sync marker"))
    return actions


def _newer(left, right) -> bool:
    """True when `left` was modified at or after `right`. A side that reports
    no timestamp loses — better to keep the side we can reason about."""
    if left.updated is None:
        return False
    if right.updated is None:
        return True
    return left.updated >= right.updated


def _reconcile(sides):
    """First-run pass: adopt events that already look like the same thing on
    both calendars, so the first real run doesn't duplicate everything.

    Only unambiguous 1:1 matches are linked. A wrong link quietly welds two
    unrelated events together, which is harder to notice — and to undo — than
    a duplicate.
    """
    google_originals, google_copies = sides[GOOGLE]
    outlook_originals, outlook_copies = sides[OUTLOOK]

    mirrored_outlook_ids = {c.source_id for c in google_copies.values()}
    mirrored_google_ids = {c.source_id for c in outlook_copies.values()}

    candidates = [e for e in google_originals if e.uid not in mirrored_google_ids]
    partners = [e for e in outlook_originals if e.uid not in mirrored_outlook_ids]

    by_key = {}
    for event in partners:
        by_key.setdefault(match_key(event), []).append(event)
    google_key_counts = {}
    for event in candidates:
        key = match_key(event)
        google_key_counts[key] = google_key_counts.get(key, 0) + 1

    actions, adopted = [], set()
    for candidate in candidates:
        key = match_key(candidate)
        matches = by_key.get(key) or []
        if len(matches) != 1 or google_key_counts[key] != 1:
            continue          # ambiguous on either side — leave it alone
        partner = matches.pop()
        # Adopt the Google event as the mirror of the Outlook one. This writes
        # markers only; if their contents differ, the pair pass right below
        # settles which way the content flows.
        actions.append(Action(
            kind="link", target=GOOGLE, target_uid=candidate.uid,
            source_event=candidate, fingerprint=fingerprint(partner),
            link_source=OUTLOOK, link_source_id=partner.uid,
            reason="first-run match on title and start"))
        google_copies[partner.uid] = replace(
            candidate, source=OUTLOOK, source_id=partner.uid,
            base_fingerprint=None)     # no shared history yet: newest wins
        adopted.add(candidate.uid)

    google_originals[:] = [e for e in google_originals if e.uid not in adopted]
    return actions

"""journal.org — one dated entry per day in a standard org datetree.

The file lives in the Doom notes directory and stays fully hand-editable in
Emacs; the dashboard is just a second editor for it. The structure both sides
agree on is the one Doom's `j` capture template (`file+datetree`) already
produces:

    * 2026
    ** 2026-08 August
    *** 2026-08-16 Saturday
    **** Entry
    :PROPERTIES:
    :RATING: 7.3
    :END:
    body text…

Parsing goes through orgparse rather than regex-over-the-whole-file, and the
datetree levels are matched on the ISO date *prefix* of a heading plus the
heading nesting — never on the full generated string. Org's day headings carry
a weekday name whose exact form varies with org version and locale, so
"2026-08-16 Saturday" and "2026-08-16 Sat" both have to resolve to the same
day.

Writes splice into the raw lines at the offsets orgparse reports rather than
re-rendering the document, so anything else in the file (other captures under
the same day, hand-written sub-headings, extra properties, the file header)
survives untouched.
"""

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import orgparse
except ImportError:            # handled per-call, see _require_orgparse
    orgparse = None

from paths import JOURNAL_PATH
from storage import atomic_write_text

ENTRY_HEADING = "Entry"
RATING_PROPERTY = "RATING"
MIN_RATING = 1.0
MAX_RATING = 10.0

MISSING_ORGPARSE = "journal needs orgparse — run: py -m pip install orgparse"


class JournalUnavailable(RuntimeError):
    """The journal can't be read or written — currently only ever raised for
    a missing orgparse. Kept separate from ValueError so the panel can tell a
    setup problem from a bad rating."""


def _require_orgparse():
    if orgparse is None:
        raise JournalUnavailable(MISSING_ORGPARSE)

# Fixed English names, matching org-datetree's own default output. Only ever
# used when *writing* a new heading — reading matches on the date prefix, so a
# heading org wrote in another locale still resolves.
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday"]

_DAY_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH_RE = re.compile(r"^\s*(\d{4})-(\d{2})(?!\d)")
_YEAR_RE = re.compile(r"^\s*(\d{4})(?!\d)")


def heading_date(heading: str):
    """'2026-08-16 Saturday' → date(2026, 8, 16); None if not a day heading."""
    m = _DAY_RE.match(heading or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def heading_month(heading: str):
    """'2026-08 August' → (2026, 8); None if it's a day or not a month."""
    if heading_date(heading) is not None:
        return None
    m = _MONTH_RE.match(heading or "")
    if not m:
        return None
    month = int(m.group(2))
    return (int(m.group(1)), month) if 1 <= month <= 12 else None


def heading_year(heading: str):
    """'2026' → 2026; None if it's a month, a day, or anything else."""
    if heading_date(heading) is not None or heading_month(heading) is not None:
        return None
    m = _YEAR_RE.match(heading or "")
    return int(m.group(1)) if m else None


def _month_key(heading: str):
    """Month heading → the 1st of that month, for ordering sibling months."""
    ym = heading_month(heading)
    return dt.date(ym[0], ym[1], 1) if ym else None


def _year_key(heading: str):
    """Year heading → Jan 1 of that year, for ordering sibling years."""
    year = heading_year(heading)
    return dt.date(year, 1, 1) if year else None


def format_rating(rating) -> str:
    return f"{float(rating):.1f}"


def parse_rating(value):
    if value is None:
        return None
    try:
        return round(float(str(value).strip()), 1)
    except (TypeError, ValueError):
        return None


@dataclass
class JournalEntry:
    date: dt.date
    rating: float
    body: str


class _Rec:
    """One org heading plus the line span of its whole subtree."""

    __slots__ = ("node", "level", "heading", "start", "end")

    def __init__(self, node, start, end):
        self.node = node
        self.level = node.level
        self.heading = node.heading
        self.start = start   # 0-indexed line of the heading itself
        self.end = end       # exclusive end of the subtree


class JournalStore:
    def __init__(self, path: Path = JOURNAL_PATH):
        self.path = Path(path)

    # ── reading ──────────────────────────────────────────────────────
    def read_all(self) -> list:
        _, recs = self._scan(self._read_text())
        entries = []
        for i, rec in enumerate(recs):
            date = heading_date(rec.heading)
            if date is None:
                continue
            entry = self._entry_at(recs, i, date)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e.date)
        return entries

    def read_entry(self, date):
        for entry in self.read_all():
            if entry.date == date:
                return entry
        return None

    def read_month(self, year: int, month: int) -> list:
        return [e for e in self.read_all()
                if e.date.year == year and e.date.month == month]

    def months_with_entries(self) -> list:
        """[(year, month), …] newest first — drives the history dropdown."""
        seen = {(e.date.year, e.date.month) for e in self.read_all()}
        return sorted(seen, reverse=True)

    def ratings_for_year(self, year: int) -> dict:
        return {e.date: e.rating for e in self.read_all() if e.date.year == year}

    # ── writing ──────────────────────────────────────────────────────
    def write_entry(self, date, rating, body: str) -> JournalEntry:
        """Insert or update `date`'s entry. Updating rewrites the existing
        heading's property drawer and body in place; it never appends a second
        heading for a day that already has one."""
        rating = self.validate_rating(rating)
        body = (body or "").strip("\n")

        text = self._read_text()
        lines, recs = self._scan(text)
        base = self._base_level(recs)
        newline = self._detect_newline()

        day_i = self._find_day(recs, date, base, len(lines))
        if day_i is not None:
            entry_i = self._find_entry(recs, day_i)
            if entry_i is not None:
                lines = self._replace_entry(lines, recs, entry_i, rating, body)
            else:
                block = self._entry_block(recs[day_i].level + 1, rating, body)
                lines = self._insert(lines, self._tail_of(lines, recs[day_i]), block)
        else:
            lines = self._insert_new_day(lines, recs, base, date, rating, body)

        self._write(lines, newline)
        return JournalEntry(date=date, rating=rating, body=body)

    @staticmethod
    def validate_rating(rating) -> float:
        if rating is None or (isinstance(rating, str) and not rating.strip()):
            raise ValueError("a rating is required (1.0–10.0)")
        try:
            value = round(float(rating), 1)
        except (TypeError, ValueError):
            raise ValueError(f"rating must be a number, got {rating!r}")
        if not MIN_RATING <= value <= MAX_RATING:
            raise ValueError(f"rating must be between {MIN_RATING:.0f} and "
                             f"{MAX_RATING:.0f}, got {value}")
        return value

    # ── file I/O ─────────────────────────────────────────────────────
    def _read_text(self) -> str:
        """File contents with newlines normalized to \\n — orgparse's drawer
        and heading matching doesn't survive stray carriage returns."""
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def _detect_newline(self) -> str:
        """Whatever ending the file already uses. Rewriting a CRLF file as LF
        would show up as a whole-file diff the next time Joey looks at it."""
        try:
            with open(self.path, "rb") as handle:
                head = handle.read(8192)
        except OSError:
            return "\n"
        return "\r\n" if b"\r\n" in head else "\n"

    def _write(self, lines: list, newline: str = "\n") -> None:
        while lines and not lines[-1].strip():
            lines.pop()
        atomic_write_text(self.path,
                          (newline.join(lines) + newline) if lines else "")

    # ── structure ────────────────────────────────────────────────────
    @staticmethod
    def _scan(text: str):
        """(lines, [_Rec…]) with the recs in document order."""
        _require_orgparse()
        lines = text.splitlines()
        nodes = list(orgparse.loads(text)[1:])  # [0] is the file-level root
        recs = []
        for i, node in enumerate(nodes):
            end = len(lines)
            for later in nodes[i + 1:]:
                if later.level <= node.level:
                    end = later.linenumber - 1
                    break
            recs.append(_Rec(node, node.linenumber - 1, end))
        return lines, recs

    @staticmethod
    def _base_level(recs: list) -> int:
        """Level the year headings sit at — 1 for a plain datetree, deeper if
        the tree is nested under an outline path."""
        levels = [r.level for r in recs if heading_year(r.heading) is not None]
        return min(levels) if levels else 1

    @staticmethod
    def _within(recs: list, start: int, end: int, level: int) -> list:
        return [i for i, r in enumerate(recs)
                if r.level == level and start <= r.start < end]

    def _find_year(self, recs, year, base, n_lines):
        for i in self._within(recs, 0, n_lines, base):
            if heading_year(recs[i].heading) == year:
                return i
        return None

    def _find_month(self, recs, date, base, n_lines):
        year_i = self._find_year(recs, date.year, base, n_lines)
        if year_i is None:
            return None
        year = recs[year_i]
        for i in self._within(recs, year.start, year.end, base + 1):
            if heading_month(recs[i].heading) == (date.year, date.month):
                return i
        return None

    def _find_day(self, recs, date, base, n_lines):
        month_i = self._find_month(recs, date, base, n_lines)
        if month_i is None:
            return None
        month = recs[month_i]
        for i in self._within(recs, month.start, month.end, base + 2):
            if heading_date(recs[i].heading) == date:
                return i
        return None

    def _find_entry(self, recs, day_i):
        """The Entry heading under a day, or the day itself if the rating was
        written straight onto it. None when the day has no entry yet."""
        day = recs[day_i]
        children = self._within(recs, day.start + 1, day.end, day.level + 1)
        for i in children:
            if RATING_PROPERTY in {k.upper() for k in recs[i].node.properties}:
                return i
        for i in children:
            if recs[i].heading.strip().lower() == ENTRY_HEADING.lower():
                return i
        if len(children) == 1:
            return children[0]
        if not children and RATING_PROPERTY in {k.upper() for k in day.node.properties}:
            return day_i
        return None

    def _entry_at(self, recs, day_i, date):
        entry_i = self._find_entry(recs, day_i)
        if entry_i is None:
            return None
        node = recs[entry_i].node
        rating = None
        for key, value in node.properties.items():
            if key.upper() == RATING_PROPERTY:
                rating = parse_rating(value)
        return JournalEntry(date=date, rating=rating,
                            body=(node.body or "").strip("\n").rstrip())

    # ── rendering ────────────────────────────────────────────────────
    @staticmethod
    def _stars(level: int) -> str:
        return "*" * max(1, level)

    def _year_heading(self, level, date):
        return f"{self._stars(level)} {date.year}"

    def _month_heading(self, level, date):
        return (f"{self._stars(level)} {date.year:04d}-{date.month:02d} "
                f"{_MONTH_NAMES[date.month - 1]}")

    def _day_heading(self, level, date):
        return (f"{self._stars(level)} {date.isoformat()} "
                f"{_DAY_NAMES[date.weekday()]}")

    def _entry_block(self, level, rating, body, extra_props=None):
        block = [f"{self._stars(level)} {ENTRY_HEADING}"]
        block += self._drawer(extra_props or {}, rating)
        block += body.splitlines()
        block.append("")
        return block

    @staticmethod
    def _drawer(props: dict, rating) -> list:
        """Property drawer with RATING set, other properties kept in order."""
        out = [":PROPERTIES:"]
        wrote_rating = False
        for key, value in props.items():
            if key.upper() == RATING_PROPERTY:
                out.append(f":{RATING_PROPERTY}: {format_rating(rating)}")
                wrote_rating = True
            else:
                out.append(f":{key}: {value}")
        if not wrote_rating:
            out.append(f":{RATING_PROPERTY}: {format_rating(rating)}")
        out.append(":END:")
        return out

    # ── splicing ─────────────────────────────────────────────────────
    @staticmethod
    def _insert(lines, at, block):
        # Appending straight onto a non-blank last line would jam the new
        # heading against whatever text ended the file.
        if at >= len(lines) and lines and lines[-1].strip():
            block = [""] + block
        return lines[:at] + block + lines[at:]

    @staticmethod
    def _tail_of(lines, rec) -> int:
        """End of a subtree with trailing blank lines trimmed off, so an
        insertion lands directly under the last real line."""
        at = rec.end
        while at > rec.start + 1 and not lines[at - 1].strip():
            at -= 1
        return at

    def _replace_entry(self, lines, recs, entry_i, rating, body):
        rec = recs[entry_i]
        # Stop at the entry's first child heading — sub-headings someone added
        # under the entry in Emacs are not ours to rewrite.
        child_starts = [r.start for r in recs
                        if rec.start < r.start < rec.end and r.level > rec.level]
        region_end = min(child_starts) if child_starts else rec.end
        block = ([lines[rec.start]]
                 + self._drawer(dict(rec.node.properties), rating)
                 + body.splitlines())
        if region_end < len(lines):
            block.append("")
        return lines[:rec.start] + block + lines[region_end:]

    def _insert_new_day(self, lines, recs, base, date, rating, body):
        """Create whichever of year / month / day headings are missing and put
        the entry underneath, keeping siblings in chronological order."""
        n_lines = len(lines)
        month_i = self._find_month(recs, date, base, n_lines)
        if month_i is not None:
            block = ([self._day_heading(base + 2, date)]
                     + self._entry_block(base + 3, rating, body))
            at = self._sorted_slot(lines, recs, recs[month_i], base + 2,
                                   date, heading_date)
            return self._insert(lines, at, block)

        year_i = self._find_year(recs, date.year, base, n_lines)
        if year_i is not None:
            block = ([self._month_heading(base + 1, date),
                      self._day_heading(base + 2, date)]
                     + self._entry_block(base + 3, rating, body))
            at = self._sorted_slot(lines, recs, recs[year_i], base + 1,
                                   dt.date(date.year, date.month, 1), _month_key)
            return self._insert(lines, at, block)

        block = ([self._year_heading(base, date),
                  self._month_heading(base + 1, date),
                  self._day_heading(base + 2, date)]
                 + self._entry_block(base + 3, rating, body))
        at = self._sorted_slot(lines, recs, None, base,
                               dt.date(date.year, 1, 1), _year_key)
        return self._insert(lines, at, block)

    def _sorted_slot(self, lines, recs, parent, level, target_key, key_of):
        """Line to insert at so the new heading lands in date order among its
        siblings: before the first sibling that sorts after it, otherwise at
        the end of the parent (or of the file, for a brand-new year)."""
        start, end = (0, len(lines)) if parent is None else (parent.start, parent.end)
        for i in self._within(recs, start, end, level):
            sibling_key = key_of(recs[i].heading)
            if sibling_key is not None and sibling_key > target_key:
                return recs[i].start
        return len(lines) if parent is None else self._tail_of(lines, parent)

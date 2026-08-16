"""Round-trip tests for the org datetree journal.

The point of most of these is that the file stays something a human can keep
editing in Doom afterwards: existing headings are updated rather than
duplicated, unrelated content under a day survives, and headings org wrote in
a slightly different style still match.
"""

import datetime as dt

import pytest

from journal_store import JournalStore, heading_date, heading_month, heading_year

HEADER = "#+TITLE: Journal\n#+NOTETAKER: skip\n"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(HEADER, encoding="utf-8")
    return JournalStore(path)


def read(store):
    return store.path.read_text(encoding="utf-8")


# ── heading matching ─────────────────────────────────────────────────

def test_heading_matchers_split_year_month_day():
    assert heading_date("2026-08-16 Saturday") == dt.date(2026, 8, 16)
    assert heading_date("2026-08 August") is None
    assert heading_month("2026-08 August") == (2026, 8)
    assert heading_month("2026-08-16 Saturday") is None
    assert heading_year("2026") == 2026
    assert heading_year("2026-08 August") is None
    assert heading_date("not a date") is None


@pytest.mark.parametrize("day_heading", [
    "2026-08-16 Saturday",   # org's default %Y-%m-%d %A
    "2026-08-16 Sat",        # abbreviated weekday
    "2026-08-16",            # no weekday at all
    "2026-08-16 samedi",     # another locale
])
def test_existing_day_is_found_whatever_the_weekday_form(tmp_path, day_heading):
    path = tmp_path / "journal.org"
    path.write_text(
        f"{HEADER}\n* 2026\n** 2026-08 August\n*** {day_heading}\n"
        f"**** Entry\n:PROPERTIES:\n:RATING: 5.0\n:END:\nold body\n",
        encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 9.1, "new body")

    text = path.read_text(encoding="utf-8")
    assert text.count("*** 2026-08-16") == 1
    assert day_heading in text          # heading left exactly as org wrote it
    assert ":RATING: 9.1" in text
    assert "old body" not in text


# ── writing into an empty journal ────────────────────────────────────

def test_first_entry_builds_the_whole_datetree(store):
    store.write_entry(dt.date(2026, 8, 16), 7.3, "first day")

    assert read(store) == (
        HEADER
        + "\n* 2026\n"
        "** 2026-08 August\n"
        "*** 2026-08-16 Sunday\n"
        "**** Entry\n"
        ":PROPERTIES:\n"
        ":RATING: 7.3\n"
        ":END:\n"
        "first day\n"
    )


def test_header_is_preserved(store):
    store.write_entry(dt.date(2026, 8, 16), 7.3, "body")
    text = read(store)
    assert text.startswith("#+TITLE: Journal\n#+NOTETAKER: skip\n")


def test_missing_file_is_created(tmp_path):
    store = JournalStore(tmp_path / "nested" / "journal.org")
    store.write_entry(dt.date(2026, 8, 16), 6.0, "body")
    assert store.read_entry(dt.date(2026, 8, 16)).rating == 6.0


# ── round trip ───────────────────────────────────────────────────────

def test_read_back_what_was_written(store):
    store.write_entry(dt.date(2026, 8, 16), 7.3, "line one\nline two")
    entry = store.read_entry(dt.date(2026, 8, 16))
    assert entry.date == dt.date(2026, 8, 16)
    assert entry.rating == 7.3
    assert entry.body == "line one\nline two"


def test_rating_keeps_one_decimal(store):
    store.write_entry(dt.date(2026, 8, 16), 8, "body")
    assert ":RATING: 8.0" in read(store)
    assert store.read_entry(dt.date(2026, 8, 16)).rating == 8.0


def test_rewriting_a_day_updates_in_place(store):
    store.write_entry(dt.date(2026, 8, 16), 7.3, "first draft")
    store.write_entry(dt.date(2026, 8, 16), 4.5, "second draft")

    text = read(store)
    assert text.count("*** 2026-08-16 Sunday") == 1
    assert text.count("**** Entry") == 1
    assert text.count(":RATING:") == 1
    assert ":RATING: 4.5" in text
    assert "first draft" not in text
    assert "second draft" in text


def test_one_entry_per_day_across_many_writes(store):
    for rating in (5.0, 6.0, 7.0):
        store.write_entry(dt.date(2026, 8, 16), rating, f"take {rating}")
    assert len(store.read_all()) == 1


def test_empty_body_is_allowed(store):
    store.write_entry(dt.date(2026, 8, 16), 5.5, "")
    entry = store.read_entry(dt.date(2026, 8, 16))
    assert entry.rating == 5.5
    assert entry.body == ""


# ── ordering ─────────────────────────────────────────────────────────

def test_days_stay_in_chronological_order_when_backfilled(store):
    store.write_entry(dt.date(2026, 8, 20), 5.0, "later")
    store.write_entry(dt.date(2026, 8, 10), 6.0, "earlier")
    store.write_entry(dt.date(2026, 8, 15), 7.0, "middle")

    text = read(store)
    positions = [text.index(f"*** 2026-08-{d}") for d in ("10", "15", "20")]
    assert positions == sorted(positions)
    assert text.count("** 2026-08 August") == 1


def test_months_and_years_stay_in_order(store):
    store.write_entry(dt.date(2026, 8, 16), 5.0, "aug")
    store.write_entry(dt.date(2026, 3, 2), 6.0, "mar")
    store.write_entry(dt.date(2025, 12, 31), 7.0, "dec 25")
    store.write_entry(dt.date(2027, 1, 1), 8.0, "jan 27")

    text = read(store)
    order = [text.index(h) for h in ("* 2025", "* 2026", "* 2027")]
    assert order == sorted(order)
    assert text.index("** 2026-03 March") < text.index("** 2026-08 August")
    assert [e.date for e in store.read_all()] == [
        dt.date(2025, 12, 31), dt.date(2026, 3, 2),
        dt.date(2026, 8, 16), dt.date(2027, 1, 1),
    ]


# ── coexisting with hand edits ───────────────────────────────────────

def test_other_content_under_the_day_survives(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* 2026\n** 2026-08 August\n*** 2026-08-16 Saturday\n"
        "**** Entry\n:PROPERTIES:\n:RATING: 5.0\n:END:\nold body\n"
        "**** Meeting notes\nkeep me\n",
        encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 6.5, "new body")

    text = path.read_text(encoding="utf-8")
    assert "**** Meeting notes" in text
    assert "keep me" in text
    assert ":RATING: 6.5" in text
    assert "old body" not in text


def test_other_properties_on_the_entry_survive(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* 2026\n** 2026-08 August\n*** 2026-08-16 Saturday\n"
        "**** Entry\n:PROPERTIES:\n:CREATED: [2026-08-16 Sat 21:04]\n"
        ":RATING: 5.0\n:END:\nbody\n",
        encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 9.9, "rewritten")

    text = path.read_text(encoding="utf-8")
    assert ":CREATED: [2026-08-16 Sat 21:04]" in text
    assert ":RATING: 9.9" in text
    assert text.count(":PROPERTIES:") == 1


def test_sub_headings_under_the_entry_survive(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* 2026\n** 2026-08 August\n*** 2026-08-16 Saturday\n"
        "**** Entry\n:PROPERTIES:\n:RATING: 5.0\n:END:\nold body\n"
        "***** Sub thought\nnested text\n",
        encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 6.0, "new body")

    text = path.read_text(encoding="utf-8")
    assert "***** Sub thought" in text
    assert "nested text" in text
    assert "new body" in text


def test_day_written_by_hand_without_an_entry_gets_one(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* 2026\n** 2026-08 August\n*** 2026-08-16 Saturday\n",
        encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 7.0, "body")

    text = path.read_text(encoding="utf-8")
    assert text.count("*** 2026-08-16 Saturday") == 1
    assert "**** Entry" in text
    assert store.read_entry(dt.date(2026, 8, 16)).rating == 7.0


def test_rating_written_straight_onto_the_day_is_read(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* 2026\n** 2026-08 August\n*** 2026-08-16 Saturday\n"
        ":PROPERTIES:\n:RATING: 8.2\n:END:\nbody on the day\n",
        encoding="utf-8")
    store = JournalStore(path)

    entry = store.read_entry(dt.date(2026, 8, 16))
    assert entry.rating == 8.2
    assert entry.body == "body on the day"


def test_datetree_nested_under_an_outline_path(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(
        HEADER + "\n* Journal\n** 2026\n*** 2026-08 August\n"
        "**** 2026-08-16 Saturday\n***** Entry\n:PROPERTIES:\n:RATING: 5.0\n"
        ":END:\nbody\n",
        encoding="utf-8")
    store = JournalStore(path)

    assert store.read_entry(dt.date(2026, 8, 16)).rating == 5.0
    store.write_entry(dt.date(2026, 8, 17), 6.0, "next day")

    text = path.read_text(encoding="utf-8")
    assert "**** 2026-08-17 Monday" in text
    assert text.count("* Journal") == 1


def test_crlf_line_endings_are_preserved(tmp_path):
    path = tmp_path / "journal.org"
    path.write_bytes(
        b"#+TITLE: Journal\r\n#+NOTETAKER: skip\r\n\r\n* 2026\r\n"
        b"** 2026-08 August\r\n*** 2026-08-16 Sunday\r\n**** Entry\r\n"
        b":PROPERTIES:\r\n:RATING: 5.0\r\n:END:\r\nold\r\n")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 6.0, "new")

    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0   # no bare LF left behind
    assert store.read_entry(dt.date(2026, 8, 16)).body == "new"


def test_lf_files_stay_lf(store):
    store.write_entry(dt.date(2026, 8, 16), 6.0, "body")
    assert b"\r\n" not in store.path.read_bytes()


def test_unrelated_top_level_trees_are_untouched(tmp_path):
    path = tmp_path / "journal.org"
    path.write_text(HEADER + "\n* Archive\nold stuff\n", encoding="utf-8")
    store = JournalStore(path)

    store.write_entry(dt.date(2026, 8, 16), 7.0, "body")

    text = path.read_text(encoding="utf-8")
    assert "* Archive" in text
    assert "old stuff" in text
    assert "* 2026" in text


# ── history queries ──────────────────────────────────────────────────

def test_month_and_history_queries(store):
    store.write_entry(dt.date(2026, 7, 1), 5.0, "jul")
    store.write_entry(dt.date(2026, 8, 2), 6.0, "aug a")
    store.write_entry(dt.date(2026, 8, 3), 7.0, "aug b")

    assert [e.date.day for e in store.read_month(2026, 8)] == [2, 3]
    assert store.months_with_entries() == [(2026, 8), (2026, 7)]
    assert store.ratings_for_year(2026)[dt.date(2026, 8, 3)] == 7.0
    assert store.read_entry(dt.date(2026, 1, 1)) is None


def test_empty_journal_reads_as_no_entries(store):
    assert store.read_all() == []
    assert store.months_with_entries() == []


# ── rating validation ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", 0.5, 10.5, -1, "abc"])
def test_bad_ratings_are_rejected(store, bad):
    with pytest.raises(ValueError):
        store.write_entry(dt.date(2026, 8, 16), bad, "body")
    assert store.read_all() == []


@pytest.mark.parametrize("good,expected", [(1, 1.0), (10, 10.0), ("7.3", 7.3), (8.75, 8.8)])
def test_good_ratings_are_accepted(store, good, expected):
    store.write_entry(dt.date(2026, 8, 16), good, "body")
    assert store.read_entry(dt.date(2026, 8, 16)).rating == expected


# ── missing dependency ───────────────────────────────────────────────

def test_without_orgparse_reads_and_writes_raise_a_clear_error(store, monkeypatch):
    import journal_store
    monkeypatch.setattr(journal_store, "orgparse", None)

    with pytest.raises(journal_store.JournalUnavailable, match="pip install orgparse"):
        store.read_entry(dt.date(2026, 8, 16))
    with pytest.raises(journal_store.JournalUnavailable, match="pip install orgparse"):
        store.write_entry(dt.date(2026, 8, 16), 7.0, "body")


def test_a_bad_rating_is_still_a_value_error(store):
    """JournalUnavailable is about setup; a bad rating is not."""
    import journal_store

    with pytest.raises(ValueError) as caught:
        store.write_entry(dt.date(2026, 8, 16), 99, "body")
    assert not isinstance(caught.value, journal_store.JournalUnavailable)

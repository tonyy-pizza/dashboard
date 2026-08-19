"""The collector's write staging and its bounded market fetch.

The network sources themselves aren't tested here — they're thin request
wrappers. What matters is that a slow or failing one can't take the rest of
the dashboard's data down with it.
"""

import json

import pytest

import collector


@pytest.fixture(autouse=True)
def quick_sources(monkeypatch, tmp_path):
    """Nothing in this file should touch the network."""
    monkeypatch.setattr(collector, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(collector, "prepare_directories", lambda: [])
    monkeypatch.setattr(collector, "collect_weather", lambda: {"temp_c": 21})
    monkeypatch.setattr(collector, "collect_greed", lambda: {"score": 62})
    monkeypatch.setattr(collector.calendar_feed, "collect_calendar",
                        lambda: {"week_start": "2026-08-17",
                                 "days": [{"date": "2026-08-17",
                                           "events": [{"summary": "standup"}]}]})
    return tmp_path / "cache.json"


def cached(path):
    return json.loads(path.read_text(encoding="utf-8"))


def explode(**_kwargs):
    raise RuntimeError("Yahoo hung")


# ── staged writes ────────────────────────────────────────────────────

def test_the_calendar_is_written_before_the_market_fetch(quick_sources, monkeypatch):
    """The market fetch can outlast the 300s the widget allows the run, and a
    killed run used to mean nothing was written at all."""
    monkeypatch.setattr(collector, "collect_sectors", explode)

    with pytest.raises(RuntimeError):
        collector.run()

    data = cached(quick_sources)
    assert data["calendar"]["days"][0]["events"] == [{"summary": "standup"}]
    assert data["weather"] == {"temp_c": 21}
    assert "sectors" not in data


def test_a_complete_run_writes_everything(quick_sources, monkeypatch):
    monkeypatch.setattr(collector, "collect_sectors",
                        lambda **kw: {"sp500": [{"ticker": "XLK", "change_pct": 1.2}],
                                      "tsx": []})

    collector.run()

    data = cached(quick_sources)
    assert sorted(data) == ["calendar", "generated_at", "greed", "sectors", "weather"]
    assert data["sectors"]["sp500"][0]["ticker"] == "XLK"


def test_a_failed_source_keeps_its_previous_value(quick_sources, monkeypatch):
    monkeypatch.setattr(collector, "collect_sectors",
                        lambda **kw: {"sp500": [{"ticker": "XLK", "change_pct": 1.2}],
                                      "tsx": []})
    collector.run()

    # Next run: the market fetch dies before it can write.
    monkeypatch.setattr(collector, "collect_sectors", explode)
    with pytest.raises(RuntimeError):
        collector.run()

    # Yesterday's sector numbers are still there rather than gone.
    assert cached(quick_sources)["sectors"]["sp500"][0]["ticker"] == "XLK"


# ── the market fetch is bounded ──────────────────────────────────────

def test_sectors_stop_at_the_deadline_and_carry_the_last_numbers(monkeypatch):
    fetched = []

    def slow_row(ticker, label):
        fetched.append(ticker)
        return {"ticker": ticker, "label": label, "change_pct": 9.9}

    monkeypatch.setattr(collector, "_sector_row", slow_row)
    previous = {"sp500": [{"ticker": t, "label": l, "change_pct": 1.1}
                          for t, l in collector.SP500_SECTOR_ETFS.items()],
                "tsx": [{"ticker": t, "label": l, "change_pct": 2.2}
                        for t, l in collector.TSX_SECTOR_ETFS.items()]}

    # A deadline already in the past: nothing new gets fetched.
    out = collector.collect_sectors(previous=previous, deadline=0)

    assert fetched == []
    assert all(row["change_pct"] in (1.1, 2.2)
               for key in ("sp500", "tsx") for row in out[key])
    assert len(out["sp500"]) == len(collector.SP500_SECTOR_ETFS)


def test_without_a_deadline_everything_is_fetched(monkeypatch):
    monkeypatch.setattr(collector, "_sector_row",
                        lambda t, l: {"ticker": t, "label": l, "change_pct": 1.0})

    out = collector.collect_sectors()

    assert len(out["sp500"]) == len(collector.SP500_SECTOR_ETFS)
    assert len(out["tsx"]) == len(collector.TSX_SECTOR_ETFS)


def test_a_missed_ticker_with_no_history_still_gets_a_row(monkeypatch):
    monkeypatch.setattr(collector, "_sector_row",
                        lambda t, l: {"ticker": t, "label": l, "change_pct": 1.0})

    out = collector.collect_sectors(previous={}, deadline=0)

    assert all(row["change_pct"] is None for row in out["sp500"])
    assert len(out["sp500"]) == len(collector.SP500_SECTOR_ETFS)

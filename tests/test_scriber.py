"""
Unit tests for Phase 1 — Long-term OHLCV DB + Scriber.

Covers:
    - init_db creates the bars table
    - init_db is idempotent (safe to call twice)
    - write_bar inserts a bar that can then be fetched
    - write_bar deduplication: inserting the same bar twice yields one row
    - fetch_bars returns bars sorted ascending by timestamp
    - fetch_bars honours range boundaries (inclusive on both ends)
    - fetch_bars excludes bars outside the requested range
    - fetch_bars returns [] when no bars match
    - fetch_bars isolates results by ticker
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from src.models import OHLCVBar
from src.scriber import db, scriber


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(
    ticker: str = "AAPL",
    ts: str = "2026-01-02T10:00:00+00:00",
    close: float = 100.0,
) -> OHLCVBar:
    """Construct a minimal OHLCVBar for testing."""
    return OHLCVBar(
        ticker=ticker,
        timestamp=datetime.fromisoformat(ts),
        open=close - 1.0,
        high=close + 2.0,
        low=close - 2.0,
        close=close,
        volume=1_000.0,
    )


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh, empty SQLite database in a temp directory."""
    return str(tmp_path / "test_ohlcv.db")


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_bars_table(self, db_path):
        db.init_db(db_path)
        with db._connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bars'"
            ).fetchone()
        assert row is not None, "bars table was not created"

    def test_idempotent(self, db_path):
        """Calling init_db twice must not raise."""
        db.init_db(db_path)
        db.init_db(db_path)  # second call — should be a no-op


# ---------------------------------------------------------------------------
# write_bar / deduplication
# ---------------------------------------------------------------------------

class TestWriteBar:
    def test_inserts_bar(self, db_path):
        bar = _bar()
        scriber.write_bar(bar, db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T00:00:00+00:00"),
            _dt("2026-01-02T23:59:59+00:00"),
            db_path,
        )
        assert len(result) == 1
        assert result[0] == bar

    def test_deduplication_same_bar_twice(self, db_path):
        """Inserting the same (ticker, timestamp) a second time is a silent no-op."""
        bar = _bar()
        scriber.write_bar(bar, db_path)
        scriber.write_bar(bar, db_path)  # duplicate — must not raise

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T00:00:00+00:00"),
            _dt("2026-01-02T23:59:59+00:00"),
            db_path,
        )
        assert len(result) == 1, "duplicate insert should not create a second row"

    def test_deduplication_does_not_overwrite(self, db_path):
        """The original row is preserved when a duplicate is rejected."""
        bar_original = _bar(close=100.0)
        bar_duplicate = _bar(close=999.0)  # same ticker+ts, different close

        scriber.write_bar(bar_original, db_path)
        scriber.write_bar(bar_duplicate, db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T00:00:00+00:00"),
            _dt("2026-01-02T23:59:59+00:00"),
            db_path,
        )
        assert len(result) == 1
        assert result[0].close == 100.0, "original close value should be preserved"


# ---------------------------------------------------------------------------
# fetch_bars — range & ordering
# ---------------------------------------------------------------------------

class TestFetchBars:
    def test_returns_bars_sorted_ascending(self, db_path):
        bars = [
            _bar(ts="2026-01-02T10:02:00+00:00", close=102.0),
            _bar(ts="2026-01-02T10:00:00+00:00", close=100.0),
            _bar(ts="2026-01-02T10:01:00+00:00", close=101.0),
        ]
        for b in bars:
            scriber.write_bar(b, db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T10:00:00+00:00"),
            _dt("2026-01-02T10:02:00+00:00"),
            db_path,
        )
        assert len(result) == 3
        assert result[0].close == 100.0
        assert result[1].close == 101.0
        assert result[2].close == 102.0
        # timestamps strictly ascending
        assert result[0].timestamp < result[1].timestamp < result[2].timestamp

    def test_range_is_inclusive_on_both_ends(self, db_path):
        bars = [
            _bar(ts="2026-01-02T10:00:00+00:00", close=100.0),
            _bar(ts="2026-01-02T10:01:00+00:00", close=101.0),
            _bar(ts="2026-01-02T10:02:00+00:00", close=102.0),
        ]
        for b in bars:
            scriber.write_bar(b, db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T10:00:00+00:00"),   # exactly the first bar
            _dt("2026-01-02T10:01:00+00:00"),   # exactly the second bar
            db_path,
        )
        assert len(result) == 2
        assert result[0].close == 100.0
        assert result[1].close == 101.0

    def test_excludes_bars_before_range(self, db_path):
        scriber.write_bar(_bar(ts="2026-01-01T23:59:59+00:00", close=99.0), db_path)
        scriber.write_bar(_bar(ts="2026-01-02T10:00:00+00:00", close=100.0), db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T10:00:00+00:00"),
            _dt("2026-01-02T23:59:59+00:00"),
            db_path,
        )
        assert len(result) == 1
        assert result[0].close == 100.0

    def test_excludes_bars_after_range(self, db_path):
        scriber.write_bar(_bar(ts="2026-01-02T10:00:00+00:00", close=100.0), db_path)
        scriber.write_bar(_bar(ts="2026-01-02T11:00:00+00:00", close=110.0), db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T10:00:00+00:00"),
            _dt("2026-01-02T10:30:00+00:00"),
            db_path,
        )
        assert len(result) == 1
        assert result[0].close == 100.0

    def test_empty_result_when_no_match(self, db_path):
        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T10:00:00+00:00"),
            _dt("2026-01-02T11:00:00+00:00"),
            db_path,
        )
        assert result == []

    def test_ticker_isolation(self, db_path):
        """A fetch for AAPL must not return MSFT bars sharing the same timestamp."""
        scriber.write_bar(_bar(ticker="AAPL", ts="2026-01-02T10:00:00+00:00", close=100.0), db_path)
        scriber.write_bar(_bar(ticker="MSFT", ts="2026-01-02T10:00:00+00:00", close=200.0), db_path)

        result = db.fetch_bars(
            "AAPL",
            _dt("2026-01-02T00:00:00+00:00"),
            _dt("2026-01-02T23:59:59+00:00"),
            db_path,
        )
        assert len(result) == 1
        assert result[0].ticker == "AAPL"
        assert result[0].close == 100.0

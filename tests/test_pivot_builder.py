"""
Tests for the Pivot Builder wrapper (src/pivot/pivot_calc.py) and the
Pivot Cache (dev_tools/batch_runner/pivot_cache.py).

Tests cover the public interface only — we do NOT test the internals of
pivot_calculator.py (treated as a vendored library).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from src.config import PIVOT_RANGE
from src.models import OHLCVBar
from src.pivot.pivot_calc import build_pivots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bars(n: int, base_price: float = 100.0, seed: int = 42) -> list[OHLCVBar]:
    """
    Generate n synthetic 1-minute OHLCV bars with a random walk around base_price.
    Fixed seed makes tests deterministic.
    Uses at least 200 bars so the KDE has enough data to find peaks reliably.
    """
    rng = np.random.default_rng(seed)
    bars: list[OHLCVBar] = []
    base_time = datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc)
    price = base_price

    for i in range(n):
        # Small random walk: ±0.2% per bar, cluster around round numbers
        pct_change = rng.normal(0, 0.002)
        o = round(price, 4)
        c = round(price * (1 + pct_change), 4)
        h = round(max(o, c) + abs(rng.normal(0, 0.001)) * price, 4)
        lo = round(min(o, c) - abs(rng.normal(0, 0.001)) * price, 4)
        # Volume: realistic range with occasional spikes
        v = round(float(rng.uniform(50_000, 500_000)), 2)

        bars.append(OHLCVBar(
            ticker="TEST",
            timestamp=base_time + timedelta(minutes=i),
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=v,
        ))
        price = c

    return bars


# ---------------------------------------------------------------------------
# build_pivots — edge-case / boundary tests
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty():
    assert build_pivots([]) == []


def test_fewer_than_20_bars_returns_empty():
    bars = make_bars(19)
    assert build_pivots(bars) == []


def test_exactly_20_bars_does_not_raise():
    """20 bars is the minimum the underlying KDE will attempt; must not raise."""
    bars = make_bars(20)
    result = build_pivots(bars)
    # May return [] if KDE finds no peaks — that is valid. Must not raise.
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# build_pivots — output contract tests (requires enough bars for KDE)
# ---------------------------------------------------------------------------

def test_output_sorted_ascending():
    bars = make_bars(200)
    pivots = build_pivots(bars)
    assert pivots == sorted(pivots), "build_pivots must return prices sorted ascending"


def test_all_prices_within_pivot_range():
    """Every returned price must be within ±PIVOT_RANGE of the last bar's close."""
    bars = make_bars(200)
    last_close = bars[-1].close
    pivots = build_pivots(bars)
    lower = last_close * (1 - PIVOT_RANGE)
    upper = last_close * (1 + PIVOT_RANGE)
    for p in pivots:
        assert lower <= p <= upper, (
            f"Pivot {p:.4f} is outside [{lower:.4f}, {upper:.4f}] "
            f"(last close={last_close:.4f}, PIVOT_RANGE={PIVOT_RANGE})"
        )


def test_determinism():
    """Same input must always produce the same output."""
    bars = make_bars(200)
    result1 = build_pivots(bars)
    result2 = build_pivots(bars)
    assert result1 == result2, "build_pivots must be deterministic for identical input"


def test_returns_list_of_floats():
    bars = make_bars(200)
    pivots = build_pivots(bars)
    assert isinstance(pivots, list)
    for p in pivots:
        assert isinstance(p, float), f"Expected float, got {type(p)} for value {p}"


# ---------------------------------------------------------------------------
# Pivot Cache
# ---------------------------------------------------------------------------

import dev_tools.batch_runner.pivot_cache as pivot_cache_mod
from dev_tools.batch_runner.pivot_cache import load_cache, save_cache


def test_cache_round_trip(tmp_path, monkeypatch):
    """save_cache → load_cache must return identical data with int keys."""
    monkeypatch.setattr(pivot_cache_mod, "_CACHE_DIR", str(tmp_path))

    ticker = "AAPL"
    start = date(2024, 1, 2)
    end = date(2024, 3, 31)
    checkpoints: dict[int, list[float]] = {
        0:  [95.10, 97.50, 100.00, 102.50, 105.00],
        30: [96.00, 98.50, 101.00, 103.75],
        60: [97.25, 99.80, 102.30],
    }

    save_cache(ticker, start, end, checkpoints)
    loaded = load_cache(ticker, start, end)

    assert loaded is not None, "load_cache must return a dict, not None"
    assert isinstance(loaded, dict), "load_cache must return a dict"

    # Keys must be int (JSON stores them as strings; must be cast back on load)
    for k in loaded:
        assert isinstance(k, int), f"Key {k!r} must be int, got {type(k)}"

    # Values must be list[float]
    for k, v in loaded.items():
        assert isinstance(v, list), f"Value for key {k} must be list, got {type(v)}"

    assert loaded == checkpoints, (
        f"Round-trip mismatch.\nExpected: {checkpoints}\nGot:      {loaded}"
    )


def test_load_cache_missing_returns_none(tmp_path, monkeypatch):
    """load_cache must return None (not raise) when the file does not exist."""
    monkeypatch.setattr(pivot_cache_mod, "_CACHE_DIR", str(tmp_path))

    result = load_cache("NONEXISTENT", date(2024, 1, 1), date(2024, 3, 31))
    assert result is None, "load_cache must return None for a missing cache file"


def test_cache_file_name_includes_ticker_and_dates(tmp_path, monkeypatch):
    """Cache file must be named <ticker>_<start>_<end>.json."""
    monkeypatch.setattr(pivot_cache_mod, "_CACHE_DIR", str(tmp_path))

    ticker = "TSLA"
    start = date(2024, 2, 1)
    end = date(2024, 4, 30)
    save_cache(ticker, start, end, {0: [100.0, 105.0]})

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == f"{ticker}_{start.isoformat()}_{end.isoformat()}.json"


def test_cache_creates_directory_if_missing(tmp_path, monkeypatch):
    """save_cache must create the cache directory if it does not already exist."""
    nested = tmp_path / "new" / "deep" / "dir"
    monkeypatch.setattr(pivot_cache_mod, "_CACHE_DIR", str(nested))

    save_cache("MSFT", date(2024, 1, 1), date(2024, 3, 31), {0: [200.0, 205.0]})
    assert nested.exists(), "save_cache must create the target directory"
    assert any(nested.iterdir()), "save_cache must write the JSON file"

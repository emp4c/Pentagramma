"""
Integration test for Phase 6 — Batch Runner (dev_tools/batch_runner/runner.py).

Uses a fully synthetic dataset injected via _override_bars; no DB access.
build_pivots is mocked to return a fixed, predictable pivot list so the test
is deterministic and fast.

Synthetic scenario
------------------
Pivots (mocked): [98.0, 100.0, 102.0]
Entry pivot:      100.0  (index 1)
BUY_LIMIT price:  99.9   (100 * (1 - BUY_LIMIT_OFFSET=0.001))
SELL_STOP price:  99.0   ((100 + 98) / 2)

initial_cash = 10_000; MIN_CASH_RESERVE = 100; available = 9_900
quantity = floor(9_900 / 99.9) = 99 shares
cost = 99 * 99.9 = 9_890.10  →  cash_after_buy  = 109.90
proceeds = 99 * 99.0 = 9_801  →  cash_after_sell = 9_910.90

Lookback: 120 bars around 100.0, spaced by minute from 2025-10-01 UTC.
Test window (60 bars):
  Bars  0–4  (UTC 15:00–15:04 = EST 10:00–10:04)
             IDLE, entry signal fires, BUY sent each bar but low=99.95 > 99.9
  Bar   5    (UTC 15:05 = EST 10:05)
             BUY fills (low=99.85 ≤ 99.9)
  Bars  6–19 (UTC 15:06–15:19 = EST 10:06–10:19)
             LONG, low=99.5 > 99.0 — stop-loss not triggered
  Bar  20    (UTC 20:31 = EST 15:31 — post STOP_TRADING_TIME=15:30)
             Stop-loss fires (low=98.5 ≤ 99.0);
             post-hours prevents re-entry after position closes
  Bars 21–59 (UTC 20:32–20:59)
             Post-hours IDLE — analyst returns nothing

Expected ledger: [BUY: 99 @ 99.9, SELL: 99 @ 99.0]  (exactly 2 entries)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from dev_tools.batch_runner.runner import RunResult, run_batch
from src.config import MIN_CASH_RESERVE
from src.models import OHLCVBar

TICKER = "TEST"
START = date(2026, 2, 1)
END = date(2026, 2, 3)
INITIAL_CASH = 10_000.0
MOCK_PIVOTS = [98.0, 100.0, 102.0]

# UTC offsets: February is EST (UTC-5), so UTC 15:xx = EST 10:xx
_IN_HOURS_BASE = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)   # EST 10:00
_POST_HOURS_BASE = datetime(2026, 2, 1, 20, 31, tzinfo=timezone.utc)  # EST 15:31


def _build_bars() -> tuple[list[OHLCVBar], list[OHLCVBar]]:
    """Return (lookback_bars, test_window) for the synthetic scenario."""
    base_lookback_ts = datetime(2025, 10, 1, 15, 0, tzinfo=timezone.utc)
    lookback_bars = [
        OHLCVBar(
            ticker=TICKER,
            timestamp=base_lookback_ts + timedelta(minutes=i),
            open=100.0, high=100.5, low=99.5, close=100.0, volume=1_000.0,
        )
        for i in range(120)
    ]

    test_window: list[OHLCVBar] = []
    for i in range(60):
        if i < 5:
            # Entry signal fires each bar; low > 99.9 so BUY never fills from here
            ts = _IN_HOURS_BASE + timedelta(minutes=i)
            test_window.append(OHLCVBar(
                ticker=TICKER, timestamp=ts,
                open=100.0, high=100.5, low=99.95, close=100.0, volume=1_000.0,
            ))
        elif i == 5:
            # BUY fills: low=99.85 ≤ 99.9
            ts = _IN_HOURS_BASE + timedelta(minutes=i)
            test_window.append(OHLCVBar(
                ticker=TICKER, timestamp=ts,
                open=100.0, high=100.5, low=99.85, close=100.0, volume=1_000.0,
            ))
        elif i < 20:
            # LONG, price holds above stop-loss level (low=99.5 > 99.0)
            ts = _IN_HOURS_BASE + timedelta(minutes=i)
            test_window.append(OHLCVBar(
                ticker=TICKER, timestamp=ts,
                open=100.0, high=100.5, low=99.5, close=99.5, volume=1_000.0,
            ))
        elif i == 20:
            # Stop-loss fires (low=98.5 ≤ 99.0) AND post-hours (EST 15:31)
            test_window.append(OHLCVBar(
                ticker=TICKER, timestamp=_POST_HOURS_BASE,
                open=99.5, high=99.5, low=98.5, close=98.5, volume=1_000.0,
            ))
        else:
            # Post-hours: IDLE, analyst stops early every bar
            ts = _POST_HOURS_BASE + timedelta(minutes=(i - 20))
            test_window.append(OHLCVBar(
                ticker=TICKER, timestamp=ts,
                open=98.5, high=99.0, low=98.0, close=98.5, volume=1_000.0,
            ))

    return lookback_bars, test_window


@pytest.fixture(scope="module")
def run_result() -> RunResult:
    lookback_bars, test_window = _build_bars()
    with patch("dev_tools.batch_runner.runner.build_pivots", return_value=MOCK_PIVOTS):
        return run_batch(
            ticker=TICKER,
            start_date=START,
            end_date=END,
            initial_cash=INITIAL_CASH,
            _override_bars=(lookback_bars, test_window),
        )


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def test_exactly_two_ledger_entries(run_result: RunResult) -> None:
    assert len(run_result.ledger) == 2


def test_bars_processed(run_result: RunResult) -> None:
    assert run_result.bars_processed == 60


def test_buy_before_sell(run_result: RunResult) -> None:
    buy, sell = run_result.ledger[0], run_result.ledger[1]
    assert buy.side == "BUY"
    assert sell.side == "SELL"
    assert buy.timestamp < sell.timestamp


def test_no_ledger_entry_below_min_cash_reserve(run_result: RunResult) -> None:
    for entry in run_result.ledger:
        assert entry.cash_after >= MIN_CASH_RESERVE, (
            f"cash_after {entry.cash_after:.2f} is below MIN_CASH_RESERVE {MIN_CASH_RESERVE}"
        )


def test_final_nav_is_positive(run_result: RunResult) -> None:
    assert run_result.final_nav > 0.0
"""
Batch Runner (Dev / Testing Only).

Responsibility:
    Runs a full simulated trading session over a historical date range.
    Slices the long-term OHLCV DB to obtain a 3-month lookback window (for pivot
    warm-up) plus the test window. Replays the test window bar-by-bar in time order,
    maintaining MachineStatus, routing orders to TestBus, and accumulating the ledger.
    On completion, calls the report writer.

    Parallelism: may run multiple (ticker, start, end) test cases concurrently using
    concurrent.futures or similar. Each run has its own independent MachineStatus and
    Bookkeeper instance.

Public interface:
    run_batch(ticker, start_date, end_date, initial_cash) -> List[LedgerEntry]
"""

from __future__ import annotations

from datetime import date
from typing import List

from src.models import LedgerEntry


def run_batch(
    ticker: str,
    start_date: date,
    end_date: date,
    initial_cash: float = 10_000.0,
) -> List[LedgerEntry]:
    """
    Execute a simulated trading session and return the resulting ledger.

    Args:
        ticker:       Ticker symbol to trade (e.g. "AAPL").
        start_date:   First date of the *test* window (time-zero for simulation).
                      Bars before this date (up to PIVOT_LOOKBACK_DAYS back) are
                      used only to warm up the pivot builder.
        end_date:     Last date of the test window (inclusive).
        initial_cash: Starting cash for the session.

    Returns:
        The complete list of LedgerEntry records produced during the session.
        Also writes a report to outputs/active/ via the report writer.

    Steps:
        1. Fetch lookback bars (start_date - PIVOT_LOOKBACK_DAYS → start_date).
        2. Pre-calculate pivot snapshots at every 30-bar checkpoint → pivot cache.
        3. For each bar in the test window (sequential):
            a. Inject cached pivots if at a 30-bar checkpoint.
            b. Call analyst(bar, status, pivots, recent_bars) → AnalystOrders.
            c. Call trader(AnalystOrders, status, bookkeeper) → BrokerOrders.
            d. Send each BrokerOrder to TestBus; pass back confirmations to bookkeeper.
        4. Call report_writer on completion.
    """
    raise NotImplementedError

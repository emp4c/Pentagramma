"""
Report Writer (Dev / Testing Only).

Responsibility:
    Produces a human-readable text report from a completed batch run.
    One line per bar: timestamp, OHLCV values, machine status at that bar,
    orders issued, and any executions. Followed by a summary section.

    Output path: outputs/active/YYYYMMDD_HHMMSS_report.txt

Public interface:
    write_report(ledger, bars, output_path) -> None
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from src.models import LedgerEntry, OHLCVBar


def write_report(
    ledger: List[LedgerEntry],
    bars: List[OHLCVBar],
    output_path: Path | None = None,
) -> None:
    """
    Write a human-readable trading report to disk.

    Args:
        ledger:      Complete list of ledger entries from the bookkeeper.
        bars:        All bars in the test window (same order as replayed).
        output_path: Override the default output path. If None, generates a
                     timestamped path under outputs/active/.

    Report contents:
        Header: ticker, date range, initial/final NAV, total trades.
        Body: one line per bar with OHLCV, machine status, orders issued,
              execution confirmations received.
        Summary: total trades, P&L, max drawdown, number of stop-loss hits.
    """
    raise NotImplementedError

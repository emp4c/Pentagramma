"""
Smoke test for TradingSession (streaming entry point).

Creates 10 synthetic OHLCVBar objects, feeds them through a TradingSession
backed by TestBus and a temporary SQLite DB, then prints the final ledger.

Exit code 0 = no exceptions raised.
Run: python dev_tools/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Ensure project root is on the path when run directly.
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.models import OHLCVBar
from src.entry.stream_entry import TradingSession
from dev_tools.test_api.test_bus import TestBus

_EST = ZoneInfo("America/New_York")


def _make_bars(n: int = 10) -> list[OHLCVBar]:
    """Generate n synthetic 1-minute OHLCV bars during market hours."""
    # 9:30 EST on a weekday — well within the STOP_TRADING_TIME of 15:30
    base = datetime(2026, 3, 2, 9, 30, 0, tzinfo=_EST).astimezone(timezone.utc)
    bars = []
    price = 150.0
    for i in range(n):
        ts = base + timedelta(minutes=i)
        # Small price drift each bar so the sequence is non-trivial
        close = round(price + i * 0.05, 4)
        bars.append(OHLCVBar(
            ticker="TEST",
            timestamp=ts,
            open=round(close - 0.10, 4),
            high=round(close + 0.20, 4),
            low=round(close - 0.15, 4),
            close=close,
            volume=10_000.0 + i * 100,
        ))
    return bars


def main() -> None:
    bars = _make_bars(10)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = os.path.join(tmp_dir, "smoke_test.db")
        # Windows holds SQLite file locks until GC; ignore cleanup errors.

        # TestBus receives all bars upfront for fill simulation.
        # TradingSession calls send_order() without current_bar_index, so
        # TestBus treats the full bar list as future (backward-compat mode).
        bus = TestBus(bars)

        session = TradingSession(
            ticker="TEST",
            initial_cash=10_000.0,
            bus=bus,
            db_path=db_path,
        )

        for bar in bars:
            session.on_bar(bar)

    ledger = session._bookkeeper.get_ledger()
    print(f"Smoke test complete — {len(ledger)} ledger entries:")
    if ledger:
        for entry in ledger:
            print(
                f"  {entry.side:4s}  qty={entry.quantity:.4f}  "
                f"price={entry.price:.4f}  cash_after={entry.cash_after:.2f}"
            )
    else:
        print("  (no trades — expected when DB has no pivot history at session start)")


if __name__ == "__main__":
    main()
    sys.exit(0)
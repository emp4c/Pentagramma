"""
Batch entry point.

Usage:
    python src/entry/batch.py \\
        --ticker AAPL \\
        --start  2026-02-01 \\
        --end    2026-03-15 \\
        --db     <path to OHLCV DB; defaults to config.DB_PATH> \\
        --cash   10000
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Ensure the project root is on sys.path so dev_tools and src are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Algo Trader — Batch Back-tester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="Start of the test window",
    )
    parser.add_argument(
        "--end", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="End of the test window (inclusive)",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="Path to the OHLCV SQLite database (defaults to config.DB_PATH)",
    )
    parser.add_argument(
        "--cash", type=float, default=10_000.0, metavar="AMOUNT",
        help="Initial cash for the session",
    )
    args = parser.parse_args()

    from dev_tools.batch_runner.runner import run_batch

    result = run_batch(
        ticker=args.ticker,
        start_date=args.start,
        end_date=args.end,
        initial_cash=args.cash,
        db_path=args.db,
    )
    print(
        f"Done. {result.bars_processed} bars processed. "
        f"Final NAV: ${result.final_nav:,.2f}"
    )


if __name__ == "__main__":
    main()

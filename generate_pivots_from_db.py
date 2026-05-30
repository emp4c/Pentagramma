"""
Generate pivots from OHLCV database.

Loads the last 3 months of data from data/ohlcv.db ending on May 22, 2026,
calls build_pivots() on that data, and saves the results to a CSV file.

Usage:
    python generate_pivots_from_db.py
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd

from src.scriber import db
from src.pivot.pivot_calc import build_pivots


def generate_pivots(
    end_date: date = date(2026, 5, 22),
    lookback_days: int = 90,
    db_path: str = "data/ohlcv.db",
    output_csv: str = "data/pivots/generated_pivots.csv",
    ticker: str = "ONDS",
) -> None:
    """
    Generate pivots from the last N months of OHLCV data.

    Args:
        end_date: The end date for the lookback window (default: May 22, 2026).
        lookback_days: Number of days to look back (default: 90 = ~3 months).
        db_path: Path to the SQLite database.
        output_csv: Path to save the pivots CSV file.
        ticker: Ticker symbol to fetch from the database.
    """
    # Calculate start date
    start_date = end_date - timedelta(days=lookback_days)

    print(f"Fetching bars from {start_date} to {end_date} for ticker {ticker}")
    print(f"Database: {db_path}")

    # Fetch bars from database
    bars = db.fetch_bars(
        ticker=ticker,
        from_dt=datetime.combine(start_date, datetime.min.time()),
        to_dt=datetime.combine(end_date, datetime.max.time()),
        db_path=db_path,
    )

    if not bars:
        print(f"Error: No bars found for ticker {ticker} in the specified date range")
        return

    print(f"Fetched {len(bars)} bars")

    # Generate pivots
    print("Calculating pivots...")
    pivots = build_pivots(bars)

    if not pivots:
        print("Warning: No pivots generated (insufficient data or KDE found no peaks)")
        return

    print(f"Generated {len(pivots)} pivots")

    # Save to CSV
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({"pivot_price": pivots})
    df.to_csv(output_path, index=False)

    print(f"Pivots saved to: {output_csv}")
    print(f"Pivot range: {min(pivots):.5f} to {max(pivots):.5f}")


if __name__ == "__main__":
    generate_pivots()

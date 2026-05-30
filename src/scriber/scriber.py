"""
Scriber — Bar Persistence (Streaming Mode Only).

Responsibility:
    Writes each incoming OHLCVBar to the long-term OHLCV SQLite database.
    Append-only: existing rows are never modified. Deduplicates on the
    composite primary key (ticker, timestamp) — inserting a bar that already
    exists is a no-op (not an error).

    Not used in batch mode (batch_runner reads directly from the DB via db.py).

Public interface:
    write_bar(bar: OHLCVBar, db_path=None) -> None
"""

from __future__ import annotations

from src.models import OHLCVBar
from src.scriber import db as _db


def write_bar(bar: OHLCVBar, db_path: str | None = None) -> None:
    """
    Persist a single OHLCV bar to the long-term database.

    Uses ``INSERT OR IGNORE`` so that inserting a bar whose
    (ticker, timestamp) primary key already exists is silently skipped —
    no exception is raised and the existing row is unchanged.

    Initialises the schema on first call (idempotent).

    Args:
        bar:     The closed 1-minute bar to store.
        db_path: Override DB file path (defaults to config.DB_PATH).
                 Pass this in tests to point at a temporary database.
    """
    _db.init_db(db_path)
    ts_str = _db._to_utc_str(bar.timestamp)

    with _db._connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO bars
                (ticker, timestamp, open, high, low, close, volume)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (bar.ticker, ts_str, bar.open, bar.high, bar.low, bar.close, bar.volume),
        )

"""
Scriber — Database Layer.

Responsibility:
    Low-level SQLite access for the long-term OHLCV bar store.
    Provides schema initialisation, bar insertion, and bar fetch.
    Used by scriber.write_bar (streaming) and batch_runner / pivot builder (reads).

    DB file location: controlled by src.config.DB_PATH.
    Pass an explicit db_path to any function to override (used in tests).

Public interface:
    init_db(db_path=None) -> None
    fetch_bars(ticker, from_dt, to_dt, db_path=None) -> List[OHLCVBar]

Internal helpers (used by scriber.py):
    _connect(db_path=None) -> sqlite3.Connection
    _to_utc_str(dt: datetime) -> str
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import src.config as config
from src.models import OHLCVBar


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """
    Open the SQLite database at *db_path* (defaults to config.DB_PATH).
    Creates parent directories and the file if they do not exist.
    Sets row_factory to sqlite3.Row for named-column access.
    """
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _to_utc_str(dt: datetime) -> str:
    """
    Normalise *dt* to a fixed-format UTC ISO-8601 string suitable for
    SQLite lexicographic range comparisons:  YYYY-MM-DDTHH:MM:SS+00:00

    Handles both timezone-aware (converts to UTC) and naive (assumed UTC)
    datetimes.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def init_db(db_path: str | None = None) -> None:
    """
    Create the ``bars`` table if it does not already exist.

    Schema:
        ticker     TEXT    NOT NULL
        timestamp  TEXT    NOT NULL  (ISO-8601 UTC, format YYYY-MM-DDTHH:MM:SS+00:00)
        open       REAL    NOT NULL
        high       REAL    NOT NULL
        low        REAL    NOT NULL
        close      REAL    NOT NULL
        volume     REAL    NOT NULL
        PRIMARY KEY (ticker, timestamp)

    Idempotent — safe to call multiple times.
    """
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bars (
                ticker     TEXT NOT NULL,
                timestamp  TEXT NOT NULL,
                open       REAL NOT NULL,
                high       REAL NOT NULL,
                low        REAL NOT NULL,
                close      REAL NOT NULL,
                volume     REAL NOT NULL,
                PRIMARY KEY (ticker, timestamp)
            )
        """)


def fetch_bars(
    ticker: str,
    from_dt: datetime,
    to_dt: datetime,
    db_path: str | None = None,
) -> List[OHLCVBar]:
    """
    Retrieve bars for *ticker* within the UTC datetime range [from_dt, to_dt]
    (both endpoints inclusive).

    Ensures the table exists before querying (safe to call on a fresh DB).

    Args:
        ticker:   Ticker symbol, e.g. ``"AAPL"``.
        from_dt:  Range start, inclusive (UTC). Naive datetimes treated as UTC.
        to_dt:    Range end, inclusive (UTC). Naive datetimes treated as UTC.
        db_path:  Override DB file path (defaults to config.DB_PATH).

    Returns:
        List of :class:`OHLCVBar` objects sorted ascending by timestamp.
        Empty list if no bars match.
    """
    init_db(db_path)
    from_str = _to_utc_str(from_dt)
    to_str   = _to_utc_str(to_dt)

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT ticker, timestamp, open, high, low, close, volume
            FROM   bars
            WHERE  ticker    = ?
              AND  timestamp >= ?
              AND  timestamp <= ?
            ORDER  BY timestamp ASC
            """,
            (ticker, from_str, to_str),
        ).fetchall()

    return [_row_to_bar(row) for row in rows]


# ---------------------------------------------------------------------------
# Private row mapper
# ---------------------------------------------------------------------------

def _row_to_bar(row: sqlite3.Row) -> OHLCVBar:
    return OHLCVBar(
        ticker=row["ticker"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
    )

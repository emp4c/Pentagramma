"""
Pivot Cache — Batch Runner Optimisation.

Responsibility:
    Pre-computes and serialises pivot snapshots for every 30-bar checkpoint
    in a test window, so the batch runner can inject them without recomputing
    from raw bars on each run.

    Cache files are stored as JSON in: data/pivots_cache/<ticker>_<start>_<end>.json

    Format (see data_model.md):
        {
          "ticker": "AAPL",
          "generated_at": "2026-01-15T10:00:00Z",
          "interval_bars": 30,
          "checkpoints": {
            "0":  [182.10, 184.50, ...],
            "30": [183.00, 185.20, ...],
            ...
          }
        }

Public interface:
    save_cache(ticker, start, end, checkpoints) -> None
    load_cache(ticker, start, end) -> Dict[int, List[float]] | None
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Dict, List

from src.config import PIVOT_INTERVAL_BARS

_CACHE_DIR = "data/pivots_cache"


def _cache_path(ticker: str, start: date, end: date) -> str:
    """Return the absolute-ish path for a cache file given ticker and date range."""
    fname = f"{ticker}_{start.isoformat()}_{end.isoformat()}.json"
    return os.path.join(_CACHE_DIR, fname)


def save_cache(
    ticker: str,
    start: date,
    end: date,
    checkpoints: Dict[int, List[float]],
) -> None:
    """
    Serialise pivot snapshots to a JSON cache file.

    Args:
        ticker:       Ticker symbol.
        start:        Test window start date (used as part of the filename).
        end:          Test window end date (used as part of the filename).
        checkpoints:  Dict mapping bar-index → sorted pivot list.
                      Keys are multiples of PIVOT_INTERVAL_BARS (0, 30, 60, …).
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    payload = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval_bars": PIVOT_INTERVAL_BARS,
        # JSON requires string keys; cast int keys → str on save, str → int on load.
        "checkpoints": {str(k): v for k, v in checkpoints.items()},
    }
    path = _cache_path(ticker, start, end)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_cache(
    ticker: str,
    start: date,
    end: date,
) -> Dict[int, List[float]] | None:
    """
    Load a previously saved pivot cache from disk.

    Args:
        ticker: Ticker symbol.
        start:  Test window start date.
        end:    Test window end date.

    Returns:
        Dict mapping bar-index (int) → pivot list if the cache file exists,
        or None if no matching cache is found.
    """
    path = _cache_path(ticker, start, end)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # JSON keys are always strings; cast back to int for the caller.
    return {int(k): v for k, v in data["checkpoints"].items()}

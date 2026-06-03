# NOTE: get_adaptive_pivots() contains an adaptive bandwidth loop (up to 10 KDE fits)
# targeting 5-8 peaks per side. This is intentional for quality but means each call
# takes ~50-200ms. In batch mode, always use the pivot cache — never call build_pivots()
# inside the per-bar loop.

from __future__ import annotations

import os
import sys
from typing import List

import pandas as pd

from src.config import PIVOT_RANGE
from src.models import OHLCVBar

from .pivot_calculator import get_adaptive_pivots 

def build_pivots(bars: List[OHLCVBar]) -> List[float]:
    """
    Convert bars to DataFrame, call get_adaptive_pivots(), return sorted pivot prices.
    Returns [] if input is empty or fewer than 20 bars (get_adaptive_pivots requirement).

    Args:
        bars: OHLCV bars covering the lookback window (PIVOT_LOOKBACK_DAYS of history).
              Must be sorted ascending by timestamp. Must be non-empty.

    Returns:
        List of pivot price levels, sorted ascending, filtered to within
        ±PIVOT_RANGE of the last bar's close price. Length is variable (can be 0).
    """
    if not bars or len(bars) < 20:
        return []

    timestamps = [b.timestamp.replace(tzinfo=None) for b in bars]
    index = pd.DatetimeIndex(timestamps)
    df = pd.DataFrame(
        {
            "open":   [b.open   for b in bars],
            "high":   [b.high   for b in bars],
            "low":    [b.low    for b in bars],
            "close":  [b.close  for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=index,
    )
    df.sort_index(inplace=True)

    result = get_adaptive_pivots(df, pivots_range=PIVOT_RANGE)

    # Extract prices; discard width, rel_width, density — preserved in pivot_calculator.py
    # for future use but not part of the public interface here.
    return sorted(p["price"] for p in result)

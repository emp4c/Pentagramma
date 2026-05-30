"""
Analyst.

Responsibility:
    Contains all trading logic. Stateless — receives the current bar, machine
    state, pivot list, and recent bars; returns a list of AnalystOrder instructions.
    Never communicates directly with the broker. The returned list is processed
    in order by the Trader.

    Logic specification: docs/analyst_logic.md (authoritative). Any deviation
    from that document is a bug.

Public interface:
    analyse(bar, status, pivots, recent_bars) -> List[AnalystOrder]
"""

from __future__ import annotations

from typing import List

from src.models import OHLCVBar, MachineStatus, AnalystOrder


def analyse(
    bar: OHLCVBar,
    status: MachineStatus,
    pivots: List[float],
    recent_bars: List[OHLCVBar],
) -> List[AnalystOrder]:
    """
    Evaluate the current bar against machine state and pivots; emit trading instructions.

    Args:
        bar:         The latest closed 1-minute bar.
        status:      Current machine state (read-only inside the analyst).
        pivots:      Current pivot list, sorted ascending. Immutable within the
                     30-bar window in which it was computed.
        recent_bars: The last VWAP_WINDOW_BARS bars (including `bar`), used to
                     compute the rolling VWAP in the IDLE branch.

    Returns:
        A (possibly empty) list of AnalystOrder objects, to be processed in order
        by the Trader. An empty list means "do nothing this bar".

    Stop conditions (checked first — if any is true, returns []):
        - Daily loss limit breached: current_nav < initial_nav * (1 - DAILY_LOSS_LIMIT)
        - Post-hours idle: status.position == "IDLE" and current_time_EST > STOP_TRADING_TIME

    IDLE branch:
        1. Emit CANCEL_ALL_BUYS.
        2. Compute VWAP of recent_bars and find closest pivot.
        3. If pivot_vwap == pivot_close: emit BUY_LIMIT + conditional SELL_LIMIT stop-loss.
           If i == 0 (no pivot below for stop-loss): emit nothing, log warning.

    LONG branch:
        1. If close > pivots[watermark_level + 1]: advance watermark, emit UPDATE_STOPLOSS.
        2. If current_time_EST > STOP_TRADING_TIME: emit SELL_MARKET.
        3. Otherwise: emit nothing (existing stop-loss remains active at broker).
    """
    raise NotImplementedError

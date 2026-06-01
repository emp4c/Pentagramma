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
    analyse(bar, status, pivots, recent_bars, bookkeeper) -> List[AnalystOrder]

--- Order Attribution Convention (UUID linkage) ---
The analyst generates a uuid4 for each BUY_LIMIT order and stores it in
BUY_LIMIT.condition.  The paired SELL_LIMIT (stop-loss) carries the string
"on_fill:{uuid}" in its own condition field.  The Trader MUST use the value
found in BUY_LIMIT.condition as the BrokerOrder.order_id for that buy, so that
the stop-loss condition string matches the live broker order ID.

--- Stop-Loss Tracking Convention ---
The single-position constraint guarantees that when the machine is LONG, every
entry in status.pending_orders is a SELL order (the active stop-loss).
Therefore: len(pending_orders) == 0 while LONG implies the stop-loss has
been unexpectedly removed and must be re-emitted.

--- Priority Rule ---
"Stop Condition Wins": time-based and risk-based exit signals (STOP_TRADING_TIME,
daily loss limit) take absolute precedence over trailing watermark adjustments.
Within the LONG branch, stop-condition checks run before watermark logic.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Callable, List
from zoneinfo import ZoneInfo

from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import BUY_LIMIT_OFFSET, DAILY_LOSS_LIMIT, STOP_TRADING_TIME
from src.models import AnalystOrder, MachineStatus, OHLCVBar

_logger = logging.getLogger(__name__)
_EST = ZoneInfo("America/New_York")

_VIRTUAL_STEP = 0.015      # geometric spacing for virtual pivot grid extension
_OOS_BUFFER = _VIRTUAL_STEP / 2   # 0.75% half-step; defines in-scope boundary around pivot extremes


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _vwap(bars: List[OHLCVBar]) -> float:
    """VWAP = sum(typical_price * volume) / sum(volume); typical_price = (H+L+C)/3."""
    total_tp_vol = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars)
    total_vol = sum(b.volume for b in bars)
    return total_tp_vol / total_vol


def _closest_pivot(value: float, pivots: List[float]) -> tuple[float, int]:
    """Return (pivot_value, index) of the pivot closest to value."""
    idx = min(range(len(pivots)), key=lambda i: abs(pivots[i] - value))
    return pivots[idx], idx


def _stoploss_price(pivots: List[float], idx: int) -> float:
    """Midpoint between pivots[idx] and pivots[idx-1]. Caller must ensure idx >= 1."""
    return (pivots[idx] + pivots[idx - 1]) / 2


def _emit_missing_stoploss(
    close: float, pivots: List[float], bookkeeper: Bookkeeper
) -> List[AnalystOrder]:
    """
    Build a replacement SELL_LIMIT stop-loss anchored to the current close price.

    Called when the machine is LONG but pending_orders is empty, which under
    the single-position constraint means the stop-loss was unexpectedly removed.

    Stop-loss is re-anchored to the closest pivot to close (not the original
    watermark), so the protection level follows wherever price currently is.
    Returns [] without emitting if i == 0 (no pivot below) or shares_held <= 0.
    """
    _, i = _closest_pivot(close, pivots)
    if i == 0:
        _logger.warning(
            "Cannot re-emit stop-loss: closest pivot to close %.4f is at index 0 — no pivot below",
            close,
        )
        return []
    qty = bookkeeper.shares_held()
    if qty <= 0:
        return []
    return [AnalystOrder(
        type="SELL_STOP",
        price=_stoploss_price(pivots, i),
        size="ALL_SHARES",
    )]


def _is_out_of_scope(close: float, pivots: List[float]) -> bool:
    """True when close breaks past the half-step buffer around the pivot extremes."""
    return close < pivots[0] * (1 - _OOS_BUFFER) or close > pivots[-1] * (1 + _OOS_BUFFER)


def _extend_pivots(close: float, pivots: List[float]) -> List[float]:
    """
    Append or prepend virtual pivots at _VIRTUAL_STEP geometric spacing until the
    pivot closest to close is covered.

    The integer j ≥ 1 used in each direction is:
      above: max_pivot * (1 + STEP)^j  closest to close
      below: min_pivot * (1 - STEP)^j  closest to close
    """
    if close > pivots[-1]:
        max_p = pivots[-1]
        j = max(1, round(math.log(close / max_p) / math.log(1 + _VIRTUAL_STEP)))
        return pivots + [max_p * (1 + _VIRTUAL_STEP) ** k for k in range(1, j + 1)]
    else:
        min_p = pivots[0]
        j = max(1, round(math.log(close / min_p) / math.log(1 - _VIRTUAL_STEP)))
        extension = [min_p * (1 - _VIRTUAL_STEP) ** k for k in range(j, 0, -1)]
        return extension + pivots


def _resolve_pivots(
    close: float,
    pivots: List[float],
    recalc_hook: Callable[[], List[float]] | None,
) -> List[float]:
    """
    Guarantee the returned pivot list covers close.

    Step 1 — if a recalc_hook is provided, call it for fresh pivots.
    Step 2 — if still OOS (or no hook), extend virtually at ±_VIRTUAL_STEP spacing.

    The hook should follow the same rebuild path used at PIVOT_INTERVAL_BARS
    checkpoints in stream_entry.on_bar().
    # TODO: stream_entry.on_bar() must supply this hook once it is implemented,
    #       invoking the pivot builder identically to the scheduled 30-bar rebuild.
    """
    if not _is_out_of_scope(close, pivots):
        return pivots

    _logger.warning(
        "Price %.4f is out of pivot scope [%.4f, %.4f] — triggering recalculation",
        close,
        pivots[0] * (1 - _OOS_BUFFER),
        pivots[-1] * (1 + _OOS_BUFFER),
    )

    if recalc_hook is not None:
        fresh = recalc_hook()
        if fresh and not _is_out_of_scope(close, fresh):
            _logger.info("Recalculation resolved out-of-scope price; using fresh pivots")
            return fresh
        if fresh:
            pivots = fresh

    _logger.warning(
        "Price %.4f still out of scope — extending pivot grid virtually at %.1f%% spacing",
        close,
        _VIRTUAL_STEP * 100,
    )
    return _extend_pivots(close, pivots)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def analyse(
    bar: OHLCVBar,
    status: MachineStatus,
    pivots: List[float],
    recent_bars: List[OHLCVBar],
    bookkeeper: Bookkeeper,
    recalc_hook: Callable[[], List[float]] | None = None,
) -> List[AnalystOrder]:
    """
    Evaluate the current bar against machine state and pivots; emit trading instructions.

    Args:
        bar:         The latest closed 1-minute bar.
        status:      Current machine state (read-only within analyse — coordinator
                     updates active_stop_price when orders are routed).
        pivots:      Current pivot list, sorted ascending. Immutable within the
                     30-bar window in which it was computed.
        recent_bars: The last VWAP_WINDOW_BARS bars (including `bar`), used to
                     compute the rolling VWAP in the IDLE branch.
        bookkeeper:  Access to current cash and shares held.

    Returns:
        A (possibly empty) list of AnalystOrder objects, to be processed in order
        by the Trader. An empty list means "do nothing this bar".
    """
    if not pivots:
        # TODO: pivot list empty — issue warning (analyst_logic.md Unresolved section)
        _logger.warning("Pivot list is empty — analyst returning no orders")
        return []

    pivots = _resolve_pivots(bar.close, pivots, recalc_hook)

    bar_time_est = bar.timestamp.astimezone(_EST).time()

    # -----------------------------------------------------------------------
    # Stop conditions — IDLE only (analyst_logic.md §Stop Conditions)
    # -----------------------------------------------------------------------
    if status.position == "IDLE":
        available = bookkeeper.available_cash()
        if available < status.daily_start_nav * (1 - DAILY_LOSS_LIMIT):
            return []
        if bar_time_est > STOP_TRADING_TIME:
            return []

    # -----------------------------------------------------------------------
    # IDLE branch
    # -----------------------------------------------------------------------
    if status.position == "IDLE":
        orders: List[AnalystOrder] = [AnalystOrder(type="CANCEL_ALL_BUYS")]

        vwap_value = _vwap(recent_bars)
        _, vwap_idx = _closest_pivot(vwap_value, pivots)
        _, close_idx = _closest_pivot(bar.close, pivots)

        if vwap_idx != close_idx:
            return orders  # [CANCEL_ALL_BUYS] only

        i = vwap_idx  # == close_idx; both agree on the same pivot

        if i == 0:
            # No pivot below for stop-loss anchor — prepend one virtual pivot at -1.5%
            # so the normal order-construction path below can proceed without guarding.
            pivots = [pivots[0] * (1 - _VIRTUAL_STEP)] + pivots
            i = 1  # original pivots[0] is now at index 1

        # UUID convention: analyst generates ID here; trader uses BUY_LIMIT.condition as
        # BrokerOrder.order_id so the paired stop-loss condition string matches at execution.
        order_uuid = str(uuid.uuid4())
        orders.append(AnalystOrder(
            type="BUY_LIMIT",
            price=min(pivots[i], bar.close) * (1 - BUY_LIMIT_OFFSET),
            size="FULL_AVAILABLE_CASH",
            condition=order_uuid,   # trader reads this as the intended BrokerOrder.order_id
        ))
        orders.append(AnalystOrder(
            type="SELL_STOP",
            price=_stoploss_price(pivots, i),
            size="ALL_SHARES",
            condition=f"on_fill:{order_uuid}",
        ))
        return orders

    # -----------------------------------------------------------------------
    # LONG branch
    # -----------------------------------------------------------------------

    # [Priority: Stop Condition Wins] Time-based exit short-circuits all trailing logic.
    # End-of-day check runs before watermark advance so a SELL_MARKET is never mixed
    # with an UPDATE_STOPLOSS in the same bar.
    if bar_time_est > STOP_TRADING_TIME:
        if bookkeeper.shares_held() > 0:
            return [AnalystOrder(type="SELL_MARKET", size="ALL_SHARES")]
        return []

    orders = []

    # [Single-position stop-loss guarantee] When LONG, every pending order is a SELL.
    # An empty pending_orders therefore means the stop-loss was removed unexpectedly.
    if not status.pending_orders:
        orders.extend(_emit_missing_stoploss(bar.close, pivots, bookkeeper))

    # Trailing stop-loss ratchet: advance only when close is now in a higher pivot band.
    # The stop is always midpoint(pivot[i-1], pivot[i]) where pivot[i] is the closest
    # pivot to close. It only moves up — never down (ratchet: candidate > active_stop_price).
    _, i = _closest_pivot(bar.close, pivots)
    if i == 0:
        _logger.warning(
            "Price %.4f at or below lowest pivot — cannot advance stop-loss", bar.close,
        )
        return orders

    candidate = _stoploss_price(pivots, i)
    if status.active_stop_price is not None and candidate > status.active_stop_price:
        orders.append(AnalystOrder(
            type="UPDATE_STOPLOSS",
            price=candidate,
        ))

    return orders

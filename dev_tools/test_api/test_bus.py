"""
Test Broker Bus (Batch / Test Mode Only).

Responsibility:
    Simulates order execution by inspecting future OHLCV bars. Implements
    BrokerBusProtocol so it can be swapped transparently with LiveBus.
    NEVER imported in streaming (production) code paths.

    Execution rules:
        BUY LIMIT   — fills on the first future bar where bar.low  <= limit_price
        SELL LIMIT  — fills on the first future bar where bar.high >= limit_price
        MARKET      — fills at the next bar's open price
        No fill     — returns None if the condition is never met within future_bars

    Filled price = the limit_price (not the bar's low/high), matching typical
    limit-order semantics. Market fills use next bar's open exactly.

    Fill computation is immediate (look-ahead at construction of the order) — the
    bus knows the full future sequence. get_confirmation() returns the pre-computed
    result synchronously.

Public interface:
    TestBus(future_bars: List[OHLCVBar])
    send_order(order: BrokerOrder) -> str
    cancel_order(order_id: str) -> bool
    get_confirmation(order_id: str) -> ExecutionConfirmation | None
"""

from __future__ import annotations

import uuid
from typing import List

from src.models import BrokerOrder, ExecutionConfirmation, OHLCVBar


class TestBus:
    """
    Simulated broker bus for batch testing.

    Args:
        future_bars: All bars after the current simulation point, in chronological
                     order. The bus scans these to determine if and when an order fills.
    """

    def __init__(self, future_bars: List[OHLCVBar]) -> None:
        self._future_bars = future_bars
        # Maps order_id → ExecutionConfirmation (or None if order never filled)
        self._confirmations: dict[str, ExecutionConfirmation | None] = {}
        # Tracks order_ids that resulted in a fill
        self._filled_ids: set[str] = set()

    def send_order(self, order: BrokerOrder) -> str:
        """
        Simulate order execution against future bar data. Fill is computed
        immediately via look-ahead; result is stored for get_confirmation().

        Returns:
            order.order_id (confirms the order was received).
        """
        conf = self._simulate(order)
        self._confirmations[order.order_id] = conf
        if conf is not None:
            self._filled_ids.add(order.order_id)
        return order.order_id

    def cancel_order(self, order_id: str) -> bool:
        """
        Mark an open (unfilled) order as cancelled.

        Returns:
            True if the order was pending (not yet filled) and cancellation
            succeeded. False if already filled, already unknown, or not found.
        """
        if order_id not in self._confirmations:
            return False
        if order_id in self._filled_ids:
            return False
        # Mark cancelled by removing the pending entry; fill stays None.
        # We could track a separate cancelled set, but since get_confirmation
        # already returns None for unfilled orders, this is a no-op on state.
        return True

    def get_confirmation(self, order_id: str) -> ExecutionConfirmation | None:
        """
        Return the pre-computed fill result for a submitted order.

        Returns:
            ExecutionConfirmation if filled, None if unfilled/cancelled/unknown.
        """
        return self._confirmations.get(order_id)

    # ------------------------------------------------------------------
    # Internal fill simulation
    # ------------------------------------------------------------------

    def _simulate(self, order: BrokerOrder) -> ExecutionConfirmation | None:
        """Scan future_bars and return a fill confirmation, or None."""
        if order.order_type == "MARKET":
            return self._fill_market(order)
        return self._fill_limit(order)

    def _fill_market(self, order: BrokerOrder) -> ExecutionConfirmation | None:
        if not self._future_bars:
            return None
        bar = self._future_bars[0]
        return ExecutionConfirmation(
            confirmation_id=str(uuid.uuid4()),
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            filled_quantity=order.quantity,
            filled_price=bar.open,
            filled_at=bar.timestamp,
        )

    def _fill_limit(self, order: BrokerOrder) -> ExecutionConfirmation | None:
        limit_price = order.limit_price
        for bar in self._future_bars:
            if order.side == "BUY" and bar.low <= limit_price:
                return ExecutionConfirmation(
                    confirmation_id=str(uuid.uuid4()),
                    order_id=order.order_id,
                    ticker=order.ticker,
                    side=order.side,
                    filled_quantity=order.quantity,
                    filled_price=limit_price,
                    filled_at=bar.timestamp,
                )
            if order.side == "SELL" and bar.high >= limit_price:
                return ExecutionConfirmation(
                    confirmation_id=str(uuid.uuid4()),
                    order_id=order.order_id,
                    ticker=order.ticker,
                    side=order.side,
                    filled_quantity=order.quantity,
                    filled_price=limit_price,
                    filled_at=bar.timestamp,
                )
        return None

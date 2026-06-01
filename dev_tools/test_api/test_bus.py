"""
Test Broker Bus (Batch / Test Mode Only).

Responsibility:
    Simulates order execution by inspecting future OHLCV bars. Implements
    BrokerBusProtocol so it can be swapped transparently with LiveBus.
    NEVER imported in streaming (production) code paths.

    Execution rules:
        BUY LIMIT   — fills on the first future bar where bar.low  <= limit_price
        SELL LIMIT  — fills on the first future bar where bar.high >= limit_price
        STOP LIMIT (sell stop-loss) — fills on the first future bar where bar.low <= limit_price
        MARKET      — fills at the next bar's open price
        No fill     — returns None if the condition is never met within future_bars

    Filled price = the limit_price (not the bar's low/high), matching typical
    limit-order semantics. Market fills use next bar's open exactly.

    Fill computation is immediate (look-ahead at construction of the order) — the
    bus knows the full future sequence. get_confirmation() returns the pre-computed
    result synchronously.

    current_bar_index: when passed to send_order(), the bus uses
        all_bars[current_bar_index + 1:] as the future bar slice. When omitted,
        all bars supplied at construction are treated as future (backward compat).

Public interface:
    TestBus(all_bars: List[OHLCVBar])
    send_order(order: BrokerOrder, current_bar_index: int | None = None) -> str
    cancel_order(order_id: str) -> bool
    get_confirmation(order_id: str) -> ExecutionConfirmation | None
"""

from __future__ import annotations

import uuid
from typing import Callable, List

from src.models import BrokerOrder, ExecutionConfirmation, OHLCVBar


class TestBus:
    """
    Simulated broker bus for batch testing.

    Args:
        all_bars: The complete bar sequence available for fill simulation.
                  When send_order() is called with current_bar_index=t, only
                  all_bars[t+1:] are considered future. Without current_bar_index,
                  all bars are treated as future (backward-compatible behaviour).
    """

    def __init__(self, all_bars: List[OHLCVBar]) -> None:
        self._all_bars = all_bars
        self._confirmations: dict[str, ExecutionConfirmation | None] = {}
        self._filled_ids: set[str] = set()
        self._confirmation_handler: Callable[[ExecutionConfirmation], None] | None = None

    def send_order(
        self,
        order: BrokerOrder,
        current_bar_index: int | None = None,
    ) -> str:
        """
        Simulate order execution against future bar data. Fill is computed
        immediately via look-ahead; result is stored for get_confirmation().

        Args:
            order:             The broker order to simulate.
            current_bar_index: Index of the bar being processed. Future bars are
                               all_bars[current_bar_index + 1:]. When None, all
                               bars supplied at construction are used as future.

        Returns:
            order.order_id (confirms the order was received).
        """
        if current_bar_index is None:
            future_bars = self._all_bars
        else:
            future_bars = self._all_bars[current_bar_index + 1:]

        conf = self._simulate(order, future_bars)
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

    def register_confirmation_handler(
        self, handler: Callable[[ExecutionConfirmation], None]
    ) -> None:
        """
        Store a callback for fill events. In batch mode the runner drives
        delivery timing by polling get_confirmation(); this stored handler
        mirrors what LiveBus would call on a broker push in streaming mode.
        """
        self._confirmation_handler = handler

    # ------------------------------------------------------------------
    # Internal fill simulation
    # ------------------------------------------------------------------

    def _simulate(
        self, order: BrokerOrder, future_bars: List[OHLCVBar]
    ) -> ExecutionConfirmation | None:
        """Scan future_bars and return a fill confirmation, or None."""
        if order.order_type == "MARKET":
            return self._fill_market(order, future_bars)
        if order.order_type == "STOP_LIMIT":
            return self._fill_stop(order, future_bars)
        return self._fill_limit(order, future_bars)

    def _fill_market(
        self, order: BrokerOrder, future_bars: List[OHLCVBar]
    ) -> ExecutionConfirmation | None:
        if not future_bars:
            return None
        bar = future_bars[0]
        return ExecutionConfirmation(
            confirmation_id=str(uuid.uuid4()),
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            filled_quantity=order.quantity,
            filled_price=bar.open,
            filled_at=bar.timestamp,
        )

    def _fill_stop(
        self, order: BrokerOrder, future_bars: List[OHLCVBar]
    ) -> ExecutionConfirmation | None:
        """Stop-loss fill: SELL triggers when bar.low drops to or below limit_price."""
        limit_price = order.limit_price
        for bar in future_bars:
            if order.side == "SELL" and bar.low <= limit_price:
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

    def _fill_limit(
        self, order: BrokerOrder, future_bars: List[OHLCVBar]
    ) -> ExecutionConfirmation | None:
        limit_price = order.limit_price
        for bar in future_bars:
            if order.side == "BUY" and bar.low <= limit_price:
                return ExecutionConfirmation(
                    confirmation_id=str(uuid.uuid4()),
                    order_id=order.order_id,
                    ticker=order.ticker,
                    side=order.side,
                    filled_quantity=order.quantity,
                    filled_price=min(limit_price, bar.close),
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
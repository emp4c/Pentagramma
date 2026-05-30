"""
Fake Broker Bus (Batch / Test Mode Only).

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

Public interface:
    FakeBus(future_bars: List[OHLCVBar])
    send_order(order: BrokerOrder) -> ExecutionConfirmation | None
    cancel_all_buys(pending_order_ids: list[str]) -> None

# REVIEW: todo.md refers to this module as dev_tools/fake_api/fake_bus.py, but
# the package folder created on disk is dev_tools/test_api/. Using test_api
# to match what exists. Update todo.md or rename the folder to reconcile.
"""

from __future__ import annotations

from typing import List

from src.models import BrokerOrder, ExecutionConfirmation, OHLCVBar
from src.bus.protocol import BrokerBusProtocol


class FakeBus:
    """
    Simulated broker bus for batch testing.

    Args:
        future_bars: All bars after the current simulation point, in chronological
                     order. The bus scans these to determine if and when an order fills.
    """

    def __init__(self, future_bars: List[OHLCVBar]) -> None:
        raise NotImplementedError

    def send_order(self, order: BrokerOrder) -> ExecutionConfirmation | None:
        """
        Simulate order execution against future bar data.

        Returns:
            ExecutionConfirmation if filled, None if never filled within future_bars.
        """
        raise NotImplementedError

    def cancel_all_buys(self, pending_order_ids: list[str]) -> None:
        """
        Mark all listed buy orders as cancelled. No-op for orders not tracked.
        """
        raise NotImplementedError

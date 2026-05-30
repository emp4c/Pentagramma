"""
Broker Bus Protocol.

Responsibility:
    Defines the common interface that both LiveBus and FakeBus must implement.
    Components (trader, batch_runner) depend only on this Protocol — never on
    a concrete bus implementation. This ensures streaming and batch modes are
    interchangeable at the bus boundary.

    LiveBus  (src/bus/live_bus.py)    — wraps real broker API (out of scope for now)
    FakeBus  (dev_tools/test_api/)    — simulates fills from future bar data

# REVIEW: In streaming mode, broker execution confirmations arrive asynchronously
# (the broker calls back after the order is routed). In batch/fake mode, the fill
# is returned synchronously. This Protocol uses a synchronous return signature
# (returning ExecutionConfirmation | None). For the LiveBus, async delivery will
# need a callback or queue mechanism not captured here — revisit when implementing
# live_bus.py.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.models import BrokerOrder, ExecutionConfirmation


@runtime_checkable
class BrokerBusProtocol(Protocol):
    """
    Common interface for all broker bus implementations.
    Both LiveBus and FakeBus must satisfy this Protocol.
    """

    def send_order(self, order: BrokerOrder) -> ExecutionConfirmation | None:
        """
        Submit a single order to the broker (real or simulated).

        Args:
            order: A fully constructed BrokerOrder from the Trader.

        Returns:
            ExecutionConfirmation if the order was filled (immediately or
            within the simulated window), or None if it expired unfilled.

        # REVIEW: For the LiveBus this will not return a confirmation directly —
        # real broker APIs are async. This signature works for FakeBus.
        # The live implementation will need an event/callback mechanism instead.
        """
        ...

    def cancel_all_buys(self, pending_order_ids: list[str]) -> None:
        """
        Request cancellation of all open buy orders.

        Args:
            pending_order_ids: List of broker order IDs currently awaiting fill.
        """
        ...

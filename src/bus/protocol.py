"""
Broker Bus Protocol.

Responsibility:
    Defines the common interface that both LiveBus and TestBus must implement.
    Components (trader, batch_runner) depend only on this Protocol — never on
    a concrete bus implementation. This ensures streaming and batch modes are
    interchangeable at the bus boundary.

    LiveBus  (src/bus/live_bus.py)    — wraps real broker API (out of scope for now)
    TestBus  (dev_tools/test_api/)    — simulates fills from future bar data

# REVIEW: In streaming mode, broker execution confirmations arrive asynchronously
# (the broker calls back after the order is routed). In batch/test mode, the fill
# is computed synchronously via look-ahead. This Protocol uses a synchronous
# get_confirmation() pattern. For the LiveBus, async delivery will need a callback
# or queue mechanism not captured here — revisit when implementing live_bus.py.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.models import BrokerOrder, ExecutionConfirmation


@runtime_checkable
class BrokerBusProtocol(Protocol):
    """
    Common interface for all broker bus implementations.
    Both LiveBus and TestBus must satisfy this Protocol.
    """

    def send_order(self, order: BrokerOrder) -> str:
        """
        Submit a single order to the broker (real or simulated).

        Returns:
            The order_id string (same as order.order_id), confirming receipt.
        """
        ...

    def cancel_order(self, order_id: str) -> bool:
        """
        Request cancellation of a single open order.

        Returns:
            True if the order was found and successfully cancelled (i.e. it had
            not yet been filled). False if the order is already filled, already
            cancelled, or unknown.
        """
        ...

    def get_confirmation(self, order_id: str) -> ExecutionConfirmation | None:
        """
        Retrieve the execution confirmation for a previously submitted order.

        Returns:
            ExecutionConfirmation if the order was filled, None if it expired
            unfilled, was cancelled, or is unknown.
        """
        ...

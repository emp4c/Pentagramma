"""
Broker Bus Protocol.

Responsibility:
    Defines the common interface that both LiveBus and TestBus must implement.
    Components (trader, batch_runner) depend only on this Protocol — never on
    a concrete bus implementation. This ensures streaming and batch modes are
    interchangeable at the bus boundary.

    LiveBus  (src/bus/live_bus.py)    — wraps real broker API (out of scope for now)
    TestBus  (dev_tools/test_api/)    — simulates fills from future bar data

    Confirmation delivery model:
        Streaming: the broker pushes ExecutionConfirmation asynchronously; the
        coordinator registers a handler via register_confirmation_handler() and
        the LiveBus calls it on every fill event.
        Batch/test: fills are pre-computed on send_order(); the batch runner
        simulates the push by polling get_confirmation() at each bar and calling
        the same handler function, preserving identical coordinator logic across
        both modes.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

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

    def register_confirmation_handler(
        self, handler: Callable[[ExecutionConfirmation], None]
    ) -> None:
        """
        Register a callback that the bus will invoke when an order fills.

        In LiveBus this is called by the broker's push mechanism.
        In TestBus this is stored but the batch runner drives delivery timing
        by polling get_confirmation() and calling the handler itself.
        """
        ...

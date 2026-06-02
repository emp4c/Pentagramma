"""
Live Broker Bus — Stub.

NOT IMPLEMENTED — integrate your broker API here.
Must implement BrokerBusProtocol (see src/bus/protocol.py).

All methods raise NotImplementedError. Replace each with a real broker API
call once a broker integration is chosen.
"""

from __future__ import annotations

from typing import Callable

from src.models import BrokerOrder, ExecutionConfirmation


class LiveBus:
    """
    NOT IMPLEMENTED — integrate your broker API here.
    Must implement BrokerBusProtocol (see src/bus/protocol.py).
    """

    def send_order(self, order: BrokerOrder) -> str:
        raise NotImplementedError("LiveBus.send_order: broker API not integrated")

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("LiveBus.cancel_order: broker API not integrated")

    def get_confirmation(self, order_id: str) -> ExecutionConfirmation | None:
        raise NotImplementedError("LiveBus.get_confirmation: broker API not integrated")

    def register_confirmation_handler(
        self, handler: Callable[[ExecutionConfirmation], None]
    ) -> None:
        raise NotImplementedError(
            "LiveBus.register_confirmation_handler: broker API not integrated"
        )
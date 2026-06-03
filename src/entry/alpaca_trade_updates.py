"""
Trade Update Stream — Alpaca WebSocket.

Subscribes to order fill events and updates the bookkeeper and MachineStatus
in real time, so the streaming coordinator stays consistent with broker state
without polling get_confirmation() on every bar.

Public interface:
    AlpacaTradeUpdateStream(bookkeeper, status) — construct once per session
    AlpacaTradeUpdateStream.run()               — blocks; runs its own event loop
"""

from __future__ import annotations

import logging

from alpaca.trading.stream import TradingStream
from alpaca.trading.models import TradeUpdate

from src.bookkeeper.bookkeeper import Bookkeeper
from src.config_env import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER
from src.models import ExecutionConfirmation

_logger = logging.getLogger(__name__)


class AlpacaTradeUpdateStream:
    def __init__(self, bookkeeper: Bookkeeper, status) -> None:
        # status is MachineStatus — passed by reference so mutations are visible to coordinator
        self.bookkeeper = bookkeeper
        self.status = status
        self.stream = TradingStream(ALPACA_API_KEY, ALPACA_API_SECRET, paper=ALPACA_PAPER)
        self.stream.subscribe_trade_updates(self._on_trade_update)

    async def _on_trade_update(self, update: TradeUpdate) -> None:
        event = update.event  # "fill", "partial_fill", "canceled", "rejected", "expired", …

        if event in ("fill", "partial_fill"):
            order = update.order
            conf = ExecutionConfirmation(
                confirmation_id=str(order.id) + "_" + event,
                order_id=str(order.id),
                ticker=order.symbol,
                side="BUY" if order.side.value == "buy" else "SELL",
                filled_quantity=float(order.filled_qty),
                filled_price=float(order.filled_avg_price),
                filled_at=order.filled_at,
            )
            self.bookkeeper.receive_confirmation(conf)
            _logger.info(
                "Trade update — %s %s: %.4f shares @ %.4f (order_id=%s)",
                event, conf.side, conf.filled_quantity, conf.filled_price, conf.order_id,
            )

            if event == "fill":
                self.status.pending_orders.pop(str(order.id), None)
                if order.side.value == "sell":
                    self.status.position = "IDLE"
                    self.status.active_stop_price = None
                elif order.side.value == "buy":
                    self.status.position = "LONG"

        elif event in ("canceled", "rejected", "expired"):
            order_id = str(update.order.id)
            _logger.warning("Order %s event: %s", order_id, event)
            self.status.pending_orders.pop(order_id, None)

    def run(self) -> None:
        self.stream.run()
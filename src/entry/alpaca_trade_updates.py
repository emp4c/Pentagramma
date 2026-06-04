"""
Trade Update Stream — Alpaca WebSocket.

Subscribes to order fill events and routes them through TradingSession so that
the full fill-handling path runs (bookkeeper update, position transition, and
conditional stop-loss activation on BUY fills).

Public interface:
    AlpacaTradeUpdateStream(session) — construct once per session
    AlpacaTradeUpdateStream.run()    — blocks; runs its own event loop
"""

from __future__ import annotations

import logging

from alpaca.trading.stream import TradingStream
from alpaca.trading.models import TradeUpdate

from src.config_env import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER
from src.models import ExecutionConfirmation

_logger = logging.getLogger(__name__)


class AlpacaTradeUpdateStream:
    def __init__(self, session) -> None:
        # session is TradingSession — imported at runtime to avoid circular import
        self.session = session
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

            if event == "fill":
                # Route through session so _handle_confirmation runs:
                # bookkeeper update + position transition + stop-loss activation.
                self.session.notify_fill(conf)
            else:
                # partial_fill: stage the cumulative fill so notify_cancel can commit
                # it if the order is later canceled before a full fill arrives.
                self.session.stage_partial_fill(conf)

        elif event in ("canceled", "rejected", "expired"):
            order_id = str(update.order.id)
            _logger.warning("Order %s event: %s", order_id, event)
            # Delegates all state updates (pending_orders cleanup, partial-fill commit,
            # stop-loss emission) to notify_cancel so the logic stays in one place.
            self.session.notify_cancel(order_id)

    def run(self) -> None:
        self.stream.run()
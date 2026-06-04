"""
Trade Update Stream — Alpaca WebSocket.

Subscribes to order fill events and routes them through TradingSession so that
the full fill-handling path runs (bookkeeper update, position transition, and
conditional stop-loss activation on BUY fills).

Public interface:
    AlpacaTradeUpdateStream(session) — construct once per session
    AlpacaTradeUpdateStream.run()    — blocks; reconnects automatically on failure
"""

from __future__ import annotations

import logging
import time

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
            side = "BUY" if order.side.value == "buy" else "SELL"
            conf = ExecutionConfirmation(
                confirmation_id=str(order.id) + "_" + event,
                order_id=str(order.id),
                ticker=order.symbol,
                side=side,
                filled_quantity=float(order.filled_qty),
                filled_price=float(order.filled_avg_price),
                filled_at=order.filled_at,
            )

            if event == "fill":
                # Route through session so _handle_confirmation runs:
                # bookkeeper update + position transition + stop-loss activation.
                self.session.notify_fill(conf)
            elif side == "BUY":
                # partial_fill BUY: commit filled shares, cancel remainder, go LONG with stop.
                self.session.handle_buy_partial_fill(conf)
            else:
                # partial_fill SELL: machine stays LONG until the full fill arrives.
                # The remainder continues as an open order at Alpaca — no state change needed.
                _logger.info(
                    "Trade update — partial_fill SELL: %.4f shares @ %.4f (order_id=%s) "
                    "— remainder stays open, position remains LONG",
                    conf.filled_quantity, conf.filled_price, conf.order_id,
                )

        elif event in ("canceled", "rejected", "expired"):
            order_id = str(update.order.id)
            _logger.warning("Order %s event: %s", order_id, event)
            # Delegates all state updates (pending_orders cleanup, partial-fill commit,
            # stop-loss emission) to notify_cancel so the logic stays in one place.
            self.session.notify_cancel(order_id)

    def _rebuild_client(self) -> None:
        self.stream = TradingStream(ALPACA_API_KEY, ALPACA_API_SECRET, paper=ALPACA_PAPER)
        self.stream.subscribe_trade_updates(self._on_trade_update)

    def run(self) -> None:
        backoff = 5
        while True:
            try:
                _logger.info("%s: connecting...", self.__class__.__name__)
                self.stream.run()
                _logger.warning(
                    "%s: stream ended — reconnecting in %ds",
                    self.__class__.__name__, backoff,
                )
            except Exception as e:
                _logger.error(
                    "%s: error %s — reconnecting in %ds",
                    self.__class__.__name__, e, backoff,
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            self._rebuild_client()
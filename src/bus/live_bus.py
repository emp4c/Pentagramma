"""
Live Broker Bus — Alpaca implementation.

Implements BrokerBusProtocol via Alpaca's TradingClient (REST).
Paper vs live trading is controlled by ALPACA_PAPER in .env.

Confirmation delivery model:
    The broker pushes ExecutionConfirmation asynchronously via the handler
    registered with register_confirmation_handler(). LiveBus does not poll;
    the stream_entry coordinator drives that via a websocket or webhook
    (not implemented here — stubs left for integration).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)

from src.config_env import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER
from src.models import BrokerOrder, ExecutionConfirmation

logger = logging.getLogger(__name__)


def _with_retry(fn, *args, max_attempts: int = 3, backoff: float = 1.0, **kwargs):
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            sleep_for = backoff * (attempt + 1)
            logger.warning(f"Alpaca API retry {attempt + 1}: {e} — retrying in {sleep_for}s")
            time.sleep(sleep_for)


class LiveBus:
    """
    Alpaca broker bus.  Implements BrokerBusProtocol.

    Attributes:
        paper  — True when connected to Alpaca paper trading endpoint.
        client — Underlying TradingClient (exposed for smoke tests / reconciliation).
    """

    def __init__(self) -> None:
        self.paper = ALPACA_PAPER
        self.client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_API_SECRET,
            paper=ALPACA_PAPER,
        )
        self._confirmation_handler: Callable[[ExecutionConfirmation], None] | None = None

    # ------------------------------------------------------------------
    # BrokerBusProtocol implementation
    # ------------------------------------------------------------------

    def send_order(self, order: BrokerOrder) -> str:
        """
        Submit an order to Alpaca and return the Alpaca order UUID.

        The returned UUID must be stored by the coordinator as the broker order ID
        so that confirmations can be matched via get_confirmation().
        """
        alpaca_order = _with_retry(self._submit_order, order)
        return str(alpaca_order.id)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.  Returns False if already filled or unknown."""
        try:
            _with_retry(self.client.cancel_order_by_id, order_id)
            return True
        except Exception as e:
            # Alpaca returns 422 when the order cannot be cancelled (filled/unknown)
            logger.warning(f"cancel_order({order_id}): {e}")
            return False

    def get_confirmation(self, order_id: str) -> ExecutionConfirmation | None:
        """
        Poll Alpaca for order status.

        Returns ExecutionConfirmation when filled (or partially filled),
        None when still pending or when the order ended without a fill.
        """
        try:
            alpaca_order = _with_retry(self.client.get_order_by_id, order_id)
        except Exception as e:
            logger.warning(f"get_confirmation({order_id}): {e}")
            return None

        status = str(alpaca_order.status).lower()

        if status in ("filled", "partially_filled"):
            return ExecutionConfirmation(
                confirmation_id=str(alpaca_order.id),
                order_id=order_id,
                ticker=str(alpaca_order.symbol),
                side="BUY" if str(alpaca_order.side).upper() == "BUY" else "SELL",
                filled_quantity=float(alpaca_order.filled_qty),
                filled_price=float(alpaca_order.filled_avg_price),
                filled_at=alpaca_order.filled_at,
            )

        if status in ("canceled", "expired", "rejected"):
            logger.warning(f"Order {order_id} ended with status '{status}' — no fill")
            return None

        # "new", "accepted", "pending_new", "held", etc. — not yet filled
        return None

    def register_confirmation_handler(
        self, handler: Callable[[ExecutionConfirmation], None]
    ) -> None:
        """
        Store the handler to be called when a fill event arrives.

        In a full integration this would be wired to an Alpaca websocket stream
        that pushes trade-update events.  For now the coordinator may also poll
        get_confirmation() per bar and call the handler itself.
        """
        self._confirmation_handler = handler

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _submit_order(self, order: BrokerOrder):
        """Translate a BrokerOrder into an Alpaca request and submit it."""
        otype = order.order_type
        side = order.side
        limit_price = round(order.limit_price, 2) if order.limit_price is not None else None

        if otype == "CANCEL":
            self.client.cancel_order_by_id(order.order_id)
            return _FakeAlpacaOrder(order.order_id)

        if otype == "LIMIT" and side == "BUY":
            req = LimitOrderRequest(
                symbol=order.ticker,
                qty=order.quantity,
                side=OrderSide.BUY,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
            )

        elif otype == "LIMIT" and side == "SELL":
            # GTC so the stop-loss survives beyond the current bar
            req = LimitOrderRequest(
                symbol=order.ticker,
                qty=order.quantity,
                side=OrderSide.SELL,
                limit_price=limit_price,
                time_in_force=TimeInForce.GTC,
            )

        elif otype == "STOP_LIMIT" and side == "SELL":
            # stop_price sits 0.1 % above limit_price so the order triggers before
            # the limit is hit — prevents the limit from never being reached on a gap
            stop_price = round(limit_price * 1.001, 2)
            req = StopLimitOrderRequest(
                symbol=order.ticker,
                qty=order.quantity,
                side=OrderSide.SELL,
                stop_price=stop_price,
                limit_price=limit_price,
                time_in_force=TimeInForce.GTC,
            )

        elif otype == "MARKET":
            req = MarketOrderRequest(
                symbol=order.ticker,
                qty=order.quantity,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )

        else:
            raise ValueError(f"Unsupported order type/side combination: {otype}/{side}")

        return self.client.submit_order(order_data=req)


class _FakeAlpacaOrder:
    """Minimal stand-in returned by CANCEL paths so send_order() has a .id."""

    def __init__(self, order_id: str) -> None:
        self.id = order_id
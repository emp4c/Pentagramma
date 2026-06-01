"""
Trader.

Responsibility:
    Translates AnalystOrder instructions into concrete BrokerOrder objects.
    Stateless — does not execute orders itself, only constructs and returns them.
    Handles share-quantity sizing: each buy order uses all available cash
    minus MIN_CASH_RESERVE. Passes the resulting BrokerOrder list to the
    caller (stream_entry or batch_runner), which forwards them to the broker bus.

    CANCEL orders (order_type="CANCEL") are returned in-band. The caller must
    route them to bus.cancel_order(order.order_id) rather than bus.send_order().

Public interface:
    process(orders, status, bookkeeper, ticker) -> List[BrokerOrder]
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from src.bookkeeper.bookkeeper import Bookkeeper
from src.models import AnalystOrder, BrokerOrder, MachineStatus

_logger = logging.getLogger(__name__)

# DESIGN: MachineStatus.pending_orders is Dict[str, Literal["BUY","SELL"]] (order_id → side)
# rather than the original List[str]. This lets the trader filter by side for
# CANCEL_ALL_BUYS (cancel only BUY orders) and locate the active stop-loss for
# UPDATE_STOPLOSS (find the SELL order), without relying on the implicit positional
# contract that "IDLE → all pending are BUYs, LONG → all pending are SELLs".
# That contract holds today but the trader should not encode it; an explicit mapping
# keeps each component's logic self-contained.


def process(
    orders: List[AnalystOrder],
    status: MachineStatus,
    bookkeeper: Bookkeeper,
    ticker: str,
) -> List[BrokerOrder]:
    """
    Translate a list of AnalystOrders into BrokerOrders ready for the bus.

    CANCEL orders (order_type="CANCEL") must be routed by the caller to
    bus.cancel_order(order.order_id), not bus.send_order().

    Translation rules:
        CANCEL_ALL_BUYS   → one CANCEL BrokerOrder per pending BUY order_id
        BUY_LIMIT         → LIMIT buy; quantity = floor(available_cash() / price)
        SELL_STOP         → STOP_LIMIT sell (stop-loss); quantity = shares_held(); condition passed through
        SELL_MARKET       → MARKET sell; quantity = shares_held()
        UPDATE_STOPLOSS   → CANCEL the existing SELL + fresh STOP_LIMIT sell at new price
    """
    result: List[BrokerOrder] = []
    now = datetime.now(timezone.utc)

    for order in orders:
        if order.type == "CANCEL_ALL_BUYS":
            for oid, side in status.pending_orders.items():
                if side == "BUY":
                    result.append(BrokerOrder(
                        order_id=oid,
                        ticker=ticker,
                        side="BUY",
                        order_type="CANCEL",
                        limit_price=None,
                        quantity=0.0,
                        condition=None,
                        created_at=now,
                    ))

        elif order.type == "BUY_LIMIT":
            cash = bookkeeper.available_cash()
            if cash == 0.0:
                _logger.warning(
                    "BUY_LIMIT skipped: available_cash is 0 (ticker=%s)", ticker
                )
                continue
            quantity = int(cash / order.price)
            if quantity < 1:
                _logger.warning(
                    "BUY_LIMIT skipped: quantity %d < 1 (cash=%.2f, price=%.4f, ticker=%s)",
                    quantity, cash, order.price, ticker,
                )
                continue
            # UUID convention: analyst stores the intended BrokerOrder.order_id in
            # BUY_LIMIT.condition so the paired stop-loss "on_fill:<id>" resolves correctly.
            order_id = order.condition if order.condition else str(uuid.uuid4())
            result.append(BrokerOrder(
                order_id=order_id,
                ticker=ticker,
                side="BUY",
                order_type="LIMIT",
                limit_price=order.price,
                quantity=float(quantity),
                condition=None,
                created_at=now,
            ))

        elif order.type == "SELL_STOP":
            shares = bookkeeper.shares_held()
            if shares == 0.0:
                _logger.warning(
                    "SELL_STOP skipped: shares_held is 0 (ticker=%s)", ticker
                )
                continue
            result.append(BrokerOrder(
                order_id=str(uuid.uuid4()),
                ticker=ticker,
                side="SELL",
                order_type="STOP_LIMIT",
                limit_price=order.price,
                quantity=shares,
                condition=order.condition,
                created_at=now,
            ))

        elif order.type == "SELL_MARKET":
            shares = bookkeeper.shares_held()
            if shares == 0.0:
                _logger.warning(
                    "SELL_MARKET skipped: shares_held is 0 (ticker=%s)", ticker
                )
                continue
            result.append(BrokerOrder(
                order_id=str(uuid.uuid4()),
                ticker=ticker,
                side="SELL",
                order_type="MARKET",
                limit_price=None,
                quantity=shares,
                condition=None,
                created_at=now,
            ))

        elif order.type == "UPDATE_STOPLOSS":
            old_sl_id = next(
                (oid for oid, side in status.pending_orders.items() if side == "SELL"),
                None,
            )
            if old_sl_id is None:
                _logger.warning(
                    "UPDATE_STOPLOSS: no SELL order in pending_orders — issuing fresh stop-loss "
                    "(ticker=%s)", ticker
                )
            else:
                result.append(BrokerOrder(
                    order_id=old_sl_id,
                    ticker=ticker,
                    side="SELL",
                    order_type="CANCEL",
                    limit_price=None,
                    quantity=0.0,
                    condition=None,
                    created_at=now,
                ))

            shares = bookkeeper.shares_held()
            if shares == 0.0:
                _logger.warning(
                    "UPDATE_STOPLOSS: shares_held is 0 — stop-loss not issued (ticker=%s)", ticker
                )
                continue
            result.append(BrokerOrder(
                order_id=str(uuid.uuid4()),
                ticker=ticker,
                side="SELL",
                order_type="STOP_LIMIT",
                limit_price=order.price,
                quantity=shares,
                condition=None,
                created_at=now,
            ))

    return result

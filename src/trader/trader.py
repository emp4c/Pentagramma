"""
Trader.

Responsibility:
    Translates AnalystOrder instructions into concrete BrokerOrder objects.
    Stateless — does not execute orders itself, only constructs and returns them.
    Handles share-quantity sizing: each buy order uses all available cash
    minus MIN_CASH_RESERVE. Passes the resulting BrokerOrder list to the
    caller (stream_entry or batch_runner), which forwards them to the broker bus.

Public interface:
    process(orders, status, bookkeeper) -> List[BrokerOrder]
"""

from __future__ import annotations

from typing import List

from src.models import AnalystOrder, BrokerOrder, MachineStatus


def process(
    orders: List[AnalystOrder],
    status: MachineStatus,
    bookkeeper,                          # type: src.bookkeeper.bookkeeper.Bookkeeper
) -> List[BrokerOrder]:
    """
    Translate a list of AnalystOrders into BrokerOrders ready for the bus.

    Args:
        orders:      Instructions from the analyst. Processed in list order.
        status:      Current machine state (may be updated in place for watermark).
        bookkeeper:  Used to query available_cash() when sizing buy orders.

    Returns:
        A list of BrokerOrder objects. May be empty if orders is empty or all
        orders are informational (e.g. watermark updates only).

    Translation rules:
        CANCEL_ALL_BUYS   → broker cancel instruction for all open buy orders
        BUY_LIMIT         → LIMIT buy; quantity = bookkeeper.available_cash() / limit_price
        SELL_LIMIT        → LIMIT sell; quantity = bookkeeper.shares_held()
        SELL_MARKET       → MARKET sell; quantity = bookkeeper.shares_held()
        UPDATE_STOPLOSS   → amend/replace existing stop-loss order at broker

    Each BrokerOrder gets a fresh UUID as order_id.
    """
    raise NotImplementedError

"""
Unit tests for TradingSession (src/entry/stream_entry.py).

Covers:
    - UUID remapping: BUY fills correctly when broker returns different UUID
    - Stop-loss activation uses correct fill quantity (not round number)
    - push_confirmations=True skips REST polling entirely
    - notify_fill() with unknown order_id logs warning, does not raise
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from src.entry.stream_entry import TradingSession
from src.models import AnalystOrder, BrokerOrder, ExecutionConfirmation, OHLCVBar


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERNAL_BUY_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_BROKER_BUY_ID   = "bbbbbbbb-0000-0000-0000-000000000002"
_BUY_PRICE  = 100.0
_STOP_PRICE = 99.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar() -> OHLCVBar:
    return OHLCVBar(
        ticker="TEST",
        timestamp=datetime(2026, 1, 2, 15, 0, 0, tzinfo=timezone.utc),
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.0,
        volume=1000.0,
    )


def _buy_conf(order_id: str, quantity: float = 10.0) -> ExecutionConfirmation:
    return ExecutionConfirmation(
        confirmation_id=str(uuid.uuid4()),
        order_id=order_id,
        ticker="TEST",
        side="BUY",
        filled_quantity=quantity,
        filled_price=_BUY_PRICE,
        filled_at=datetime(2026, 1, 2, 15, 1, 0, tzinfo=timezone.utc),
    )


def _analyst_buy_orders() -> tuple:
    """Analyst response: BUY_LIMIT with paired warehoused SELL_STOP."""
    return (
        [
            AnalystOrder(
                type="BUY_LIMIT",
                price=_BUY_PRICE,
                size="FULL_AVAILABLE_CASH",
                condition=_INTERNAL_BUY_ID,   # trader uses this as BrokerOrder.order_id
            ),
            AnalystOrder(
                type="SELL_STOP",
                price=_STOP_PRICE,
                condition=f"on_fill:{_INTERNAL_BUY_ID}",
            ),
        ],
        [],  # returned pivot list (unused by these tests)
    )


# ---------------------------------------------------------------------------
# MockBus
# ---------------------------------------------------------------------------

class MockBus:
    """
    Broker bus stub.

    send_order() returns _BROKER_BUY_ID for BUY orders (simulating Alpaca's UUID
    remapping) and a fresh UUID for every SELL order.
    get_confirmation() answers only with broker-assigned UUIDs — internal UUIDs
    return None, mirroring the live behaviour that the fix must handle.
    """

    def __init__(self) -> None:
        self.sent_orders: List[BrokerOrder] = []
        self._confirmations: dict = {}

        self.send_order = MagicMock(side_effect=self._send_order)
        self.get_confirmation = MagicMock(side_effect=self._get_confirmation)
        self.cancel_order = MagicMock(return_value=True)
        self.register_confirmation_handler = MagicMock()

    def _send_order(self, order: BrokerOrder) -> str:
        self.sent_orders.append(order)
        return _BROKER_BUY_ID if order.side == "BUY" else str(uuid.uuid4())

    def _get_confirmation(self, order_id: str) -> ExecutionConfirmation | None:
        return self._confirmations.get(order_id)

    def set_fill(self, broker_id: str, conf: ExecutionConfirmation) -> None:
        self._confirmations[broker_id] = conf


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def _make_session(bus: MockBus, push: bool = False) -> TradingSession:
    with patch("src.entry.stream_entry._db.fetch_bars", return_value=[]):
        return TradingSession(
            "TEST", 10_000.0, bus, db_path=":memory:", push_confirmations=push
        )


# ---------------------------------------------------------------------------
# Test 1 — UUID remapping
# ---------------------------------------------------------------------------

class TestUUIDRemapping:
    def test_buy_fills_correctly_with_broker_uuid(self) -> None:
        """
        After a bar that triggers a BUY:
          - bus.send_order returns broker_uuid != internal_uuid
          - bus.get_confirmation(broker_uuid) returns the fill
          - bus.get_confirmation(internal_uuid) returns None
        Expected: position transitions to LONG and stop-loss is dispatched.
        """
        bus = MockBus()
        bus.set_fill(_BROKER_BUY_ID, _buy_conf(_BROKER_BUY_ID))

        session = _make_session(bus)

        with (
            patch("src.entry.stream_entry.write_bar"),
            patch("src.entry.stream_entry._db.fetch_bars", return_value=[]),
            patch("src.entry.stream_entry.analyse", return_value=_analyst_buy_orders()),
        ):
            session.on_bar(_make_bar())

        assert session._status.position == "LONG"
        # BUY order + activated stop-loss
        assert bus.send_order.call_count == 2

    def test_no_false_fill_via_internal_uuid(self) -> None:
        """
        If the fill is registered under the internal UUID (not broker UUID),
        the session must NOT detect it — confirming the bus uses broker IDs only.
        """
        bus = MockBus()
        bus.set_fill(_INTERNAL_BUY_ID, _buy_conf(_INTERNAL_BUY_ID))

        session = _make_session(bus)

        with (
            patch("src.entry.stream_entry.write_bar"),
            patch("src.entry.stream_entry._db.fetch_bars", return_value=[]),
            patch("src.entry.stream_entry.analyse", return_value=_analyst_buy_orders()),
        ):
            session.on_bar(_make_bar())

        assert session._status.position == "IDLE"


# ---------------------------------------------------------------------------
# Test 2 — Stop-loss quantity
# ---------------------------------------------------------------------------

class TestStopLossQuantity:
    def test_stop_loss_uses_filled_quantity(self) -> None:
        """
        The stop-loss BrokerOrder quantity must match the actual BUY fill quantity,
        not the trader's computed quantity.
        """
        bus = MockBus()
        odd_qty = 47.3
        bus.set_fill(_BROKER_BUY_ID, _buy_conf(_BROKER_BUY_ID, quantity=odd_qty))

        session = _make_session(bus)

        with (
            patch("src.entry.stream_entry.write_bar"),
            patch("src.entry.stream_entry._db.fetch_bars", return_value=[]),
            patch("src.entry.stream_entry.analyse", return_value=_analyst_buy_orders()),
        ):
            session.on_bar(_make_bar())

        stop_order: BrokerOrder = bus.sent_orders[1]
        assert stop_order.side == "SELL"
        assert stop_order.order_type == "STOP_LIMIT"
        assert stop_order.quantity == pytest.approx(odd_qty)


# ---------------------------------------------------------------------------
# Test 3 — push_confirmations skips REST polling
# ---------------------------------------------------------------------------

class TestPushConfirmations:
    def test_get_confirmation_never_called_when_push_enabled(self) -> None:
        """
        With push_confirmations=True, _process_confirmations() must return
        immediately without calling bus.get_confirmation().
        """
        bus = MockBus()
        session = _make_session(bus, push=True)

        with (
            patch("src.entry.stream_entry.write_bar"),
            patch("src.entry.stream_entry._db.fetch_bars", return_value=[]),
            patch("src.entry.stream_entry.analyse", return_value=([], [])),
        ):
            session.on_bar(_make_bar())

        bus.get_confirmation.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — notify_fill with unknown order_id
# ---------------------------------------------------------------------------

class TestNotifyFill:
    def test_unknown_order_id_logs_warning_no_exception(self, caplog) -> None:
        """notify_fill() with an untracked order_id must log a warning and not raise."""
        bus = MockBus()
        session = _make_session(bus)

        unknown_conf = ExecutionConfirmation(
            confirmation_id=str(uuid.uuid4()),
            order_id="unknown-order-id",
            ticker="TEST",
            side="BUY",
            filled_quantity=1.0,
            filled_price=100.0,
            filled_at=datetime(2026, 1, 2, 15, 0, 0, tzinfo=timezone.utc),
        )

        with caplog.at_level(logging.WARNING, logger="src.entry.stream_entry"):
            session.notify_fill(unknown_conf)  # must not raise

        assert any("unknown order_id" in r.message for r in caplog.records)
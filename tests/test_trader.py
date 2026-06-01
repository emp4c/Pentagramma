"""
Tests for src/trader/trader.py.

Shared helpers:
  _bk_with_cash(available)  — Bookkeeper whose available_cash() == available
  _bk_with_shares(qty, price) — Bookkeeper that has bought qty shares at price
  _make_status(...)          — minimal MachineStatus constructor
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest

from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import MIN_CASH_RESERVE
from src.models import AnalystOrder, ExecutionConfirmation, MachineStatus
from src.trader.trader import process

_TS = datetime(2026, 1, 15, 19, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_status(
    position: str = "IDLE",
    watermark_level: int | None = None,
    pending_orders: dict | None = None,
) -> MachineStatus:
    return MachineStatus(
        position=position,
        watermark_level=watermark_level,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_orders=pending_orders if pending_orders is not None else {},
        bar_count=0,
    )


def _bk_with_cash(available: float) -> Bookkeeper:
    """Bookkeeper whose available_cash() == available."""
    return Bookkeeper(initial_cash=available + MIN_CASH_RESERVE)


def _bk_with_shares(qty: float = 10.0, price: float = 100.0) -> Bookkeeper:
    """Bookkeeper that purchased qty shares at price; has a small cash balance left."""
    cost = qty * price
    bk = Bookkeeper(initial_cash=cost + MIN_CASH_RESERVE + 500.0)
    bk.receive_confirmation(ExecutionConfirmation(
        confirmation_id="c-setup",
        order_id="o-setup",
        ticker="TEST",
        side="BUY",
        filled_quantity=qty,
        filled_price=price,
        filled_at=_TS,
    ))
    return bk


# ---------------------------------------------------------------------------
# BUY_LIMIT
# ---------------------------------------------------------------------------

def test_buy_limit_quantity_computed_correctly() -> None:
    """quantity = floor(available_cash / price): 1000 / 50 == 20."""
    bk = _bk_with_cash(1_000.0)
    result = process(
        [AnalystOrder(type="BUY_LIMIT", price=50.0, condition="test-uuid")],
        _make_status(),
        bk,
        "TEST",
    )
    assert len(result) == 1
    assert result[0].quantity == 20.0
    assert result[0].side == "BUY"
    assert result[0].order_type == "LIMIT"
    assert result[0].limit_price == 50.0
    assert result[0].ticker == "TEST"


def test_buy_limit_uses_condition_as_order_id() -> None:
    """Analyst puts the intended order_id in BUY_LIMIT.condition; trader honours it."""
    bk = _bk_with_cash(1_000.0)
    result = process(
        [AnalystOrder(type="BUY_LIMIT", price=50.0, condition="intended-id")],
        _make_status(),
        bk,
        "TEST",
    )
    assert result[0].order_id == "intended-id"


def test_buy_limit_generates_uuid_when_no_condition() -> None:
    """BUY_LIMIT with no condition → trader generates a UUID order_id."""
    bk = _bk_with_cash(1_000.0)
    result = process(
        [AnalystOrder(type="BUY_LIMIT", price=50.0)],
        _make_status(),
        bk,
        "TEST",
    )
    assert len(result) == 1
    assert result[0].order_id  # non-empty string


def test_buy_limit_zero_cash_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """available_cash == 0 → order skipped, warning logged."""
    bk = _bk_with_cash(0.0)
    with caplog.at_level(logging.WARNING, logger="src.trader.trader"):
        result = process(
            [AnalystOrder(type="BUY_LIMIT", price=50.0)],
            _make_status(),
            bk,
            "TEST",
        )
    assert result == []
    assert any("BUY_LIMIT" in r.message for r in caplog.records)


def test_buy_limit_quantity_rounded_down() -> None:
    """199.99 / 100 = 1.9999 → quantity = 1, not 2 (floor, not round)."""
    bk = _bk_with_cash(199.99)
    result = process(
        [AnalystOrder(type="BUY_LIMIT", price=100.0)],
        _make_status(),
        bk,
        "TEST",
    )
    assert len(result) == 1
    assert result[0].quantity == 1.0


def test_buy_limit_quantity_less_than_one_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """available_cash < price → quantity == 0 < 1 → order skipped, warning logged."""
    bk = _bk_with_cash(49.99)
    with caplog.at_level(logging.WARNING, logger="src.trader.trader"):
        result = process(
            [AnalystOrder(type="BUY_LIMIT", price=50.0)],
            _make_status(),
            bk,
            "TEST",
        )
    assert result == []
    assert any("BUY_LIMIT" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SELL_LIMIT
# ---------------------------------------------------------------------------

def test_sell_limit_quantity_from_shares() -> None:
    """quantity = shares_held(); condition string passed through to BrokerOrder."""
    bk = _bk_with_shares(qty=10.0, price=100.0)
    result = process(
        [AnalystOrder(type="SELL_LIMIT", price=95.0, size="ALL_SHARES",
                      condition="on_fill:uuid-1")],
        _make_status("LONG", watermark_level=1),
        bk,
        "TEST",
    )
    assert len(result) == 1
    assert result[0].side == "SELL"
    assert result[0].order_type == "LIMIT"
    assert result[0].quantity == 10.0
    assert result[0].limit_price == 95.0
    assert result[0].condition == "on_fill:uuid-1"


def test_sell_limit_zero_shares_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """shares_held == 0 → order skipped, warning logged."""
    bk = _bk_with_cash(1_000.0)  # no shares
    with caplog.at_level(logging.WARNING, logger="src.trader.trader"):
        result = process(
            [AnalystOrder(type="SELL_LIMIT", price=95.0)],
            _make_status(),
            bk,
            "TEST",
        )
    assert result == []
    assert any("SELL_LIMIT" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SELL_MARKET
# ---------------------------------------------------------------------------

def test_sell_market_produces_market_order() -> None:
    """SELL_MARKET → MARKET order with no limit_price, quantity = shares_held."""
    bk = _bk_with_shares(qty=10.0, price=100.0)
    result = process(
        [AnalystOrder(type="SELL_MARKET", size="ALL_SHARES")],
        _make_status("LONG", watermark_level=1),
        bk,
        "TEST",
    )
    assert len(result) == 1
    assert result[0].order_type == "MARKET"
    assert result[0].side == "SELL"
    assert result[0].limit_price is None
    assert result[0].quantity == 10.0


def test_sell_market_zero_shares_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """shares_held == 0 → order skipped, warning logged."""
    bk = _bk_with_cash(1_000.0)
    with caplog.at_level(logging.WARNING, logger="src.trader.trader"):
        result = process(
            [AnalystOrder(type="SELL_MARKET")],
            _make_status(),
            bk,
            "TEST",
        )
    assert result == []
    assert any("SELL_MARKET" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CANCEL_ALL_BUYS
# ---------------------------------------------------------------------------

def test_cancel_all_buys_cancels_only_buy_ids() -> None:
    """Only BUY entries in pending_orders produce CANCEL orders; SELL entries are left alone."""
    status = _make_status(
        "IDLE",
        pending_orders={"buy-id-1": "BUY", "buy-id-2": "BUY", "sell-id": "SELL"},
    )
    bk = _bk_with_cash(1_000.0)
    result = process([AnalystOrder(type="CANCEL_ALL_BUYS")], status, bk, "TEST")
    assert len(result) == 2
    cancel_ids = {o.order_id for o in result}
    assert cancel_ids == {"buy-id-1", "buy-id-2"}
    for o in result:
        assert o.order_type == "CANCEL"
        assert o.side == "BUY"


def test_cancel_all_buys_empty_pending_produces_nothing() -> None:
    """No pending orders → CANCEL_ALL_BUYS produces an empty list."""
    bk = _bk_with_cash(1_000.0)
    result = process(
        [AnalystOrder(type="CANCEL_ALL_BUYS")],
        _make_status("IDLE", pending_orders={}),
        bk,
        "TEST",
    )
    assert result == []


# ---------------------------------------------------------------------------
# UPDATE_STOPLOSS
# ---------------------------------------------------------------------------

def test_update_stoploss_cancels_old_and_creates_new() -> None:
    """Existing SELL in pending_orders → CANCEL the old + fresh SELL LIMIT at new price."""
    bk = _bk_with_shares(qty=10.0, price=100.0)
    status = _make_status("LONG", watermark_level=2,
                          pending_orders={"stoploss-id": "SELL"})
    result = process(
        [AnalystOrder(type="UPDATE_STOPLOSS", price=105.0, watermark=3)],
        status,
        bk,
        "TEST",
    )
    cancel_orders = [o for o in result if o.order_type == "CANCEL"]
    new_orders = [o for o in result if o.order_type == "LIMIT"]
    assert len(cancel_orders) == 1
    assert cancel_orders[0].order_id == "stoploss-id"
    assert cancel_orders[0].side == "SELL"
    assert len(new_orders) == 1
    assert new_orders[0].side == "SELL"
    assert new_orders[0].limit_price == 105.0
    assert new_orders[0].quantity == 10.0


def test_update_stoploss_no_existing_creates_fresh(caplog: pytest.LogCaptureFixture) -> None:
    """No SELL in pending_orders → warning logged, fresh SELL LIMIT created from shares_held."""
    bk = _bk_with_shares(qty=10.0, price=100.0)
    status = _make_status("LONG", watermark_level=2, pending_orders={})
    with caplog.at_level(logging.WARNING, logger="src.trader.trader"):
        result = process(
            [AnalystOrder(type="UPDATE_STOPLOSS", price=105.0)],
            status,
            bk,
            "TEST",
        )
    assert any("UPDATE_STOPLOSS" in r.message for r in caplog.records)
    assert len(result) == 1
    assert result[0].order_type == "LIMIT"
    assert result[0].side == "SELL"
    assert result[0].limit_price == 105.0
    assert result[0].quantity == 10.0

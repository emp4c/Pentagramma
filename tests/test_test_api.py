"""
Unit tests for Phase 3 — TestBus (dev_tools/test_api/test_bus.py).

Covers:
    - BUY LIMIT fills on the first bar where low <= limit_price (not before)
    - SELL LIMIT fills on the first bar where high >= limit_price (not before)
    - MARKET order fills at next bar's open
    - Order that never satisfies its condition returns None from get_confirmation
    - cancel_order returns True for a pending (unfilled) order
    - cancel_order returns False for an already-filled order
    - cancel_order returns False for an unknown order_id
    - cancel_order on a filled order does not undo the fill
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from src.models import BrokerOrder, OHLCVBar
from dev_tools.test_api.test_bus import TestBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TS = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)


def _bar(
    index: int,
    low: float,
    high: float,
    open_: float = 100.0,
    close: float = 100.0,
) -> OHLCVBar:
    return OHLCVBar(
        ticker="AAPL",
        timestamp=BASE_TS + timedelta(minutes=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000.0,
    )


def _order(
    side: str = "BUY",
    order_type: str = "LIMIT",
    limit_price: float | None = 100.0,
    quantity: float = 10.0,
) -> BrokerOrder:
    return BrokerOrder(
        order_id=str(uuid.uuid4()),
        ticker="AAPL",
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        quantity=quantity,
        condition=None,
        created_at=BASE_TS,
    )


# ---------------------------------------------------------------------------
# BUY LIMIT
# ---------------------------------------------------------------------------

class TestBuyLimit:
    def test_fills_on_first_eligible_bar(self):
        """Fill must occur on the first bar where low <= limit_price, not earlier."""
        limit = 100.0
        bars = [
            _bar(0, low=101.0, high=105.0),  # low > limit — no fill
            _bar(1, low=101.0, high=105.0),  # low > limit — no fill
            _bar(2, low=99.0,  high=105.0),  # low <= limit — fills here
            _bar(3, low=98.0,  high=105.0),  # should not be reached
        ]
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=limit)

        order_id = bus.send_order(order)
        conf = bus.get_confirmation(order_id)

        assert conf is not None
        assert conf.filled_price == limit
        assert conf.filled_at == bars[2].timestamp
        assert conf.filled_quantity == order.quantity
        assert conf.side == "BUY"

    def test_filled_price_is_limit_not_bar_low(self):
        """Fills execute at limit_price, not at the bar's low."""
        limit = 100.0
        bars = [_bar(0, low=95.0, high=105.0)]  # low well below limit
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=limit)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is not None
        assert conf.filled_price == limit  # not 95.0

    def test_no_fill_when_low_never_reaches_limit(self):
        bars = [
            _bar(0, low=101.0, high=110.0),
            _bar(1, low=102.0, high=111.0),
        ]
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=100.0)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is None

    def test_no_fill_on_empty_future_bars(self):
        bus = TestBus([])
        order = _order(side="BUY", order_type="LIMIT", limit_price=100.0)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is None


# ---------------------------------------------------------------------------
# SELL LIMIT
# ---------------------------------------------------------------------------

class TestSellLimit:
    def test_fills_on_first_eligible_bar(self):
        """Fill must occur on the first bar where high >= limit_price, not earlier."""
        limit = 110.0
        bars = [
            _bar(0, low=100.0, high=108.0),  # high < limit — no fill
            _bar(1, low=100.0, high=109.0),  # high < limit — no fill
            _bar(2, low=100.0, high=112.0),  # high >= limit — fills here
            _bar(3, low=100.0, high=115.0),  # should not be reached
        ]
        bus = TestBus(bars)
        order = _order(side="SELL", order_type="LIMIT", limit_price=limit)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is not None
        assert conf.filled_price == limit
        assert conf.filled_at == bars[2].timestamp
        assert conf.side == "SELL"

    def test_filled_price_is_limit_not_bar_high(self):
        limit = 110.0
        bars = [_bar(0, low=100.0, high=120.0)]
        bus = TestBus(bars)
        order = _order(side="SELL", order_type="LIMIT", limit_price=limit)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is not None
        assert conf.filled_price == limit  # not 120.0

    def test_no_fill_when_high_never_reaches_limit(self):
        bars = [
            _bar(0, low=100.0, high=108.0),
            _bar(1, low=100.0, high=109.0),
        ]
        bus = TestBus(bars)
        order = _order(side="SELL", order_type="LIMIT", limit_price=110.0)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is None


# ---------------------------------------------------------------------------
# MARKET orders
# ---------------------------------------------------------------------------

class TestMarketOrder:
    def test_market_buy_fills_at_next_bar_open(self):
        bars = [
            _bar(0, low=99.0, high=102.0, open_=101.0),
            _bar(1, low=99.0, high=103.0, open_=102.0),
        ]
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="MARKET", limit_price=None)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is not None
        assert conf.filled_price == 101.0  # bars[0].open
        assert conf.filled_at == bars[0].timestamp

    def test_market_sell_fills_at_next_bar_open(self):
        bars = [_bar(0, low=99.0, high=105.0, open_=103.5)]
        bus = TestBus(bars)
        order = _order(side="SELL", order_type="MARKET", limit_price=None)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is not None
        assert conf.filled_price == 103.5
        assert conf.side == "SELL"

    def test_market_order_no_fill_on_empty_bars(self):
        bus = TestBus([])
        order = _order(side="BUY", order_type="MARKET", limit_price=None)

        conf = bus.get_confirmation(bus.send_order(order))

        assert conf is None


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_cancel_pending_order_returns_true(self):
        """Unfilled order can be cancelled — cancel_order returns True."""
        bars = [_bar(0, low=101.0, high=105.0)]  # low never <= 90 (limit)
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=90.0)
        order_id = bus.send_order(order)

        assert bus.get_confirmation(order_id) is None  # confirm it's unfilled
        result = bus.cancel_order(order_id)

        assert result is True

    def test_cancel_filled_order_returns_false(self):
        """Filled order cannot be cancelled — cancel_order returns False."""
        bars = [_bar(0, low=99.0, high=105.0)]  # low <= 100 → fills immediately
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=100.0)
        order_id = bus.send_order(order)

        assert bus.get_confirmation(order_id) is not None  # confirm it filled
        result = bus.cancel_order(order_id)

        assert result is False

    def test_cancel_unknown_order_returns_false(self):
        bus = TestBus([])
        result = bus.cancel_order(str(uuid.uuid4()))

        assert result is False

    def test_cancel_filled_order_does_not_undo_fill(self):
        """After a failed cancel, get_confirmation still returns the fill."""
        bars = [_bar(0, low=99.0, high=105.0)]
        bus = TestBus(bars)
        order = _order(side="BUY", order_type="LIMIT", limit_price=100.0)
        order_id = bus.send_order(order)

        original_conf = bus.get_confirmation(order_id)
        bus.cancel_order(order_id)  # returns False but must not mutate state
        after_conf = bus.get_confirmation(order_id)

        assert after_conf is not None
        assert after_conf.confirmation_id == original_conf.confirmation_id

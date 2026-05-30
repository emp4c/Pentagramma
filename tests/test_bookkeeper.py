"""
Unit tests for Phase 3 — Bookkeeper (src/bookkeeper/bookkeeper.py).

Covers:
    - Cash decreases correctly on BUY confirmation
    - Shares increase correctly on BUY confirmation
    - Cash increases correctly on SELL confirmation
    - Shares decrease correctly on SELL confirmation
    - Duplicate confirmation_id is silently ignored (no double-counting)
    - available_cash() returns cash minus MIN_CASH_RESERVE
    - current_nav() returns cash plus shares * last_price
    - ValueError raised when cash falls below MIN_CASH_RESERVE
    - LedgerEntry is appended and readable via get_ledger()
    - get_ledger() returns a copy (mutations do not affect internal state)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.models import ExecutionConfirmation, LedgerEntry
from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import MIN_CASH_RESERVE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FILLED_AT = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)


def _conf(
    side: str,
    quantity: float,
    price: float,
    confirmation_id: str | None = None,
    order_id: str | None = None,
) -> ExecutionConfirmation:
    return ExecutionConfirmation(
        confirmation_id=confirmation_id or str(uuid.uuid4()),
        order_id=order_id or str(uuid.uuid4()),
        ticker="AAPL",
        side=side,
        filled_quantity=quantity,
        filled_price=price,
        filled_at=FILLED_AT,
    )


# ---------------------------------------------------------------------------
# BUY confirmations
# ---------------------------------------------------------------------------

class TestBuyConfirmation:
    def test_cash_decreases_on_buy(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))
        # 10 shares * $100 = $1000 spent
        assert bk._cash == pytest.approx(9_000.0)

    def test_shares_increase_on_buy(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))
        assert bk.shares_held() == pytest.approx(10.0)

    def test_cash_and_shares_accumulate_across_buys(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=5.0, price=100.0))
        bk.receive_confirmation(_conf("BUY", quantity=5.0, price=100.0))
        assert bk._cash == pytest.approx(9_000.0)
        assert bk.shares_held() == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# SELL confirmations
# ---------------------------------------------------------------------------

class TestSellConfirmation:
    def test_cash_increases_on_sell(self):
        bk = Bookkeeper(initial_cash=9_000.0)
        bk.receive_confirmation(_conf("SELL", quantity=10.0, price=100.0))
        assert bk._cash == pytest.approx(10_000.0)

    def test_shares_decrease_on_sell(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))
        bk.receive_confirmation(_conf("SELL", quantity=10.0, price=100.0))
        assert bk.shares_held() == pytest.approx(0.0)

    def test_round_trip_buy_then_sell_restores_cash(self):
        """Buy then sell at same price should return to starting cash."""
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY",  quantity=10.0, price=100.0))
        bk.receive_confirmation(_conf("SELL", quantity=10.0, price=100.0))
        assert bk._cash == pytest.approx(10_000.0)
        assert bk.shares_held() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_confirmation_id_is_ignored(self):
        """Second receive_confirmation with the same confirmation_id is a no-op."""
        bk = Bookkeeper(initial_cash=10_000.0)
        conf = _conf("BUY", quantity=10.0, price=100.0, confirmation_id="dup-id-001")
        bk.receive_confirmation(conf)
        bk.receive_confirmation(conf)  # duplicate — must not double-count

        assert bk._cash == pytest.approx(9_000.0)
        assert bk.shares_held() == pytest.approx(10.0)

    def test_duplicate_adds_no_ledger_entry(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        conf = _conf("BUY", quantity=10.0, price=100.0, confirmation_id="dup-id-002")
        bk.receive_confirmation(conf)
        bk.receive_confirmation(conf)

        assert len(bk.get_ledger()) == 1

    def test_different_confirmation_ids_are_not_deduplicated(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=5.0, price=100.0, confirmation_id="conf-A"))
        bk.receive_confirmation(_conf("BUY", quantity=5.0, price=100.0, confirmation_id="conf-B"))

        assert len(bk.get_ledger()) == 2
        assert bk.shares_held() == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# available_cash
# ---------------------------------------------------------------------------

class TestAvailableCash:
    def test_available_cash_subtracts_reserve(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        assert bk.available_cash() == pytest.approx(10_000.0 - MIN_CASH_RESERVE)

    def test_available_cash_reflects_spent_cash(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))
        expected = 9_000.0 - MIN_CASH_RESERVE
        assert bk.available_cash() == pytest.approx(expected)

    def test_available_cash_raises_when_below_reserve(self):
        """If a BUY drains cash below MIN_CASH_RESERVE, available_cash must raise."""
        # initial_cash is just enough to buy; the purchase leaves cash below reserve
        initial = MIN_CASH_RESERVE + 50.0   # e.g. 150.0
        bk = Bookkeeper(initial_cash=initial)
        bk.receive_confirmation(_conf("BUY", quantity=1.0, price=100.0))
        # cash is now 50.0 < MIN_CASH_RESERVE (100.0)

        with pytest.raises(ValueError):
            bk.available_cash()


# ---------------------------------------------------------------------------
# current_nav
# ---------------------------------------------------------------------------

class TestCurrentNav:
    def test_nav_cash_only(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        assert bk.current_nav(last_price=100.0) == pytest.approx(10_000.0)

    def test_nav_includes_share_value(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))
        # cash = 9000, shares = 10, last_price = 105
        assert bk.current_nav(last_price=105.0) == pytest.approx(9_000.0 + 10 * 105.0)

    def test_nav_zero_shares_price_has_no_effect(self):
        bk = Bookkeeper(initial_cash=5_000.0)
        assert bk.current_nav(last_price=999.0) == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# get_ledger
# ---------------------------------------------------------------------------

class TestGetLedger:
    def test_ledger_entry_created_on_confirmation(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        conf = _conf("BUY", quantity=10.0, price=100.0)
        bk.receive_confirmation(conf)

        ledger = bk.get_ledger()
        assert len(ledger) == 1
        entry = ledger[0]
        assert entry.confirmation_id == conf.confirmation_id
        assert entry.ticker == "AAPL"
        assert entry.side == "BUY"
        assert entry.quantity == pytest.approx(10.0)
        assert entry.price == pytest.approx(100.0)
        assert entry.gross_value == pytest.approx(1_000.0)
        assert entry.cash_after == pytest.approx(9_000.0)
        assert entry.shares_after == pytest.approx(10.0)

    def test_get_ledger_returns_copy(self):
        """Mutating the returned list must not affect the internal ledger."""
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_conf("BUY", quantity=10.0, price=100.0))

        ledger = bk.get_ledger()
        ledger.clear()  # mutate the returned copy

        assert len(bk.get_ledger()) == 1  # internal ledger unaffected

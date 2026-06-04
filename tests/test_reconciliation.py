"""
Unit tests for Bookkeeper.reconcile() — Phase C, Part 4.

Covers:
    - No discrepancy: reconcile() is silent and leaves state unchanged.
    - Cash mismatch (>0.01): logs a WARNING and updates bookkeeper cash to broker value.
    - Shares mismatch (>0.01): logs a WARNING and updates bookkeeper shares to broker value.
    - Both mismatched: both warnings logged, both values updated.
    - Difference exactly at tolerance boundary (<=0.01): treated as matching, no warning.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.bookkeeper.bookkeeper import Bookkeeper
from src.models import ExecutionConfirmation


FILLED_AT = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)


def _buy_conf(quantity: float, price: float) -> ExecutionConfirmation:
    return ExecutionConfirmation(
        confirmation_id=str(uuid.uuid4()),
        order_id=str(uuid.uuid4()),
        ticker="AAPL",
        side="BUY",
        filled_quantity=quantity,
        filled_price=price,
        filled_at=FILLED_AT,
    )


class TestReconcileNoDiscrepancy:
    def test_matching_cash_logs_no_warning(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        with patch("logging.Logger.warning") as mock_warn:
            bk.reconcile(broker_cash=10_000.0, broker_shares=0.0, ticker="AAPL")
        mock_warn.assert_not_called()

    def test_matching_values_leave_state_unchanged(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        # cash = 9000, shares = 10
        bk.reconcile(broker_cash=9_000.0, broker_shares=10.0, ticker="AAPL")
        assert bk._cash == pytest.approx(9_000.0)
        assert bk._shares == pytest.approx(10.0)

    def test_difference_within_tolerance_is_silent(self):
        """Difference well below 0.01 must not trigger a warning or update state."""
        bk = Bookkeeper(initial_cash=10_000.0)
        with patch("logging.Logger.warning") as mock_warn:
            bk.reconcile(broker_cash=10_000.005, broker_shares=0.0, ticker="AAPL")
        mock_warn.assert_not_called()
        assert bk._cash == pytest.approx(10_000.0)


class TestReconcileCashMismatch:
    def test_cash_mismatch_logs_warning(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        with patch("logging.Logger.warning") as mock_warn:
            bk.reconcile(broker_cash=9_999.50, broker_shares=0.0, ticker="AAPL")
        assert mock_warn.call_count >= 1
        # Warning message must mention 'cash'
        args_str = str(mock_warn.call_args_list)
        assert "cash" in args_str.lower()

    def test_cash_mismatch_updates_cash_to_broker_value(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.reconcile(broker_cash=9_999.00, broker_shares=0.0, ticker="AAPL")
        assert bk._cash == pytest.approx(9_999.00)

    def test_cash_mismatch_does_not_add_ledger_entry(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.reconcile(broker_cash=9_500.00, broker_shares=0.0, ticker="AAPL")
        assert len(bk.get_ledger()) == 0


class TestReconcileSharesMismatch:
    def test_shares_mismatch_logs_warning(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        # bookkeeper has 10 shares; broker says 9.5
        with patch("logging.Logger.warning") as mock_warn:
            bk.reconcile(broker_cash=9_000.0, broker_shares=9.5, ticker="AAPL")
        assert mock_warn.call_count >= 1
        args_str = str(mock_warn.call_args_list)
        assert "share" in args_str.lower()

    def test_shares_mismatch_updates_shares_to_broker_value(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        bk.reconcile(broker_cash=9_000.0, broker_shares=9.5, ticker="AAPL")
        assert bk._shares == pytest.approx(9.5)

    def test_shares_mismatch_does_not_add_ledger_entry(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        initial_ledger_len = len(bk.get_ledger())
        bk.reconcile(broker_cash=9_000.0, broker_shares=9.5, ticker="AAPL")
        assert len(bk.get_ledger()) == initial_ledger_len


class TestReconcileBothMismatch:
    def test_both_mismatched_updates_both(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        # bookkeeper: cash=9000, shares=10 — broker says cash=8950, shares=9.8
        bk.reconcile(broker_cash=8_950.0, broker_shares=9.8, ticker="AAPL")
        assert bk._cash == pytest.approx(8_950.0)
        assert bk._shares == pytest.approx(9.8)

    def test_both_mismatched_logs_two_warnings(self):
        bk = Bookkeeper(initial_cash=10_000.0)
        bk.receive_confirmation(_buy_conf(quantity=10.0, price=100.0))
        with patch("logging.Logger.warning") as mock_warn:
            bk.reconcile(broker_cash=8_950.0, broker_shares=9.8, ticker="AAPL")
        assert mock_warn.call_count >= 2
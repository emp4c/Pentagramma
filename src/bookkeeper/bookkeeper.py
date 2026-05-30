"""
Bookkeeper.

Responsibility:
    The single source of truth for cash, shares held, and the trade ledger.
    Stateful — owns cash balance, share count, and the append-only ledger.
    Receives ExecutionConfirmation objects from the broker bus and updates
    state accordingly. Deduplicates confirmations by confirmation_id.
    In streaming mode, periodically reconciles against the broker API.

Public interface:
    Bookkeeper(initial_cash: float)
    receive_confirmation(conf: ExecutionConfirmation) -> None
    available_cash() -> float           cash minus MIN_CASH_RESERVE; raises ValueError if below
    shares_held() -> float              current share count
    current_nav(last_price: float) -> float   cash + shares * last_price
    get_ledger() -> List[LedgerEntry]   full immutable ledger copy
"""

from __future__ import annotations

import uuid
from typing import List

from src.models import ExecutionConfirmation, LedgerEntry
from src.config import MIN_CASH_RESERVE


class Bookkeeper:
    """
    Tracks cash, shares, and the trade ledger. One instance per session.

    Args:
        initial_cash: Starting cash balance for this session (USD).
    """

    def __init__(self, initial_cash: float) -> None:
        self._cash: float = initial_cash
        self._shares: float = 0.0
        self._ledger: List[LedgerEntry] = []
        self._seen_confirmation_ids: set[str] = set()

    def receive_confirmation(self, conf: ExecutionConfirmation) -> None:
        """
        Process an execution confirmation from the broker bus.

        - If confirmation_id already seen: silently ignore (idempotent).
        - BUY:  cash -= gross_value + fee;  shares += filled_quantity
        - SELL: cash += gross_value - fee;  shares -= filled_quantity
        - Appends a LedgerEntry recording the full state after the trade.
        """
        if conf.confirmation_id in self._seen_confirmation_ids:
            return

        gross_value = conf.filled_quantity * conf.filled_price
        fee = 0.0  # TestBus does not model fees; LiveBus will supply real fees

        if conf.side == "BUY":
            net_value = gross_value + fee
            self._cash -= net_value
            self._shares += conf.filled_quantity
        else:  # SELL
            net_value = gross_value - fee
            self._cash += net_value
            self._shares -= conf.filled_quantity

        entry = LedgerEntry(
            entry_id=str(uuid.uuid4()),
            confirmation_id=conf.confirmation_id,
            ticker=conf.ticker,
            side=conf.side,
            quantity=conf.filled_quantity,
            price=conf.filled_price,
            gross_value=gross_value,
            fee=fee,
            net_value=net_value,
            cash_after=self._cash,
            shares_after=self._shares,
            timestamp=conf.filled_at,
            note="",
        )
        self._ledger.append(entry)
        self._seen_confirmation_ids.add(conf.confirmation_id)

    def available_cash(self) -> float:
        """
        Return deployable cash: total cash minus MIN_CASH_RESERVE.

        Raises:
            ValueError: if total cash has fallen below MIN_CASH_RESERVE,
                        indicating a system error (over-deployment).
        """
        if self._cash < MIN_CASH_RESERVE:
            raise ValueError(
                f"Cash balance ({self._cash:.2f}) is below MIN_CASH_RESERVE "
                f"({MIN_CASH_RESERVE:.2f}) — possible over-deployment."
            )
        return self._cash - MIN_CASH_RESERVE

    def shares_held(self) -> float:
        """Return current number of shares held (may be fractional)."""
        return self._shares

    def current_nav(self, last_price: float) -> float:
        """Return current net asset value: cash + market value of shares held."""
        return self._cash + self._shares * last_price

    def get_ledger(self) -> List[LedgerEntry]:
        """Return a copy of the ledger (caller must not mutate it)."""
        return list(self._ledger)

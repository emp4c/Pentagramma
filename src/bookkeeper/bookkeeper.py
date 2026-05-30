"""
Bookkeeper.

Responsibility:
    The single source of truth for cash, shares held, and the trade ledger.
    Stateful — owns cash balance, share count, and the append-only ledger.
    Receives ExecutionConfirmation objects from the broker bus and updates
    state accordingly. Deduplicates confirmations by confirmation_id.
    In streaming mode, periodically reconciles against the broker API.

Public interface:
    Bookkeeper(initial_cash: float)     constructor
    receive_confirmation(conf)          process a broker execution confirmation
    available_cash() -> float           cash minus MIN_CASH_RESERVE
    shares_held() -> float              current share count
    get_ledger() -> List[LedgerEntry]   full immutable ledger copy
"""

from __future__ import annotations

from typing import List, Set

from src.models import ExecutionConfirmation, LedgerEntry
from src.config import MIN_CASH_RESERVE


class Bookkeeper:
    """
    Tracks cash, shares, and the trade ledger. One instance per session.

    Args:
        initial_cash: Starting cash balance for this session (USD).
    """

    def __init__(self, initial_cash: float) -> None:
        raise NotImplementedError

    def receive_confirmation(self, conf: ExecutionConfirmation) -> None:
        """
        Process an execution confirmation from the broker bus.

        - If conf.confirmation_id has already been seen, silently ignore (idempotent).
        - On BUY: subtract gross_value + fee from cash; add filled_quantity to shares.
        - On SELL: add gross_value - fee to cash; subtract filled_quantity from shares.
        - Append a new LedgerEntry to the ledger.

        Args:
            conf: The execution confirmation from the broker (live or fake).
        """
        raise NotImplementedError

    def available_cash(self) -> float:
        """
        Return deployable cash: total cash minus MIN_CASH_RESERVE.
        Never negative — returns 0.0 if cash <= MIN_CASH_RESERVE.
        """
        raise NotImplementedError

    def shares_held(self) -> float:
        """Return current number of shares held (may be fractional)."""
        raise NotImplementedError

    def get_ledger(self) -> List[LedgerEntry]:
        """Return a copy of the ledger list (caller must not mutate it)."""
        raise NotImplementedError

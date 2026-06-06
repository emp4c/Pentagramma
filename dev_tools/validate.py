"""
Phase 9 — Integration & Validation Script.

Calls run_batch() for a given ticker and date range, then runs six automated
checks against the result. Prints PASS / FAIL / WARN for each check and a
summary line at the end.

Usage:
    python dev_tools/validate.py --ticker AAPL --start 2026-02-01 --end 2026-03-15

Exit code: 0 if FAILED == 0; 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

# Ensure project root is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_tools.batch_runner.runner import RunResult, run_batch
from src.config import MIN_CASH_RESERVE, PIVOT_RANGE
from src.models import OHLCVBar
from src.scriber.db import fetch_bars


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check1_no_naked_positions(result: RunResult) -> tuple[str, str]:
    """Every BUY fill must have a STOP/SELL order in order_log within 1 bar."""
    buy_fills = [(t, conf) for t, conf in result.execution_log if conf.side == "BUY"]
    if not buy_fills:
        return "PASS", ""

    # Map bar index → set of bar indices where a STOP/SELL order was placed
    stop_bars: set[int] = {
        t
        for t, order in result.order_log
        if order.order_type == "STOP" and order.side == "SELL"
    }

    failures = []
    for t, conf in buy_fills:
        if t not in stop_bars and (t + 1) not in stop_bars:
            failures.append(
                f"BUY fill at bar {t} (order_id={conf.order_id}) "
                "has no STOP/SELL within 1 bar"
            )

    if failures:
        return "FAIL", "; ".join(failures)
    return "PASS", ""


def check2_cash_floor(result: RunResult) -> tuple[str, str]:
    """No LedgerEntry may have cash_after < MIN_CASH_RESERVE."""
    violations = [
        f"entry {e.entry_id}: cash_after={e.cash_after:.2f}"
        for e in result.ledger
        if e.cash_after < MIN_CASH_RESERVE
    ]
    if violations:
        return "FAIL", f"{len(violations)} violation(s): " + "; ".join(violations[:3]) + (
            "…" if len(violations) > 3 else ""
        )
    return "PASS", ""


def check3_no_duplicate_confs(result: RunResult) -> tuple[str, str]:
    """All confirmation_ids in the ledger must be unique."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for e in result.ledger:
        if e.confirmation_id in seen:
            duplicates.append(e.confirmation_id)
        seen.add(e.confirmation_id)

    if duplicates:
        return "FAIL", f"{len(duplicates)} duplicate confirmation ID(s): {duplicates[:3]}"
    return "PASS", ""


def check4_pivot_sort_and_range(
    result: RunResult, test_bars: list[OHLCVBar]
) -> tuple[str, str]:
    """Pivots at each checkpoint are strictly ascending and within ±PIVOT_RANGE of bar close."""
    if not result.pivot_snapshots:
        return "PASS", "(no pivot snapshots)"

    failures: list[str] = []
    for c, pivots in sorted(result.pivot_snapshots.items()):
        if not pivots:
            continue

        # Strictly ascending
        for j in range(len(pivots) - 1):
            if pivots[j] >= pivots[j + 1]:
                failures.append(
                    f"checkpoint {c}: not strictly ascending at index {j} "
                    f"({pivots[j]} >= {pivots[j + 1]})"
                )
                break

        # ±PIVOT_RANGE of the bar close at index c
        if c < len(test_bars):
            ref_close = test_bars[c].close
            lower = ref_close * (1.0 - PIVOT_RANGE)
            upper = ref_close * (1.0 + PIVOT_RANGE)
            out_of_range = [p for p in pivots if p < lower or p > upper]
            if out_of_range:
                failures.append(
                    f"checkpoint {c}: {len(out_of_range)} pivot(s) outside "
                    f"±{PIVOT_RANGE * 100:.0f}% of close {ref_close:.2f} "
                    f"(sample: {out_of_range[:2]})"
                )

    if failures:
        return "FAIL", "; ".join(failures)
    return "PASS", ""


def check5_position_consistency(result: RunResult) -> tuple[str, str]:
    """Replay ledger entries to reconstruct final position; must match last entry's side."""
    if not result.ledger:
        return "PASS", ""

    position = "IDLE"
    for entry in result.ledger:
        if entry.side == "BUY":
            position = "LONG"
        elif entry.side == "SELL":
            position = "IDLE"

    last = result.ledger[-1]
    expected = "LONG" if last.side == "BUY" else "IDLE"

    if position != expected:
        return "FAIL", f"reconstructed={position!r} expected={expected!r}"
    return "PASS", ""


def check6_trade_pairing(result: RunResult) -> tuple[str, str]:
    """Every BUY must have a subsequent SELL; WARN if last trade is an open position."""
    n_buys = sum(1 for e in result.ledger if e.side == "BUY")
    n_sells = sum(1 for e in result.ledger if e.side == "SELL")

    unpaired = n_buys - n_sells
    if unpaired < 0:
        return "FAIL", f"more SELLs ({n_sells}) than BUYs ({n_buys})"
    if unpaired > 1:
        return "FAIL", f"{unpaired} unpaired BUY entries — single-position constraint violated"
    if unpaired == 1:
        return "WARN", f"1 open position at end of run (BUYs={n_buys}, SELLs={n_sells})"
    return "PASS", ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS = [
    ("CHECK 1", "NO NAKED POSITIONS",    check1_no_naked_positions),
    ("CHECK 2", "CASH FLOOR",            check2_cash_floor),
    ("CHECK 3", "NO DUPLICATE CONFS",    check3_no_duplicate_confs),
    # CHECK 4 needs test_bars injected separately — handled in main()
    ("CHECK 5", "POSITION CONSISTENCY",  check5_position_consistency),
    ("CHECK 6", "TRADE PAIRING",         check6_trade_pairing),
]


def run_validation(
    ticker: str,
    start_date: date,
    end_date: date,
    initial_cash: float = 10_000.0,
    db_path: str | None = None,
) -> int:
    """Run all checks and print a summary. Returns exit code (0 = no failures)."""
    print(f"Running batch for {ticker}  {start_date} → {end_date} …")
    result = run_batch(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        db_path=db_path,
    )
    print(
        f"Batch complete: {result.bars_processed} bars, "
        f"{len(result.ledger)} ledger entries\n"
    )

    # Fetch test window bars for CHECK 4 pivot-range validation
    test_start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    test_end = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    test_bars = fetch_bars(ticker, test_start, test_end, db_path=db_path)

    # Evaluate checks 1, 2, 3, 5, 6
    results: list[tuple[str, str, str, str]] = []  # (check_id, label, status, detail)
    for check_id, label, fn in _CHECKS:
        status, detail = fn(result)
        results.append((check_id, label, status, detail))

    # Insert CHECK 4 in the correct slot (position 3 = after CHECK 3)
    status4, detail4 = check4_pivot_sort_and_range(result, test_bars)
    results.insert(3, ("CHECK 4", "PIVOT SORT & RANGE", status4, detail4))

    # Tally
    passed = sum(1 for _, _, s, _ in results if s == "PASS")
    warned  = sum(1 for _, _, s, _ in results if s == "WARN")
    failed  = sum(1 for _, _, s, _ in results if s == "FAIL")

    # Print summary table
    width = 60
    print("=" * width)
    print(f"VALIDATION SUMMARY — {ticker}  {start_date} → {end_date}")
    for check_id, label, status, detail in results:
        line = f"{check_id}  {label:<26} {status}"
        if detail:
            line += f"  ({detail})"
        print(line)
    print("=" * width)
    print(f"PASSED: {passed}   WARNED: {warned}   FAILED: {failed}")

    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 9 integration & validation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="Start of the test window",
    )
    parser.add_argument(
        "--end", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="End of the test window (inclusive)",
    )
    parser.add_argument("--cash", type=float, default=10_000.0, help="Initial cash")
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="OHLCV SQLite DB path (defaults to config.DB_PATH)",
    )
    args = parser.parse_args()

    sys.exit(
        run_validation(
            ticker=args.ticker,
            start_date=args.start,
            end_date=args.end,
            initial_cash=args.cash,
            db_path=args.db,
        )
    )


if __name__ == "__main__":
    main()

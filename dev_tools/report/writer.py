"""
Report Writer (Dev / Testing Only).

Responsibility:
    Produces a human-readable text report from a completed batch run.
    One line per bar: timestamp, OHLCV values, machine status at end of that bar,
    orders issued, and any fills received. Followed by a trade summary section.

    Output filename: {ticker}_{start_date}_{end_date}_{YYYYMMDD_HHMMSS}.txt
    Output directory: outputs/active/ (or caller-supplied override).

Public interface:
    write_report(result, bars, output_dir) -> str   # returns full output path
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Tuple

from src.config import PIVOT_INTERVAL_BARS
from src.models import BrokerOrder, ExecutionConfirmation, LedgerEntry, OHLCVBar

if TYPE_CHECKING:
    from dev_tools.batch_runner.runner import RunResult

_EST = timezone(timedelta(hours=-5))
_SEP = "=" * 60


def write_report(
    result: "RunResult",
    bars: List[OHLCVBar],
    output_dir: str = "outputs/active",
) -> str:
    """
    Write a human-readable trading report to disk.

    Args:
        result:     Completed RunResult from run_batch().
        bars:       The test-window bars in replay order (same list passed to the loop).
        output_dir: Directory for the output file (created if absent).

    Returns:
        Absolute path of the written report file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    now_est = datetime.now(tz=_EST)
    filename = (
        f"{result.ticker}_{result.start_date}_{result.end_date}"
        f"_{now_est.strftime('%Y%m%d_%H%M%S')}.txt"
    )
    output_path = str(Path(output_dir) / filename)

    sections: List[str] = []
    sections.extend(_header(result, now_est))
    sections.append("")
    sections.extend(_bar_section(result, bars, result.pivot_snapshots))
    sections.append("")
    sections.extend(_trade_summary(result, bars))

    Path(output_path).write_text("\n".join(sections), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _header(result: "RunResult", now_est: datetime) -> List[str]:
    initial = result.initial_cash
    final = result.final_nav
    pct = (final - initial) / initial * 100 if initial else 0.0
    sign = "+" if pct >= 0 else ""
    trade_count = sum(1 for e in result.ledger if e.side == "BUY")
    return [
        _SEP,
        "ALGO TRADER — RUN REPORT",
        f"Ticker:       {result.ticker}",
        f"Period:       {result.start_date} → {result.end_date}",
        f"Initial cash: ${initial:,.2f}",
        f"Final NAV:    ${final:,.2f}  ({sign}{pct:.2f}%)",
        f"Trades:       {trade_count}",
        f"Generated:    {now_est.strftime('%Y-%m-%d %H:%M:%S')} EST",
        _SEP,
    ]


def _bar_section(
    result: "RunResult",
    bars: List[OHLCVBar],
    pivot_snapshots: Dict[int, List[float]],
) -> List[str]:
    fills_at: Dict[int, List[ExecutionConfirmation]] = {}
    for bi, conf in result.execution_log:
        fills_at.setdefault(bi, []).append(conf)

    orders_at: Dict[int, List[BrokerOrder]] = {}
    for bi, order in result.order_log:
        orders_at.setdefault(bi, []).append(order)

    position = "IDLE"
    lines: List[str] = []

    for t, bar in enumerate(bars):
        # Apply fills at this bar to track position at END of bar processing
        for conf in fills_at.get(t, []):
            position = "LONG" if conf.side == "BUY" else "IDLE"

        checkpoint = (t // PIVOT_INTERVAL_BARS) * PIVOT_INTERVAL_BARS
        active_pivots = pivot_snapshots.get(checkpoint, [])

        ts_est = _to_est(bar.timestamp).strftime("%Y-%m-%d %H:%M")
        ohlcv = (
            f"O:{bar.open:<8.2f}H:{bar.high:<8.2f}"
            f"L:{bar.low:<8.2f}C:{bar.close:<8.2f}"
            f"V:{int(bar.volume)}"
        )
        pvt = _pivot_triplet(active_pivots, bar.close)

        parts: List[str] = []
        for conf in fills_at.get(t, []):
            parts.append(f"FILLED {conf.side}@{conf.filled_price:.2f}")
        for order in orders_at.get(t, []):
            parts.append(_order_label(order))

        activity = "  ".join(parts) if parts else "—"
        lines.append(f"{ts_est} | {ohlcv} | {pvt} | {position:<4} | {activity}")

    return lines


def _trade_summary(result: "RunResult", bars: List[OHLCVBar]) -> List[str]:
    lines: List[str] = [_SEP, "TRADE LOG"]

    buys  = [e for e in result.ledger if e.side == "BUY"]
    sells = [e for e in result.ledger if e.side == "SELL"]
    pairs: List[Tuple[LedgerEntry, LedgerEntry | None]] = [
        (b, sells[i] if i < len(sells) else None)
        for i, b in enumerate(buys)
    ]

    if not pairs:
        lines.append("No trades executed.")
    else:
        col = (
            f"{'#':<4} {'Side':<5} {'Entry time':<17} {'Entry px':>9}  "
            f"{'Exit time':<17} {'Exit px':>9}  {'Shares':>8}  {'P&L':>10}"
        )
        lines.append(col)

        pnl_values: List[float] = []
        for i, (buy, sell) in enumerate(pairs, 1):
            entry_t = _to_est(buy.timestamp).strftime("%Y-%m-%d %H:%M")
            if sell is not None:
                exit_t = _to_est(sell.timestamp).strftime("%Y-%m-%d %H:%M")
                pnl = (sell.price - buy.price) * buy.quantity
                pnl_values.append(pnl)
                pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
                lines.append(
                    f"{i:<4} {'LONG':<5} {entry_t:<17} {buy.price:>9.2f}  "
                    f"{exit_t:<17} {sell.price:>9.2f}  {buy.quantity:>8.2f}  {pnl_str:>10}"
                )
            else:
                lines.append(
                    f"{i:<4} {'LONG':<5} {entry_t:<17} {buy.price:>9.2f}  "
                    f"{'—':<17} {'—':>9}  {buy.quantity:>8.2f}  {'—':>10}"
                )
                lines.append(
                    "WARNING: position still open at end of run — no exit recorded."
                )

    lines.append(_SEP)
    lines.append("")

    # Stats
    if not pairs or all(s is None for _, s in pairs):
        for label in ("Max drawdown:", "Largest win:", "Largest loss:", "Win rate:"):
            lines.append(f"{label:<15} N/A")
        return lines

    pnl_values = [
        (sell.price - buy.price) * buy.quantity
        for buy, sell in pairs
        if sell is not None
    ]
    wins   = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p <= 0]
    total  = len(pnl_values)

    max_dd = _max_drawdown(result, bars)
    dd_str = f"-{max_dd:.2f}%" if max_dd is not None and max_dd > 0 else "0.00%"

    largest_win  = max(pnl_values) if pnl_values else 0.0
    largest_loss = min(pnl_values) if pnl_values else 0.0
    win_rate_pct = len(wins) / total * 100 if total else 0.0

    lines.append(f"Max drawdown:  {dd_str}")
    lines.append(
        f"Largest win:   +${largest_win:,.2f}"
        if largest_win >= 0
        else f"Largest win:   -${abs(largest_win):,.2f}"
    )
    lines.append(
        f"Largest loss:  -${abs(largest_loss):,.2f}"
        if largest_loss <= 0
        else f"Largest loss:  +${largest_loss:,.2f}"
    )
    lines.append(f"Win rate:      {len(wins)} / {total}  ({win_rate_pct:.1f}%)")

    return lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pivot_triplet(pivots: List[float], close: float) -> str:
    if not pivots:
        return "p-1:—     p:—       p+1:—    "
    i = min(range(len(pivots)), key=lambda k: abs(pivots[k] - close))
    prev = f"{pivots[i - 1]:.2f}" if i > 0           else "—"
    curr = f"{pivots[i]:.2f}"
    nxt  = f"{pivots[i + 1]:.2f}" if i < len(pivots) - 1 else "—"
    return f"p-1:{prev:<8} p:{curr:<8} p+1:{nxt:<8}"


def _to_est(dt: datetime) -> datetime:
    return dt.astimezone(_EST)


def _order_label(order: BrokerOrder) -> str:
    if order.order_type == "MARKET":
        return f"MARKET_SELL qty:{order.quantity:g}"
    if order.order_type == "STOP_LIMIT":
        return f"STOP_LIMIT@{order.limit_price:.2f} qty:{order.quantity:g}"
    # LIMIT
    if order.side == "BUY":
        return f"BUY_LIMIT@{order.limit_price:.2f} qty:{order.quantity:g}"
    return f"SELL_LIMIT@{order.limit_price:.2f} qty:{order.quantity:g}"


def _max_drawdown(result: "RunResult", bars: List[OHLCVBar]) -> float | None:
    """
    Compute max drawdown as a percentage of peak NAV.

    NAV at each bar = cash_after_last_fill + shares_after_last_fill * bar.close.
    Returns None if there are no ledger entries.
    """
    if not result.ledger:
        return None

    # Map confirmation_id → ledger entry for O(1) lookup
    conf_to_entry: Dict[str, LedgerEntry] = {
        e.confirmation_id: e for e in result.ledger
    }

    # Build a sorted iterator of (bar_index, conf) to apply fills in order
    sorted_execs = sorted(result.execution_log, key=lambda x: x[0])
    exec_iter: Iterator[Tuple[int, ExecutionConfirmation]] = iter(sorted_execs)
    next_exec = next(exec_iter, None)

    current_cash   = result.initial_cash
    current_shares = 0.0
    peak_nav       = current_cash
    max_dd         = 0.0

    for t, bar in enumerate(bars):
        # Apply all fills processed at bar t
        while next_exec is not None and next_exec[0] == t:
            _, conf = next_exec
            if conf.confirmation_id in conf_to_entry:
                entry = conf_to_entry[conf.confirmation_id]
                current_cash   = entry.cash_after
                current_shares = entry.shares_after
            next_exec = next(exec_iter, None)

        nav = current_cash + current_shares * bar.close
        if nav > peak_nav:
            peak_nav = nav
        if peak_nav > 0:
            dd = (peak_nav - nav) / peak_nav * 100
            if dd > max_dd:
                max_dd = dd

    return max_dd

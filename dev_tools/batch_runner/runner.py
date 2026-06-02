"""
Batch Runner (Dev / Testing Only).

Responsibility:
    Runs a full simulated trading session over a historical date range.
    Slices the long-term OHLCV DB to obtain a 3-month lookback window (for pivot
    warm-up) plus the test window. Replays the test window bar-by-bar in time order,
    maintaining MachineStatus, routing orders to TestBus, and accumulating the ledger.

    Conditional stop-losses (condition="on_fill:<buy_id>") are held in a local
    dict and only dispatched to TestBus once their paired BUY order fills.

    Parallelism: may run multiple (ticker, start, end) test cases concurrently using
    concurrent.futures or similar. Each run has its own independent MachineStatus and
    Bookkeeper instance.

Public interface:
    RunResult                   — return-value dataclass
    run_batch(ticker, start_date, end_date, initial_cash, _override_bars) -> RunResult
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List

from src.analyst.analyst import analyse
from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import PIVOT_INTERVAL_BARS, PIVOT_LOOKBACK_DAYS, VWAP_WINDOW_BARS
from src.models import BrokerOrder, ExecutionConfirmation, LedgerEntry, MachineStatus, OHLCVBar
from src.pivot.pivot_calc import build_pivots
from src.scriber.db import fetch_bars
from src.trader.trader import process
from dev_tools.batch_runner.pivot_cache import load_cache, save_cache
from dev_tools.test_api.test_bus import TestBus

_logger = logging.getLogger(__name__)
_EST = ZoneInfo("America/New_York")


@dataclass
class RunResult:
    ticker:        str
    start_date:    date
    end_date:      date
    initial_cash:  float
    ledger:        List[LedgerEntry]
    bars_processed: int
    final_nav:     float
    order_log:       List[tuple[int, BrokerOrder]]            = field(default_factory=list)
    execution_log:   List[tuple[int, ExecutionConfirmation]]  = field(default_factory=list)
    pivot_snapshots: Dict[int, List[float]]                   = field(default_factory=dict)
    pivot_log:       Dict[int, List[float]]                   = field(default_factory=dict)


def run_batch(
    ticker: str,
    start_date: date,
    end_date: date,
    initial_cash: float = 10_000.0,
    _override_bars: tuple[list[OHLCVBar], list[OHLCVBar]] | None = None,
    db_path: str | None = None,
) -> RunResult:
    """
    Execute a simulated trading session and return the run result.

    Args:
        ticker:          Ticker symbol.
        start_date:      Start of the test window. Bars before this provide pivot warm-up.
        end_date:        End of the test window (inclusive).
        initial_cash:    Starting cash for the session.
        _override_bars:  (lookback_bars, test_window) injected directly — skips DB fetch
                         and pivot cache. For testing only; must not affect production.
        db_path:         Override the SQLite DB path (defaults to config.DB_PATH).

    Returns:
        RunResult with the full ledger, bar count, final NAV, and order/execution logs.
    """
    # ------------------------------------------------------------------
    # Step 1: Obtain lookback window and test window bars
    # ------------------------------------------------------------------
    if _override_bars is not None:
        lookback_bars, test_window = _override_bars
    else:
        lookback_start = datetime.combine(
            start_date - timedelta(days=PIVOT_LOOKBACK_DAYS), time.min, tzinfo=timezone.utc
        )
        lookback_end = datetime.combine(
            start_date - timedelta(days=1), time.max, tzinfo=timezone.utc
        )
        test_start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        test_end = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
        lookback_bars = fetch_bars(ticker, lookback_start, lookback_end, db_path=db_path)
        test_window = fetch_bars(ticker, test_start, test_end, db_path=db_path)

    if not test_window:
        return RunResult(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            ledger=[],
            bars_processed=0,
            final_nav=initial_cash,
        )

    # ------------------------------------------------------------------
    # Step 2: Build or load pivot cache
    # ------------------------------------------------------------------
    if _override_bars is not None:
        # Always recompute for test runs to avoid stale on-disk state
        pivot_cache_data = _build_pivot_cache(lookback_bars, test_window)
    else:
        pivot_cache_data = load_cache(ticker, start_date, end_date)
        if pivot_cache_data is None:
            pivot_cache_data = _build_pivot_cache(lookback_bars, test_window)
            save_cache(ticker, start_date, end_date, pivot_cache_data)

    # ------------------------------------------------------------------
    # Step 3: Initialise session state
    # ------------------------------------------------------------------
    bookkeeper = Bookkeeper(initial_cash)
    status = MachineStatus(
        position="IDLE",
        active_stop_price=None,
        initial_nav=initial_cash,
        daily_start_nav=initial_cash,
        session_date=start_date,
        pending_orders={},
        bar_count=0,
    )
    test_bus = TestBus(test_window)
    # buy_order_id → stop-loss limit price (warehoused until BUY fills)
    pending_stop_losses: Dict[str, float] = {}
    current_pivots: list[float] = []

    order_log:     list[tuple[int, BrokerOrder]]           = []
    execution_log: list[tuple[int, ExecutionConfirmation]] = []
    pivot_log:     Dict[int, List[float]]                  = {}

    # ------------------------------------------------------------------
    # Step 4: Bar-by-bar simulation loop
    # ------------------------------------------------------------------
    for t, bar in enumerate(test_window):

        # 4a: Inject pivot snapshot at each 30-bar checkpoint
        if t % PIVOT_INTERVAL_BARS == 0:
            current_pivots = pivot_cache_data.get(t, [])
            pivot_log[t] = current_pivots  # record checkpoint reset

        # 4b: Collect recent bars for VWAP
        recent_bars = test_window[max(0, t - VWAP_WINDOW_BARS + 1): t + 1]

        # 4c: Poll pending orders for fills that have arrived by this bar
        for order_id in list(status.pending_orders):
            conf = test_bus.get_confirmation(order_id)
            if conf is None or conf.filled_at > bar.timestamp:
                continue
            bookkeeper.receive_confirmation(conf)
            execution_log.append((t, conf))
            del status.pending_orders[order_id]

            if conf.side == "BUY":
                status.position = "LONG"
                if order_id in pending_stop_losses:
                    stop_price = pending_stop_losses.pop(order_id)
                    status.active_stop_price = stop_price
                    stop_order = BrokerOrder(
                        order_id=str(uuid.uuid4()),
                        ticker=ticker,
                        side="SELL",
                        order_type="STOP_LIMIT",
                        limit_price=stop_price,
                        quantity=conf.filled_quantity,
                        condition=None,
                        created_at=datetime.now(timezone.utc),
                    )
                    test_bus.send_order(stop_order, current_bar_index=t)
                    status.pending_orders[stop_order.order_id] = "SELL"
                    order_log.append((t, stop_order))
                else:
                    status.active_stop_price = None

            elif conf.side == "SELL":
                status.position = "IDLE"
                status.active_stop_price = None

        # 4d: Day-boundary reset — reset daily_start_nav at the first bar of each new calendar day
        bar_date = bar.timestamp.astimezone(_EST).date()
        if bar_date != status.session_date:
            status.daily_start_nav = bookkeeper.available_cash()
            status.session_date = bar_date

        # 4e: Run analyst; persist returned pivots (may include virtual OOS extensions)
        analyst_orders, returned_pivots = analyse(
            bar, status, current_pivots, recent_bars, bookkeeper, recalc_hook=None
        )
        if returned_pivots is not current_pivots:
            current_pivots = returned_pivots
            pivot_log[t] = current_pivots  # overwrite checkpoint entry if both happened

        # Capture warehoused data from analyst orders before translation.
        # Conditional SELL_STOP: warehouse stop price — trader skips these, coordinator
        # sends the real order with correct quantity once the BUY fill arrives (step 4c).
        # UPDATE_STOPLOSS / recovery SELL_STOP: update active_stop_price immediately so
        # the next bar's analyst sees the current ratchet level.
        for ao in analyst_orders:
            if ao.type == "SELL_STOP" and ao.condition and ao.condition.startswith("on_fill:"):
                pending_stop_losses[ao.condition[len("on_fill:"):]] = ao.price
            elif ao.type == "UPDATE_STOPLOSS":
                status.active_stop_price = ao.price
            elif ao.type == "SELL_STOP":
                status.active_stop_price = ao.price  # recovery stop (non-conditional)

        # 4f: Translate to broker orders
        broker_orders = process(analyst_orders, status, bookkeeper, ticker)

        # 4g: Route each broker order to the bus or to local holding structures
        for broker_order in broker_orders:
            if broker_order.order_type == "CANCEL":
                test_bus.cancel_order(broker_order.order_id)
                status.pending_orders.pop(broker_order.order_id, None)
                if broker_order.side == "BUY":
                    pending_stop_losses.pop(broker_order.order_id, None)

            else:
                # Before sending a market sell, cancel any pending stop-losses so
                # they cannot fill after the position has been closed.
                if broker_order.order_type == "MARKET" and broker_order.side == "SELL":
                    for oid in [o for o, s in list(status.pending_orders.items()) if s == "SELL"]:
                        test_bus.cancel_order(oid)
                        del status.pending_orders[oid]

                test_bus.send_order(broker_order, current_bar_index=t)
                status.pending_orders[broker_order.order_id] = broker_order.side
                order_log.append((t, broker_order))

        status.bar_count += 1

    # ------------------------------------------------------------------
    # Step 5: Compute final NAV and return
    # ------------------------------------------------------------------
    last_bar = test_window[-1]
    final_nav = bookkeeper.current_nav(last_bar.close)

    result = RunResult(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        ledger=bookkeeper.get_ledger(),
        bars_processed=len(test_window),
        final_nav=final_nav,
        order_log=order_log,
        execution_log=execution_log,
        pivot_snapshots=pivot_cache_data,
        pivot_log=pivot_log,
    )

    # ------------------------------------------------------------------
    # Step 6: Write report (local import avoids circular dependency)
    # ------------------------------------------------------------------
    from dev_tools.report.writer import write_report  # noqa: PLC0415
    report_path = write_report(result, test_window)
    _logger.info("Report written to %s", report_path)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_pivot_cache(
    lookback_bars: list[OHLCVBar],
    test_window: list[OHLCVBar],
) -> Dict[int, list[float]]:
    """
    Pre-compute pivot snapshots at every PIVOT_INTERVAL_BARS checkpoint.

    At checkpoint c the input is (lookback_bars + test_window[:c]), reflecting
    all bars known to the system at that point in time.
    """
    cache: Dict[int, list[float]] = {}
    for c in range(0, len(test_window), PIVOT_INTERVAL_BARS):
        input_bars = lookback_bars + test_window[:c]
        cache[c] = build_pivots(input_bars)
    return cache

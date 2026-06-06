"""
Entry Interface — Streaming Mode.

Responsibility:
    Called by the live data feed once per closed 1-minute bar. Orchestrates
    the full per-bar pipeline: scriber → pivot builder (every 30 bars) →
    analyst → trader → broker bus. Maintains all session state internally.

    Conditional stop-losses (condition="on_fill:<buy_id>") are held in a local
    dict and dispatched to the bus only once their paired BUY order fills,
    using identical coordinator logic to dev_tools/batch_runner/runner.py.

Public interface:
    TradingSession(ticker, initial_cash, bus, db_path) — one instance per session
    TradingSession.on_bar(bar: OHLCVBar) -> None     — called once per bar
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from src.analyst.analyst import analyse
from src.bookkeeper.bookkeeper import Bookkeeper
from src.bus.protocol import BrokerBusProtocol
from src.config import (
    DAILY_LOSS_LIMIT,
    DB_PATH,
    ENTRY_STOP_FLOOR_PCT,
    PIVOT_INTERVAL_BARS,
    PIVOT_LOOKBACK_DAYS,
    STOP_TRADING_TIME,
    VWAP_WINDOW_BARS,
)
from src.logger.trading_logger import TradingLogger
from src.models import BrokerOrder, ExecutionConfirmation, MachineStatus, OHLCVBar
from src.pivot.pivot_calc import build_pivots
from src.scriber import db as _db
from src.scriber.scriber import write_bar
from src.trader.trader import process

_logger = logging.getLogger(__name__)
_EST = ZoneInfo("America/New_York")


class TradingSession:
    """
    Manages one live streaming trading session.

    All session state is owned here; on_bar() is the sole entry point.
    The instance must not be shared across threads.
    """

    def __init__(
        self,
        ticker: str,
        initial_cash: float,
        bus: BrokerBusProtocol,
        db_path: str = DB_PATH,
        push_confirmations: bool = False,
    ) -> None:
        self._ticker = ticker
        self._bus = bus
        self._db_path = db_path
        self._push_confirmations = push_confirmations

        self._bookkeeper = Bookkeeper(initial_cash)
        self._status = MachineStatus(
            position="IDLE",
            active_stop_price=None,
            initial_nav=initial_cash,
            daily_start_nav=initial_cash,
            session_date=datetime.now(_EST).date(),
            pending_orders={},
            bar_count=0,
        )
        # buy_order_id → stop-loss limit price; held until paired BUY fills
        self._pending_stop_losses: Dict[str, float] = {}
        # internal UUID → broker-assigned UUID; built at send time
        self._id_map: Dict[str, str] = {}
        # broker_order_id → latest cumulative partial-fill conf; overwritten on each event
        self._partial_fill_staging: Dict[str, ExecutionConfirmation] = {}
        # broker_order_id → entry-band stop price; mirrored from _pending_stop_losses at
        # dispatch time so notify_cancel can find it even after _id_map is cleaned up
        self._entry_stop_by_broker_id: Dict[str, float] = {}

        self._tlog = TradingLogger(ticker=ticker, initial_cash=initial_cash)
        # Flags to emit DAILY_LOSS_LIMIT_HIT / TIME_LIMIT_HIT only on first occurrence per day
        self._daily_loss_logged: bool = False
        self._time_limit_logged: bool = False

        # Seed pivot list from whatever history is already in the DB.
        self._current_pivots: List[float] = self._build_pivots_from_db()
        if not self._current_pivots:
            _logger.warning(
                "No historical bars in DB at session start for %s — "
                "pivot list is empty; analyst empty-pivot guard will apply "
                "until the first 30-bar checkpoint.",
                ticker,
            )
        else:
            _logger.info(
                "Initial pivots built for %s: %d levels", ticker, len(self._current_pivots)
            )
        self._tlog.log_pivots_recalculated(
            self._current_pivots, 0, self._status, self._bookkeeper
        )

    # ------------------------------------------------------------------
    # Main callback
    # ------------------------------------------------------------------

    def on_bar(self, bar: OHLCVBar) -> None:
        """Process one incoming live bar through the full pipeline."""

        # Step 1 — Persist bar to the long-term DB
        write_bar(bar, db_path=self._db_path)
        self._tlog.log_bar_received(bar, self._status, self._bookkeeper)

        # Step 2 — Increment bar counter
        self._status.bar_count += 1

        # Step 3 — Scheduled pivot rebuild every PIVOT_INTERVAL_BARS
        if self._status.bar_count % PIVOT_INTERVAL_BARS == 0:
            fresh = self._build_pivots_from_db()
            if fresh:
                self._current_pivots = fresh
                _logger.info(
                    "Pivots rebuilt at bar %d for %s: %d levels",
                    self._status.bar_count, self._ticker, len(fresh),
                )
                self._tlog.log_pivots_recalculated(
                    fresh, self._status.bar_count, self._status, self._bookkeeper
                )
            else:
                _logger.warning(
                    "Pivot rebuild at bar %d returned empty list for %s",
                    self._status.bar_count, self._ticker,
                )

        # Step 4 — Collect recent bars for VWAP (current bar is already in DB)
        recent_bars = self._fetch_recent_bars(bar)

        # Step 5 — Build recalc_hook: on-demand pivot rebuild for OOS handling
        def recalc_hook() -> List[float]:
            return self._build_pivots_from_db()

        # Step 6 — Run analyst; persist returned pivots (may include virtual OOS extensions)
        analyst_orders, returned_pivots = analyse(
            bar,
            self._status,
            self._current_pivots,
            recent_bars,
            self._bookkeeper,
            recalc_hook,
        )
        if returned_pivots is not self._current_pivots:
            self._current_pivots = returned_pivots

        # [Logging] Compute VWAP and closest-pivot indices for SIGNAL_EVALUATED record
        _log_vwap, _log_vwap_idx, _log_close_idx = 0.0, -1, -1
        if recent_bars and returned_pivots:
            _tpvol = sum((b.high + b.low + b.close) / 3 * b.volume for b in recent_bars)
            _tvol = sum(b.volume for b in recent_bars)
            if _tvol > 0:
                _log_vwap = _tpvol / _tvol
                _log_vwap_idx = min(
                    range(len(returned_pivots)),
                    key=lambda i: abs(returned_pivots[i] - _log_vwap),
                )
                _log_close_idx = min(
                    range(len(returned_pivots)),
                    key=lambda i: abs(returned_pivots[i] - bar.close),
                )
        self._tlog.log_signal_evaluated(
            bar, self._status, self._bookkeeper,
            analyst_orders, returned_pivots,
            _log_vwap, _log_vwap_idx, _log_close_idx,
        )

        # [Logging] First-occurrence detection for IDLE stop conditions
        if self._status.position == "IDLE" and not analyst_orders:
            _bar_time_est = bar.timestamp.astimezone(_EST).time()
            try:
                _avail = self._bookkeeper.available_cash()
            except ValueError:
                _avail = 0.0
            if (
                _avail < self._status.daily_start_nav * (1 - DAILY_LOSS_LIMIT)
                and not self._daily_loss_logged
            ):
                self._tlog.log_daily_loss_limit_hit(
                    _avail - self._status.daily_start_nav,
                    self._status.daily_start_nav * DAILY_LOSS_LIMIT,
                    self._status, self._bookkeeper,
                )
                self._daily_loss_logged = True
            elif _bar_time_est > STOP_TRADING_TIME and not self._time_limit_logged:
                self._tlog.log_time_limit_hit(
                    _bar_time_est, STOP_TRADING_TIME, self._status, self._bookkeeper
                )
                self._time_limit_logged = True

        # Step 7 — Translate analyst instructions to broker orders
        broker_orders = process(analyst_orders, self._status, self._bookkeeper, self._ticker)

        # Warehouse conditional stop-losses and track active_stop_price updates
        # before routing, so state is correct if confirmations arrive this bar.
        for ao in analyst_orders:
            if ao.type == "SELL_STOP" and ao.condition and ao.condition.startswith("on_fill:"):
                self._pending_stop_losses[ao.condition[len("on_fill:"):]] = ao.price
            elif ao.type == "UPDATE_STOPLOSS":
                _old_stop = self._status.active_stop_price
                self._status.active_stop_price = ao.price
                self._tlog.log_stop_updated(
                    _old_stop, ao.price, "ratchet", self._status, self._bookkeeper
                )
            elif ao.type == "SELL_STOP":
                # Recovery stop-loss (non-conditional): active immediately
                self._status.active_stop_price = ao.price

        # Step 8 — Route broker orders to the bus
        for broker_order in broker_orders:
            if broker_order.order_type == "CANCEL":
                broker_id = self._id_map.get(broker_order.order_id, broker_order.order_id)
                self._bus.cancel_order(broker_id)
                self._status.pending_orders.pop(broker_id, None)
                if broker_order.side == "BUY":
                    self._pending_stop_losses.pop(broker_order.order_id, None)
                    self._id_map.pop(broker_order.order_id, None)
                    # SELL CANCEL is internal UPDATE_STOPLOSS mechanics; not logged separately
                    self._tlog.log_order_cancelled(
                        broker_id, "cancel_all_buys", self._status, self._bookkeeper
                    )
            else:
                # Before sending a market sell, cancel any pending stop-losses so
                # they cannot fill after the position has been closed.
                if broker_order.order_type == "MARKET" and broker_order.side == "SELL":
                    for oid in [o for o, s in list(self._status.pending_orders.items()) if s == "SELL"]:
                        self._bus.cancel_order(oid)
                        del self._status.pending_orders[oid]
                        self._tlog.log_order_cancelled(
                            oid, "superseded_by_market_sell", self._status, self._bookkeeper
                        )

                broker_id = self._bus.send_order(broker_order)
                self._id_map[broker_order.order_id] = broker_id
                self._status.pending_orders[broker_id] = broker_order.side
                # Mirror entry stop price keyed by broker UUID so notify_cancel
                # can retrieve it even after _id_map has been cleaned up.
                if (broker_order.side == "BUY"
                        and broker_order.order_id in self._pending_stop_losses):
                    self._entry_stop_by_broker_id[broker_id] = (
                        self._pending_stop_losses[broker_order.order_id]
                    )
                _logger.debug(
                    "Order sent: %s %s order_id=%s broker_id=%s limit=%.4f qty=%.4f",
                    broker_order.side,
                    broker_order.order_type,
                    broker_order.order_id,
                    broker_id,
                    broker_order.limit_price or 0.0,
                    broker_order.quantity,
                )
                if broker_order.order_type == "STOP":
                    self._tlog.log_stop_submitted(
                        broker_order, broker_id, self._status, self._bookkeeper
                    )
                else:
                    self._tlog.log_order_submitted(
                        broker_order, broker_id, self._status, self._bookkeeper
                    )

        # Steps 9–11 — Poll all pending orders for fills; update status accordingly
        self._process_confirmations()

        # Day-boundary reset — re-anchor daily_start_nav on the first bar of a new EST day
        bar_date = bar.timestamp.astimezone(_EST).date()
        if bar_date != self._status.session_date:
            self._status.daily_start_nav = self._bookkeeper.available_cash()
            self._status.session_date = bar_date
            _logger.info(
                "New trading day %s: daily_start_nav reset to %.2f",
                bar_date, self._status.daily_start_nav,
            )
            self._daily_loss_logged = False
            self._time_limit_logged = False
            self._tlog.on_new_day()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_confirmations(self) -> None:
        """
        Poll the bus for fills on every pending order; update position and
        trigger conditional stop-loss activation on BUY fills.
        Skipped when push_confirmations=True (AlpacaTradeUpdateStream drives delivery).
        """
        if self._push_confirmations:
            return
        for order_id in list(self._status.pending_orders):
            conf = self._bus.get_confirmation(order_id)
            if conf is None:
                continue
            self._handle_confirmation(order_id, conf)

    def notify_fill(self, conf: ExecutionConfirmation) -> None:
        """
        Called by AlpacaTradeUpdateStream when a full fill arrives via push.
        Only used when push_confirmations=True.
        The order_id in conf is the broker-assigned UUID.
        """
        if conf.order_id not in self._status.pending_orders:
            _logger.warning(
                "notify_fill: unknown order_id %s — already processed or not sent by this session",
                conf.order_id,
            )
            return
        # A full fill supersedes any staged partial fills; clear both staging dicts.
        self._partial_fill_staging.pop(conf.order_id, None)
        self._entry_stop_by_broker_id.pop(conf.order_id, None)
        self._handle_confirmation(conf.order_id, conf)

    def stage_partial_fill(self, conf: ExecutionConfirmation) -> None:
        """
        Called by AlpacaTradeUpdateStream on each partial_fill event.
        Overwrites any previous staging for the same order_id; Alpaca reports
        filled_qty as a cumulative total, so the latest event is always correct.

        Also applies the entry stop floor to _entry_stop_by_broker_id so that
        notify_cancel always dispatches the floored stop price, not the raw
        pivot-midpoint.
        """
        self._partial_fill_staging[conf.order_id] = conf
        # Re-compute effective stop using the latest cumulative fill price.
        current_stop = self._entry_stop_by_broker_id.get(conf.order_id)
        if current_stop is not None:
            floor_stop = conf.filled_price * (1 - ENTRY_STOP_FLOOR_PCT)
            self._entry_stop_by_broker_id[conf.order_id] = min(current_stop, floor_stop)
        _logger.info(
            "Trade update — partial_fill %s: %.4f shares @ %.4f (order_id=%s) — staged",
            conf.side, conf.filled_quantity, conf.filled_price, conf.order_id,
        )
        self._tlog.log_partial_fill(conf, self._status, self._bookkeeper)

    def notify_cancel(self, broker_order_id: str) -> None:
        """
        Called by AlpacaTradeUpdateStream when a canceled/rejected/expired event arrives.

        If the canceled order was a BUY with staged partial fills:
          - Commits the acquired shares to the bookkeeper.
          - Transitions position to LONG.
          - Sets active_stop_price to the entry-band midpoint stored at dispatch time.
          - Emits a SELL_STOP to the broker for the actual shares acquired.

        If no partial fills were staged (order died before any fill), stays IDLE.
        """
        self._tlog.log_order_cancelled(
            broker_order_id, "broker_cancel", self._status, self._bookkeeper
        )
        self._status.pending_orders.pop(broker_order_id, None)
        staged = self._partial_fill_staging.pop(broker_order_id, None)
        stop_price = self._entry_stop_by_broker_id.pop(broker_order_id, None)

        if staged is None or staged.side != "BUY" or staged.filled_quantity == 0.0:
            return

        # Shares were acquired before the cancel — commit and protect with a stop-loss.
        self._bookkeeper.receive_confirmation(staged)
        self._status.position = "LONG"
        cash_spent = staged.filled_quantity * staged.filled_price
        _logger.info(
            "Canceled BUY with partial fills committed: order_id=%s shares=%.4f "
            "cash_spent=%.2f stop_price=%s",
            broker_order_id, staged.filled_quantity, cash_spent,
            f"{stop_price:.4f}" if stop_price is not None else "None",
        )

        if stop_price is None:
            _logger.warning(
                "notify_cancel: no entry stop price found for order_id=%s — "
                "stop-loss not issued; position is unprotected",
                broker_order_id,
            )
            self._status.active_stop_price = None
            return

        self._status.active_stop_price = stop_price
        stop_order = BrokerOrder(
            order_id=str(uuid.uuid4()),
            ticker=self._ticker,
            side="SELL",
            order_type="STOP",
            limit_price=None,
            quantity=staged.filled_quantity,
            condition=None,
            created_at=datetime.now(timezone.utc),
            stop_price=stop_price,
        )
        broker_id = self._bus.send_order(stop_order)
        self._id_map[stop_order.order_id] = broker_id
        self._status.pending_orders[broker_id] = "SELL"
        self._tlog.log_stop_submitted(stop_order, broker_id, self._status, self._bookkeeper)

    def _handle_confirmation(self, order_id: str, conf: ExecutionConfirmation) -> None:
        """Process a single confirmed fill: update bookkeeper, position, and activate stop-loss."""
        self._bookkeeper.receive_confirmation(conf)
        del self._status.pending_orders[order_id]
        # Reverse-lookup internal UUID before pruning (order_id is the broker UUID)
        _reverse = {v: k for k, v in self._id_map.items()}
        internal_id = _reverse.get(order_id, order_id)  # fallback: no-remap case (test/batch)
        # Remove from id_map to prevent unbounded growth in long sessions
        self._id_map = {k: v for k, v in self._id_map.items() if v != order_id}
        _logger.info(
            "Confirmation: %s filled %.4f shares @ %.4f (order_id=%s)",
            conf.side, conf.filled_quantity, conf.filled_price, order_id,
        )

        if conf.side == "BUY":
            self._status.position = "LONG"
            if internal_id in self._pending_stop_losses:
                analyst_stop = self._pending_stop_losses.pop(internal_id)
                effective_stop = min(analyst_stop, conf.filled_price * (1 - ENTRY_STOP_FLOOR_PCT))
                self._status.active_stop_price = effective_stop
                stop_order = BrokerOrder(
                    order_id=str(uuid.uuid4()),
                    ticker=self._ticker,
                    side="SELL",
                    order_type="STOP",
                    limit_price=None,
                    quantity=conf.filled_quantity,
                    condition=None,
                    created_at=datetime.now(timezone.utc),
                    stop_price=effective_stop,
                )
                broker_id = self._bus.send_order(stop_order)
                self._id_map[stop_order.order_id] = broker_id
                self._status.pending_orders[broker_id] = "SELL"
                _logger.info(
                    "Stop-loss activated: analyst_stop=%.4f effective_stop=%.4f "
                    "fill=%.4f qty=%.4f order_id=%s",
                    analyst_stop, effective_stop, conf.filled_price,
                    conf.filled_quantity, stop_order.order_id,
                )
                self._tlog.log_stop_submitted(stop_order, broker_id, self._status, self._bookkeeper)
            else:
                self._status.active_stop_price = None
            self._tlog.log_fill_received(conf, self._status, self._bookkeeper)

        elif conf.side == "SELL":
            self._tlog.log_sell_filled(conf, self._status, self._bookkeeper)
            self._status.position = "IDLE"
            self._status.active_stop_price = None

    def reconcile(self, broker_cash: float, broker_shares: float) -> None:
        """Delegate periodic broker reconciliation to the bookkeeper."""
        self._bookkeeper.reconcile(broker_cash, broker_shares, self._ticker)

    def handle_buy_partial_fill(self, conf: ExecutionConfirmation) -> None:
        """
        Called by AlpacaTradeUpdateStream on a partial_fill event for a BUY order.

        Commits the shares already acquired, cancels the unfilled remainder, transitions
        to LONG, and emits a SELL_STOP sized to the actual filled quantity.

        If the order is no longer in pending_orders (already handled), this is a no-op.
        """
        broker_order_id = conf.order_id

        if broker_order_id not in self._status.pending_orders:
            _logger.warning(
                "handle_buy_partial_fill: order %s not in pending_orders — "
                "already handled or unknown; ignoring",
                broker_order_id,
            )
            return

        _logger.info(
            "BUY partial fill: %.4f shares @ %.4f (order_id=%s) — "
            "committing and canceling unfilled remainder",
            conf.filled_quantity, conf.filled_price, broker_order_id,
        )
        self._tlog.log_partial_fill(conf, self._status, self._bookkeeper)

        # Commit acquired shares to the bookkeeper
        self._bookkeeper.receive_confirmation(conf)

        # Clear staging so the forthcoming cancel event via notify_cancel is a no-op
        self._partial_fill_staging.pop(broker_order_id, None)

        # Remove BUY from pending_orders now (cancel event will find nothing to clean up)
        self._status.pending_orders.pop(broker_order_id, None)

        # Cancel the unfilled remainder; Alpaca will push a "canceled" event which
        # notify_cancel handles safely (staging cleared above → early return)
        self._bus.cancel_order(broker_order_id)

        # Retrieve entry-band stop price mirrored at dispatch time
        stop_price = self._entry_stop_by_broker_id.pop(broker_order_id, None)

        # Clean up internal ID mapping
        _reverse = {v: k for k, v in self._id_map.items()}
        internal_id = _reverse.get(broker_order_id, broker_order_id)
        self._pending_stop_losses.pop(internal_id, None)
        self._id_map = {k: v for k, v in self._id_map.items() if v != broker_order_id}

        self._status.position = "LONG"

        if stop_price is None:
            _logger.warning(
                "handle_buy_partial_fill: no entry stop price for order_id=%s — "
                "position unprotected",
                broker_order_id,
            )
            self._status.active_stop_price = None
            return

        self._status.active_stop_price = stop_price
        stop_order = BrokerOrder(
            order_id=str(uuid.uuid4()),
            ticker=self._ticker,
            side="SELL",
            order_type="STOP",
            limit_price=None,
            quantity=conf.filled_quantity,
            condition=None,
            created_at=datetime.now(timezone.utc),
            stop_price=stop_price,
        )
        broker_id = self._bus.send_order(stop_order)
        self._id_map[stop_order.order_id] = broker_id
        self._status.pending_orders[broker_id] = "SELL"
        _logger.info(
            "Stop-loss activated (partial fill): price=%.4f qty=%.4f broker_id=%s",
            stop_price, conf.filled_quantity, broker_id,
        )
        self._tlog.log_stop_submitted(stop_order, broker_id, self._status, self._bookkeeper)

    def shutdown(self) -> None:
        """Flush final daily summary and close logger resources. Call on graceful exit."""
        self._tlog.log_daily_summary("session_end", self._status, self._bookkeeper)
        self._tlog.close()

    def _build_pivots_from_db(self) -> List[float]:
        """Fetch PIVOT_LOOKBACK_DAYS of history from DB and compute fresh pivots."""
        now = datetime.now(timezone.utc)
        from_dt = now - timedelta(days=PIVOT_LOOKBACK_DAYS)
        bars = _db.fetch_bars(self._ticker, from_dt, now, db_path=self._db_path)
        if not bars:
            return []
        return build_pivots(bars)

    def _fetch_recent_bars(self, current_bar: OHLCVBar) -> List[OHLCVBar]:
        """
        Return the last VWAP_WINDOW_BARS bars up to and including current_bar.

        current_bar has already been written to the DB before this is called.
        """
        # Query a generous window (2× VWAP_WINDOW_BARS minutes) then take the tail.
        from_dt = current_bar.timestamp - timedelta(minutes=VWAP_WINDOW_BARS * 2)
        bars = _db.fetch_bars(
            self._ticker, from_dt, current_bar.timestamp, db_path=self._db_path
        )
        return bars[-VWAP_WINDOW_BARS:] if len(bars) >= VWAP_WINDOW_BARS else bars
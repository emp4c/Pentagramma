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
from src.config import PIVOT_INTERVAL_BARS, PIVOT_LOOKBACK_DAYS, VWAP_WINDOW_BARS
from src.models import BrokerOrder, MachineStatus, OHLCVBar
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
        db_path: str = "data/bars.db",
    ) -> None:
        self._ticker = ticker
        self._bus = bus
        self._db_path = db_path

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

    # ------------------------------------------------------------------
    # Main callback
    # ------------------------------------------------------------------

    def on_bar(self, bar: OHLCVBar) -> None:
        """Process one incoming live bar through the full pipeline."""

        # Step 1 — Persist bar to the long-term DB
        write_bar(bar, db_path=self._db_path)

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

        # Step 7 — Translate analyst instructions to broker orders
        broker_orders = process(analyst_orders, self._status, self._bookkeeper, self._ticker)

        # Warehouse conditional stop-losses and track active_stop_price updates
        # before routing, so state is correct if confirmations arrive this bar.
        for ao in analyst_orders:
            if ao.type == "SELL_STOP" and ao.condition and ao.condition.startswith("on_fill:"):
                self._pending_stop_losses[ao.condition[len("on_fill:"):]] = ao.price
            elif ao.type == "UPDATE_STOPLOSS":
                self._status.active_stop_price = ao.price
            elif ao.type == "SELL_STOP":
                # Recovery stop-loss (non-conditional): active immediately
                self._status.active_stop_price = ao.price

        # Step 8 — Route broker orders to the bus
        for broker_order in broker_orders:
            if broker_order.order_type == "CANCEL":
                self._bus.cancel_order(broker_order.order_id)
                self._status.pending_orders.pop(broker_order.order_id, None)
                if broker_order.side == "BUY":
                    self._pending_stop_losses.pop(broker_order.order_id, None)
            else:
                # Before sending a market sell, cancel any pending stop-losses so
                # they cannot fill after the position has been closed.
                if broker_order.order_type == "MARKET" and broker_order.side == "SELL":
                    for oid in [o for o, s in list(self._status.pending_orders.items()) if s == "SELL"]:
                        self._bus.cancel_order(oid)
                        del self._status.pending_orders[oid]

                self._bus.send_order(broker_order)
                self._status.pending_orders[broker_order.order_id] = broker_order.side
                _logger.debug(
                    "Order sent: %s %s order_id=%s limit=%.4f qty=%.4f",
                    broker_order.side,
                    broker_order.order_type,
                    broker_order.order_id,
                    broker_order.limit_price or 0.0,
                    broker_order.quantity,
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_confirmations(self) -> None:
        """
        Poll the bus for fills on every pending order; update position and
        trigger conditional stop-loss activation on BUY fills.
        """
        for order_id in list(self._status.pending_orders):
            conf = self._bus.get_confirmation(order_id)
            if conf is None:
                continue

            self._bookkeeper.receive_confirmation(conf)
            del self._status.pending_orders[order_id]
            _logger.info(
                "Confirmation: %s filled %.4f shares @ %.4f (order_id=%s)",
                conf.side, conf.filled_quantity, conf.filled_price, order_id,
            )

            if conf.side == "BUY":
                self._status.position = "LONG"
                if order_id in self._pending_stop_losses:
                    stop_price = self._pending_stop_losses.pop(order_id)
                    self._status.active_stop_price = stop_price
                    stop_order = BrokerOrder(
                        order_id=str(uuid.uuid4()),
                        ticker=self._ticker,
                        side="SELL",
                        order_type="STOP_LIMIT",
                        limit_price=stop_price,
                        quantity=conf.filled_quantity,
                        condition=None,
                        created_at=datetime.now(timezone.utc),
                    )
                    self._bus.send_order(stop_order)
                    self._status.pending_orders[stop_order.order_id] = "SELL"
                    _logger.info(
                        "Stop-loss activated: price=%.4f qty=%.4f order_id=%s",
                        stop_price, conf.filled_quantity, stop_order.order_id,
                    )
                else:
                    self._status.active_stop_price = None

            elif conf.side == "SELL":
                self._status.position = "IDLE"
                self._status.active_stop_price = None

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
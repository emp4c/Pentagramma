"""
TradingLogger — dual-stream trade reporting.

Writes two parallel outputs:
  1. JSONL debug log  outputs/stream/debug_YYYYMMDD.jsonl
     One record per event, appended and flushed immediately.
     Rotates to a new file on EST day boundary.

  2. SQLite trade archive  outputs/stream/trades.db
     Three tables: orders (lifecycle), trades (round-trips), daily_summary.

All I/O is best-effort: exceptions are logged but never propagate to the
trading loop. Thread-safe: a single Lock serialises both JSONL and SQLite
writes (both threads — bar-stream and trade-updates — call into this class).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import DAILY_LOSS_LIMIT, ENTRY_STOP_FLOOR_PCT, MIN_CASH_RESERVE, STOP_TRADING_TIME
from src.models import AnalystOrder, BrokerOrder, ExecutionConfirmation, MachineStatus, OHLCVBar

_LOG = logging.getLogger(__name__)
_EST = ZoneInfo("America/New_York")


class TradingLogger:
    """
    Instantiate once per TradingSession; call log_* methods at the matching
    event points in stream_entry.py.  Call close() on graceful shutdown.
    """

    def __init__(
        self,
        ticker: str,
        initial_cash: float,
        output_dir: str = "outputs/stream",
        db_path: Optional[str] = None,
    ) -> None:
        self._ticker = ticker
        self._lock = threading.Lock()
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------ SQLite
        _db = db_path or os.path.join(output_dir, "trades.db")
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = sqlite3.connect(_db, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        except Exception:
            _LOG.exception("TradingLogger: failed to open SQLite at %s", _db)
            self._conn = None

        # ------------------------------------------------------------------ JSONL
        self._output_dir = output_dir
        self._jsonl_file: Optional[object] = None
        self._jsonl_date: Optional[str] = None
        self._ensure_jsonl_file()

        # ------------------------------------------------------------------ P&L tracking
        self._entry_price: Optional[float] = None
        self._entry_time: Optional[datetime] = None
        self._entry_order_id: Optional[str] = None   # broker UUID of the BUY order
        self._entry_qty: float = 0.0
        # broker_id → {order_type, side, limit_price, qty} for exit_reason lookup
        self._order_meta: Dict[str, dict] = {}

        # ------------------------------------------------------------------ Daily counters
        self._day_pnl: float = 0.0
        self._day_trades: int = 0
        self._day_wins: int = 0

        # Flags to log stop-condition events only once per day
        self._loss_limit_logged: bool = False
        self._time_limit_logged: bool = False

        # Last bar close — used for unrealised P&L in portfolio snapshot
        self._last_close: Optional[float] = None

    # ===================================================================
    # Schema
    # ===================================================================

    def _init_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id         TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                side             TEXT NOT NULL,
                qty              REAL,
                order_type       TEXT,
                limit_price      REAL,
                stop_price       REAL,
                submitted_at     TEXT,
                filled_at        TEXT,
                cancelled_at     TEXT,
                filled_qty       REAL,
                filled_avg_price REAL,
                status           TEXT,
                trade_id         INTEGER
            );
            CREATE TABLE IF NOT EXISTS trades (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol            TEXT NOT NULL,
                entry_time        TEXT,
                entry_price       REAL,
                qty               REAL,
                entry_order_id    TEXT,
                exit_time         TEXT,
                exit_price        REAL,
                exit_order_id     TEXT,
                exit_reason       TEXT,
                gross_pnl         REAL,
                net_pnl           REAL,
                hold_duration_sec REAL
            );
            CREATE TABLE IF NOT EXISTS daily_summary (
                date             TEXT PRIMARY KEY,
                total_trades     INTEGER,
                winning_trades   INTEGER,
                gross_pnl        REAL,
                net_pnl          REAL,
                stop_reason      TEXT
            );
        """)
        self._conn.commit()

    # ===================================================================
    # Internal I/O helpers
    # ===================================================================

    def _today_est(self) -> str:
        return datetime.now(_EST).strftime("%Y%m%d")

    def _ensure_jsonl_file(self) -> None:
        """Open or rotate the JSONL file. Called inside the lock."""
        today = self._today_est()
        if self._jsonl_date == today:
            return
        if self._jsonl_file is not None:
            try:
                self._jsonl_file.close()  # type: ignore[union-attr]
            except Exception:
                pass
        path = os.path.join(self._output_dir, f"debug_{today}.jsonl")
        try:
            self._jsonl_file = open(path, "a", encoding="utf-8")
            self._jsonl_date = today
        except Exception:
            _LOG.exception("TradingLogger: cannot open JSONL file %s", path)
            self._jsonl_file = None

    def _write_jsonl(self, record: dict) -> None:
        with self._lock:
            try:
                self._ensure_jsonl_file()
                if self._jsonl_file is not None:
                    self._jsonl_file.write(json.dumps(record, default=str) + "\n")  # type: ignore[union-attr]
                    self._jsonl_file.flush()  # type: ignore[union-attr]
            except Exception:
                _LOG.exception("TradingLogger: JSONL write failed")

    def _db_write(self, sql: str, params: tuple = ()) -> Optional[int]:
        """Execute one write statement; returns lastrowid or None on failure."""
        if self._conn is None:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
            except Exception:
                _LOG.exception("TradingLogger: DB write failed — %s", sql[:80])
                return None

    def _db_sell_trade(
        self,
        conf: ExecutionConfirmation,
        filled_ts: str,
        gross_pnl: float,
        net_pnl: float,
        hold_sec: Optional[float],
        exit_reason: str,
    ) -> None:
        """Atomic transaction: update sell order + insert trade + link buy order."""
        if self._conn is None:
            return
        with self._lock:
            try:
                c = self._conn
                c.execute(
                    """UPDATE orders
                       SET filled_at=?, filled_qty=?, filled_avg_price=?, status=?
                       WHERE order_id=?""",
                    (filled_ts, conf.filled_quantity, conf.filled_price, "filled", conf.order_id),
                )
                cur = c.execute(
                    """INSERT INTO trades
                       (symbol, entry_time, entry_price, qty, entry_order_id,
                        exit_time, exit_price, exit_order_id, exit_reason,
                        gross_pnl, net_pnl, hold_duration_sec)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self._ticker,
                        self._entry_time.isoformat() if self._entry_time else None,
                        self._entry_price,
                        self._entry_qty,
                        self._entry_order_id,
                        filled_ts,
                        conf.filled_price,
                        conf.order_id,
                        exit_reason,
                        gross_pnl,
                        net_pnl,
                        hold_sec,
                    ),
                )
                trade_id = cur.lastrowid
                if trade_id:
                    if self._entry_order_id:
                        c.execute(
                            "UPDATE orders SET trade_id=? WHERE order_id=?",
                            (trade_id, self._entry_order_id),
                        )
                    c.execute(
                        "UPDATE orders SET trade_id=? WHERE order_id=?",
                        (trade_id, conf.order_id),
                    )
                c.commit()
            except Exception:
                _LOG.exception("TradingLogger: sell-trade DB transaction failed")

    # ===================================================================
    # Portfolio snapshot (included in every JSONL record)
    # ===================================================================

    def _portfolio_snapshot(self, bookkeeper: Bookkeeper) -> dict:
        shares = bookkeeper.shares_held()
        try:
            avail = bookkeeper.available_cash()
            cash = avail + MIN_CASH_RESERVE
        except ValueError:
            cash = 0.0
        unrealised = (
            round(shares * (self._last_close - self._entry_price), 4)
            if (self._entry_price is not None and self._last_close is not None and shares > 0)
            else 0.0
        )
        return {
            "cash": round(cash, 4),
            "position_size": shares,
            "unrealised_pnl": unrealised,
            "realised_pnl_today": round(self._day_pnl, 4),
        }

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ===================================================================
    # Signal decision helper
    # ===================================================================

    def _signal_decision_reason(
        self,
        bar: OHLCVBar,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
        analyst_orders: List[AnalystOrder],
        pivots: List[float],
        vwap: float,
        vwap_idx: int,
        close_idx: int,
    ) -> tuple[str, str]:
        order_types = {ao.type for ao in analyst_orders}
        bar_time = bar.timestamp.astimezone(_EST).time()

        if status.position == "IDLE":
            if not analyst_orders:
                try:
                    avail = bookkeeper.available_cash()
                except ValueError:
                    avail = 0.0
                limit = status.daily_start_nav * (1 - DAILY_LOSS_LIMIT)
                if avail < limit:
                    return "BLOCKED", f"daily_loss_limit: cash={avail:.2f} limit={limit:.2f}"
                if bar_time > STOP_TRADING_TIME:
                    return "BLOCKED", f"post_hours: {bar_time} > {STOP_TRADING_TIME}"
                return "BLOCKED", "unknown_stop_condition"

            if "BUY_LIMIT" in order_types:
                buy = next(ao for ao in analyst_orders if ao.type == "BUY_LIMIT")
                stop = next((ao for ao in analyst_orders if ao.type == "SELL_STOP"), None)
                piv_val = pivots[close_idx] if 0 <= close_idx < len(pivots) else 0.0
                reason = (
                    f"entry_signal: pivot[{close_idx}]={piv_val:.4f}"
                    f" vwap={vwap:.4f} buy_price={buy.price:.4f}"
                )
                if stop:
                    reason += f" stop={stop.price:.4f}"
                return "ENTER", reason

            # CANCEL_ALL_BUYS only — mismatch or edge case
            if 0 <= vwap_idx < len(pivots) and 0 <= close_idx < len(pivots):
                if vwap_idx != close_idx:
                    vp = pivots[vwap_idx]
                    cp = pivots[close_idx]
                    return (
                        "SKIP",
                        f"signal_mismatch: vwap→pivot[{vwap_idx}]={vp:.4f}"
                        f" close→pivot[{close_idx}]={cp:.4f}",
                    )
            return "SKIP", "no_signal_alignment"

        # LONG
        if "SELL_MARKET" in order_types:
            return "EXIT", "eod_market_sell"
        if "UPDATE_STOPLOSS" in order_types:
            upd = next(ao for ao in analyst_orders if ao.type == "UPDATE_STOPLOSS")
            return "RATCHET", f"stop_advanced_to={upd.price:.4f} from={status.active_stop_price}"
        if "SELL_STOP" in order_types:
            return "RECOVER", "stop_reemitted_was_missing"
        return "HOLD", "stop_active_no_band_change"

    # ===================================================================
    # Public log methods — called by stream_entry.py
    # ===================================================================

    def log_bar_received(
        self,
        bar: OHLCVBar,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._last_close = bar.close
        self._write_jsonl({
            "timestamp": bar.timestamp.isoformat(),
            "event_type": "BAR_RECEIVED",
            "symbol": bar.ticker,
            "bar": {
                "open": bar.open, "high": bar.high,
                "low": bar.low, "close": bar.close, "volume": bar.volume,
            },
            "position": status.position,
            "bar_count": status.bar_count,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })

    def log_pivots_recalculated(
        self,
        pivots: List[float],
        bar_count: int,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "PIVOTS_RECALCULATED",
            "symbol": self._ticker,
            "bar_count": bar_count,
            "pivot_count": len(pivots),
            "pivots": [round(p, 4) for p in pivots],
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })

    def log_signal_evaluated(
        self,
        bar: OHLCVBar,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
        analyst_orders: List[AnalystOrder],
        pivots: List[float],
        vwap: float,
        vwap_pivot_idx: int,
        close_pivot_idx: int,
    ) -> None:
        decision, reason = self._signal_decision_reason(
            bar, status, bookkeeper, analyst_orders, pivots,
            vwap, vwap_pivot_idx, close_pivot_idx,
        )
        self._write_jsonl({
            "timestamp": bar.timestamp.isoformat(),
            "event_type": "SIGNAL_EVALUATED",
            "symbol": bar.ticker,
            "position": status.position,
            "vwap": round(vwap, 4),
            "vwap_pivot_idx": vwap_pivot_idx,
            "close_pivot_idx": close_pivot_idx,
            "pivot_count": len(pivots),
            "active_stop_price": status.active_stop_price,
            "orders_emitted": [ao.type for ao in analyst_orders],
            "decision": decision,
            "reason": reason,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })

    def log_order_submitted(
        self,
        broker_order: BrokerOrder,
        broker_id: str,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._order_meta[broker_id] = {
            "order_type": broker_order.order_type,
            "side": broker_order.side,
            "limit_price": broker_order.limit_price,
            "qty": broker_order.quantity,
        }
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "ORDER_SUBMITTED",
            "symbol": self._ticker,
            "order_id": broker_id,
            "side": broker_order.side,
            "qty": broker_order.quantity,
            "order_type": broker_order.order_type,
            "limit_price": broker_order.limit_price,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self._db_write(
            """INSERT OR REPLACE INTO orders
               (order_id, symbol, side, qty, order_type, limit_price, submitted_at, status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                broker_id, self._ticker, broker_order.side, broker_order.quantity,
                broker_order.order_type, broker_order.limit_price, self._ts(), "submitted",
            ),
        )

    def log_order_cancelled(
        self,
        order_id: str,
        reason: str,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._order_meta.pop(order_id, None)
        now = self._ts()
        self._write_jsonl({
            "timestamp": now,
            "event_type": "ORDER_CANCELLED",
            "symbol": self._ticker,
            "order_id": order_id,
            "reason": reason,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self._db_write(
            "UPDATE orders SET cancelled_at=?, status=? WHERE order_id=?",
            (now, "cancelled", order_id),
        )

    def log_partial_fill(
        self,
        conf: ExecutionConfirmation,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        filled_ts = conf.filled_at.isoformat() if conf.filled_at else self._ts()
        self._write_jsonl({
            "timestamp": filled_ts,
            "event_type": "PARTIAL_FILL",
            "symbol": conf.ticker,
            "order_id": conf.order_id,
            "side": conf.side,
            "filled_qty": conf.filled_quantity,
            "filled_avg_price": conf.filled_price,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })

    def log_fill_received(
        self,
        conf: ExecutionConfirmation,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        """Called when a BUY order is fully filled."""
        self._entry_price = conf.filled_price
        self._entry_time = conf.filled_at
        self._entry_order_id = conf.order_id
        self._entry_qty = conf.filled_quantity
        filled_ts = conf.filled_at.isoformat() if conf.filled_at else self._ts()
        self._write_jsonl({
            "timestamp": filled_ts,
            "event_type": "FILL_RECEIVED",
            "symbol": conf.ticker,
            "order_id": conf.order_id,
            "side": conf.side,
            "filled_qty": conf.filled_quantity,
            "filled_avg_price": conf.filled_price,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self._db_write(
            """UPDATE orders
               SET filled_at=?, filled_qty=?, filled_avg_price=?, status=?
               WHERE order_id=?""",
            (filled_ts, conf.filled_quantity, conf.filled_price, "filled", conf.order_id),
        )

    def log_stop_submitted(
        self,
        stop_order: BrokerOrder,
        broker_id: str,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._order_meta[broker_id] = {
            "order_type": stop_order.order_type,
            "side": stop_order.side,
            "stop_price": stop_order.stop_price,
            "qty": stop_order.quantity,
        }
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "STOP_SUBMITTED",
            "symbol": self._ticker,
            "order_id": broker_id,
            "side": stop_order.side,
            "qty": stop_order.quantity,
            "order_type": stop_order.order_type,
            "stop_price": stop_order.stop_price,
            "entry_stop_floor_pct": ENTRY_STOP_FLOOR_PCT,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self._db_write(
            """INSERT OR REPLACE INTO orders
               (order_id, symbol, side, qty, order_type, limit_price, stop_price,
                submitted_at, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                broker_id, self._ticker, stop_order.side, stop_order.quantity,
                stop_order.order_type, None, stop_order.stop_price,
                self._ts(), "submitted",
            ),
        )

    def log_stop_updated(
        self,
        old_price: float | None,
        new_price: float,
        trigger: str,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "STOP_UPDATED",
            "symbol": self._ticker,
            "old_stop_price": old_price,
            "new_stop_price": new_price,
            "trigger": trigger,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })

    def log_sell_filled(
        self,
        conf: ExecutionConfirmation,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        """Called when a SELL order (stop-loss or market) is fully filled."""
        meta = self._order_meta.pop(conf.order_id, {})
        exit_reason = (
            "stop_hit" if meta.get("order_type") == "STOP" else "time_limit"
        )
        gross_pnl = (
            (conf.filled_price - self._entry_price) * conf.filled_quantity
            if self._entry_price is not None
            else 0.0
        )
        net_pnl = gross_pnl  # fees are 0.0 in current model
        hold_sec: Optional[float] = (
            (conf.filled_at - self._entry_time).total_seconds()
            if conf.filled_at is not None and self._entry_time is not None
            else None
        )
        filled_ts = conf.filled_at.isoformat() if conf.filled_at else self._ts()

        self._day_pnl += gross_pnl
        self._day_trades += 1
        if gross_pnl > 0:
            self._day_wins += 1

        self._write_jsonl({
            "timestamp": filled_ts,
            "event_type": "SELL_FILLED",
            "symbol": conf.ticker,
            "order_id": conf.order_id,
            "exit_price": conf.filled_price,
            "qty": conf.filled_quantity,
            "exit_reason": exit_reason,
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "hold_duration_sec": hold_sec,
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self._db_sell_trade(conf, filled_ts, gross_pnl, net_pnl, hold_sec, exit_reason)

        # Reset per-position tracking
        self._entry_price = None
        self._entry_time = None
        self._entry_order_id = None
        self._entry_qty = 0.0

    def log_daily_loss_limit_hit(
        self,
        daily_pnl: float,
        limit: float,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "DAILY_LOSS_LIMIT_HIT",
            "symbol": self._ticker,
            "daily_pnl": round(daily_pnl, 4),
            "limit": round(limit, 4),
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self.log_daily_summary("daily_loss_limit", status, bookkeeper)

    def log_time_limit_hit(
        self,
        current_time,
        cutoff,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        self._write_jsonl({
            "timestamp": self._ts(),
            "event_type": "TIME_LIMIT_HIT",
            "symbol": self._ticker,
            "current_time": str(current_time),
            "cutoff": str(cutoff),
            "portfolio_snapshot": self._portfolio_snapshot(bookkeeper),
        })
        self.log_daily_summary("time_limit", status, bookkeeper)

    def log_daily_summary(
        self,
        stop_reason: str,
        status: MachineStatus,
        bookkeeper: Bookkeeper,
    ) -> None:
        today = datetime.now(_EST).strftime("%Y-%m-%d")
        self._db_write(
            """INSERT INTO daily_summary
               (date, total_trades, winning_trades, gross_pnl, net_pnl, stop_reason)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                   total_trades   = excluded.total_trades,
                   winning_trades = excluded.winning_trades,
                   gross_pnl      = excluded.gross_pnl,
                   net_pnl        = excluded.net_pnl,
                   stop_reason    = excluded.stop_reason""",
            (
                today, self._day_trades, self._day_wins,
                round(self._day_pnl, 4), round(self._day_pnl, 4),
                stop_reason,
            ),
        )

    # ===================================================================
    # Day-boundary reset (called by TradingSession on EST date change)
    # ===================================================================

    def on_new_day(self) -> None:
        """Reset intraday counters and flags. JSONL file rotation is lazy."""
        self._day_pnl = 0.0
        self._day_trades = 0
        self._day_wins = 0
        self._loss_limit_logged = False
        self._time_limit_logged = False

    # ===================================================================
    # Shutdown
    # ===================================================================

    def close(self) -> None:
        with self._lock:
            if self._jsonl_file is not None:
                try:
                    self._jsonl_file.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                self._jsonl_file = None
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
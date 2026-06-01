"""
Tests for src/analyst/analyst.py.

Shared fixtures:
  pivots        — [100, 105, 110, 115, 120]
  bk            — Bookkeeper with 10_000 cash, no shares
  idle_status   — IDLE, no watermark, initial_nav=10_000
  long_status   — LONG, watermark=2 (pivots[2]=110, next=pivots[3]=115),
                  pending_order_ids=["existing-stoploss"] (normal LONG state)

Timestamps (winter date, no DST, America/New_York = EST = UTC-5):
  _BEFORE  14:00 EST = 19:00 UTC — before STOP_TRADING_TIME (15:30)
  _AFTER   16:00 EST = 21:00 UTC — after  STOP_TRADING_TIME
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest

from src.analyst.analyst import (
    _extend_pivots,
    _is_out_of_scope,
    _stoploss_price,
    analyse,
)
from src.bookkeeper.bookkeeper import Bookkeeper
from src.config import BUY_LIMIT_OFFSET, DAILY_LOSS_LIMIT, MIN_CASH_RESERVE
from src.models import ExecutionConfirmation, MachineStatus, OHLCVBar

_BEFORE = datetime(2026, 1, 15, 19, 0, tzinfo=timezone.utc)   # 14:00 EST
_AFTER  = datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)   # 16:00 EST


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _bar(close: float, ts: datetime = _BEFORE, volume: float = 100.0) -> OHLCVBar:
    return OHLCVBar(
        ticker="TEST", timestamp=ts,
        open=close - 0.1, high=close + 0.5, low=close - 0.5,
        close=close, volume=volume,
    )


def _recent_bars(close: float, n: int = 5, ts: datetime = _BEFORE) -> list[OHLCVBar]:
    """n identical bars whose VWAP equals close (typical_price = close when H=C+0.5, L=C-0.5)."""
    return [_bar(close, ts) for _ in range(n)]


def _give_shares(bk: Bookkeeper, qty: float = 10.0, price: float = 110.0) -> None:
    """Push a BUY confirmation into the bookkeeper so shares_held becomes non-zero."""
    bk.receive_confirmation(ExecutionConfirmation(
        confirmation_id="c-setup",
        order_id="o-setup",
        ticker="TEST",
        side="BUY",
        filled_quantity=qty,
        filled_price=price,
        filled_at=_BEFORE,
    ))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pivots() -> list[float]:
    return [100.0, 105.0, 110.0, 115.0, 120.0]


@pytest.fixture
def bk() -> Bookkeeper:
    return Bookkeeper(initial_cash=10_000.0)


@pytest.fixture
def idle_status() -> MachineStatus:
    return MachineStatus(
        position="IDLE",
        watermark_level=None,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=[],
        bar_count=0,
    )


@pytest.fixture
def long_status() -> MachineStatus:
    """Normal LONG state: watermark at index 2, stop-loss present in pending orders."""
    return MachineStatus(
        position="LONG",
        watermark_level=2,          # pivots[2]=110; next pivot pivots[3]=115
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=["existing-stoploss"],
        bar_count=0,
    )


# ---------------------------------------------------------------------------
# Stop condition tests
# ---------------------------------------------------------------------------

def test_stop_daily_loss_returns_empty(pivots: list[float], idle_status: MachineStatus) -> None:
    """Daily loss limit breached while IDLE → returns []."""
    threshold = idle_status.initial_nav * (1 - DAILY_LOSS_LIMIT)   # 9700.0
    low_cash = threshold + MIN_CASH_RESERVE - 1.0                   # available = threshold-1
    bk = Bookkeeper(initial_cash=low_cash)
    result = analyse(_bar(106.0), idle_status, pivots, _recent_bars(106.0), bk)
    assert result == []


def test_stop_post_hours_idle_returns_empty(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper
) -> None:
    """Post-hours + IDLE → returns []."""
    result = analyse(_bar(106.0, ts=_AFTER), idle_status, pivots, _recent_bars(106.0, ts=_AFTER), bk)
    assert result == []


def test_stop_post_hours_long_falls_through(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """Post-hours + LONG → stop conditions are NOT applied; LONG branch runs instead."""
    _give_shares(bk)
    bar = _bar(112.0, ts=_AFTER)
    result = analyse(bar, long_status, pivots, _recent_bars(112.0, ts=_AFTER), bk)
    assert any(o.type == "SELL_MARKET" for o in result)


# ---------------------------------------------------------------------------
# IDLE branch tests
# ---------------------------------------------------------------------------

def test_idle_pivot_mismatch_cancel_only(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper
) -> None:
    """pivot_vwap != pivot_close → returns [CANCEL_ALL_BUYS] only."""
    # VWAP ≈ 106 → closest pivot 105 (idx 1)
    # close = 112 → closest pivot 110 (idx 2)
    result = analyse(_bar(112.0), idle_status, pivots, _recent_bars(106.0), bk)
    assert len(result) == 1
    assert result[0].type == "CANCEL_ALL_BUYS"


def test_idle_pivot_match_emits_entry_orders(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper
) -> None:
    """pivot_vwap == pivot_close, i > 0 → CANCEL_ALL_BUYS + BUY_LIMIT + SELL_LIMIT."""
    # Both VWAP and close ≈ 106 → closest pivot 105 (idx 1)
    result = analyse(_bar(106.0), idle_status, pivots, _recent_bars(106.0), bk)
    assert [o.type for o in result] == ["CANCEL_ALL_BUYS", "BUY_LIMIT", "SELL_LIMIT"]


def test_idle_pivot_at_index_zero_uses_virtual_fallback(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper,
) -> None:
    """
    pivot_vwap == pivot_close at i==0 → one virtual pivot prepended, entry orders emitted.

    close=100 → closest pivot is pivots[0]=100 (i=0).
    Virtual pivot prepended: 100 * 0.985 = 98.5.
    Extended list: [98.5, 100, 105, 110, 115, 120], i becomes 1.
    BUY_LIMIT  : 100 * (1 - BUY_LIMIT_OFFSET) = 99.9
    SELL_LIMIT : (100 + 98.5) / 2 = 99.25
    """
    result = analyse(_bar(100.0), idle_status, pivots, _recent_bars(100.0), bk)
    assert [o.type for o in result] == ["CANCEL_ALL_BUYS", "BUY_LIMIT", "SELL_LIMIT"]
    buy = next(o for o in result if o.type == "BUY_LIMIT")
    sl  = next(o for o in result if o.type == "SELL_LIMIT")
    assert buy.price == pytest.approx(100.0 * (1 - BUY_LIMIT_OFFSET))
    assert sl.price  == pytest.approx((100.0 + 100.0 * 0.985) / 2)   # 99.25


def test_idle_buy_limit_price(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper
) -> None:
    """Buy limit price == pivots[i] * (1 - BUY_LIMIT_OFFSET)."""
    i = 1   # closest pivot to 106.0 is pivots[1]=105.0
    result = analyse(_bar(106.0), idle_status, pivots, _recent_bars(106.0), bk)
    buy = next(o for o in result if o.type == "BUY_LIMIT")
    assert buy.price == pytest.approx(pivots[i] * (1 - BUY_LIMIT_OFFSET))


def test_idle_stoploss_price(
    pivots: list[float], idle_status: MachineStatus, bk: Bookkeeper
) -> None:
    """Stop-loss price == (pivots[i] + pivots[i-1]) / 2; condition links to buy UUID."""
    i = 1   # pivots[1]=105, pivots[0]=100 → midpoint=102.5
    result = analyse(_bar(106.0), idle_status, pivots, _recent_bars(106.0), bk)
    buy = next(o for o in result if o.type == "BUY_LIMIT")
    sl  = next(o for o in result if o.type == "SELL_LIMIT")
    assert sl.price == pytest.approx(_stoploss_price(pivots, i))
    # UUID linkage: stop-loss condition must reference the buy order's UUID
    assert sl.condition == f"on_fill:{buy.condition}"


# ---------------------------------------------------------------------------
# LONG branch — watermark advance tests
# ---------------------------------------------------------------------------

def test_long_no_watermark_advance(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """close <= pivots[wm+1], stop-loss present → returns []."""
    # wm=2, pivots[3]=115; close=113 ≤ 115
    result = analyse(_bar(113.0), long_status, pivots, _recent_bars(113.0), bk)
    assert result == []


def test_long_watermark_advance(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """close > pivots[wm+1] → watermark advances, UPDATE_STOPLOSS emitted with correct price."""
    # wm=2, pivots[3]=115; close=116 > 115
    result = analyse(_bar(116.0), long_status, pivots, _recent_bars(116.0), bk)
    assert any(o.type == "UPDATE_STOPLOSS" for o in result)
    sl = next(o for o in result if o.type == "UPDATE_STOPLOSS")
    # new_wm=3; price = (pivots[3]+pivots[2])/2 = (115+110)/2 = 112.5
    assert sl.price == pytest.approx(_stoploss_price(pivots, 3))
    assert sl.watermark == 3
    assert long_status.watermark_level == 3


def test_long_at_top_of_pivot_list(
    pivots: list[float], bk: Bookkeeper, caplog: pytest.LogCaptureFixture
) -> None:
    """Watermark at last pivot index, price in-scope → returns [], warning logged."""
    # close=120.5 is in scope (120.0 * 1.0075 = 120.9 > 120.5) so no OOS extension;
    # wm+1=5 >= len(pivots)=5 triggers the "top of pivot list" path.
    status = MachineStatus(
        position="LONG",
        watermark_level=4,          # wm+1=5 >= len(pivots)=5
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=["existing-stoploss"],
        bar_count=0,
    )
    with caplog.at_level(logging.WARNING, logger="src.analyst.analyst"):
        result = analyse(_bar(120.5), status, pivots, _recent_bars(120.5), bk)
    assert result == []
    assert any("top" in r.message or "cannot advance" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# LONG branch — end-of-day exit tests (Stop Condition Wins)
# ---------------------------------------------------------------------------

def test_long_end_of_day_market_sell(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """post stop_trading_time + shares_held > 0 → [SELL_MARKET] only."""
    _give_shares(bk)
    bar = _bar(113.0, ts=_AFTER)
    result = analyse(bar, long_status, pivots, _recent_bars(113.0, ts=_AFTER), bk)
    assert any(o.type == "SELL_MARKET" for o in result)


def test_long_end_of_day_no_shares(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """post stop_trading_time + shares_held == 0 → returns []."""
    result = analyse(_bar(113.0, ts=_AFTER), long_status, pivots, _recent_bars(113.0, ts=_AFTER), bk)
    assert result == []


def test_long_stop_condition_wins_over_watermark_advance(
    pivots: list[float], long_status: MachineStatus, bk: Bookkeeper
) -> None:
    """
    Stop Condition Wins: when close crosses next pivot AND it is post-hours,
    only SELL_MARKET is emitted — no UPDATE_STOPLOSS.
    """
    _give_shares(bk)
    # close=116 > pivots[3]=115 would advance watermark, but post-hours takes priority
    bar = _bar(116.0, ts=_AFTER)
    result = analyse(bar, long_status, pivots, _recent_bars(116.0, ts=_AFTER), bk)
    types = [o.type for o in result]
    assert types == ["SELL_MARKET"]
    # Watermark must NOT have been advanced
    assert long_status.watermark_level == 2


# ---------------------------------------------------------------------------
# LONG branch — stop-loss re-emission tests (Req 2)
# ---------------------------------------------------------------------------

def test_long_reemits_stoploss_when_no_pending(
    pivots: list[float], bk: Bookkeeper
) -> None:
    """pending_order_ids empty + LONG + shares > 0 → re-emits SELL_LIMIT stop-loss."""
    _give_shares(bk)
    status = MachineStatus(
        position="LONG",
        watermark_level=2,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=[],       # stop-loss is missing
        bar_count=0,
    )
    # close=113 → closest pivot: |113-110|=3, |113-115|=2 → idx 3 (pivots[3]=115)
    # re-emitted stoploss price = (115+110)/2 = 112.5
    result = analyse(_bar(113.0), status, pivots, _recent_bars(113.0), bk)
    sl_orders = [o for o in result if o.type == "SELL_LIMIT"]
    assert len(sl_orders) == 1
    assert sl_orders[0].price == pytest.approx(_stoploss_price(pivots, 3))
    assert sl_orders[0].size == "ALL_SHARES"


def test_long_reemit_stoploss_price_anchors_to_close(
    pivots: list[float], bk: Bookkeeper
) -> None:
    """Re-emitted stop-loss price reflects closest pivot to bar.close, not watermark."""
    _give_shares(bk)
    status = MachineStatus(
        position="LONG",
        watermark_level=1,          # original watermark at idx 1 (pivots[1]=105)
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=[],
        bar_count=0,
    )
    # close=118 → closest pivot: |118-120|=2, |118-115|=3 → idx 4 (pivots[4]=120)
    # re-emitted stoploss = (120+115)/2 = 117.5  (different from original watermark)
    result = analyse(_bar(118.0), status, pivots, _recent_bars(118.0), bk)
    sl_orders = [o for o in result if o.type == "SELL_LIMIT"]
    assert len(sl_orders) == 1
    assert sl_orders[0].price == pytest.approx(_stoploss_price(pivots, 4))


def test_long_reemit_stoploss_skips_at_index_zero(
    pivots: list[float], bk: Bookkeeper, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-emission skips (returns []) and logs a warning when i == 0 (no pivot below)."""
    _give_shares(bk)
    status = MachineStatus(
        position="LONG",
        watermark_level=2,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=[],
        bar_count=0,
    )
    # close=100 → closest pivot idx 0 → skip re-emission
    with caplog.at_level(logging.WARNING, logger="src.analyst.analyst"):
        result = analyse(_bar(100.0), status, pivots, _recent_bars(100.0), bk)
    assert not any(o.type == "SELL_LIMIT" for o in result)
    assert any("index 0" in r.message or "pivot below" in r.message for r in caplog.records)


def test_long_reemit_and_watermark_advance_together(
    pivots: list[float], bk: Bookkeeper
) -> None:
    """
    Missing stop-loss AND close crosses next pivot: both re-emitted SELL_LIMIT
    and UPDATE_STOPLOSS appear in the same bar.
    """
    _give_shares(bk)
    status = MachineStatus(
        position="LONG",
        watermark_level=2,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=[],       # stop-loss missing
        bar_count=0,
    )
    # close=116 > pivots[3]=115 → watermark advances; also triggers re-emission
    result = analyse(_bar(116.0), status, pivots, _recent_bars(116.0), bk)
    types = [o.type for o in result]
    assert "SELL_LIMIT" in types        # re-emitted stop-loss
    assert "UPDATE_STOPLOSS" in types   # watermark advance


# ---------------------------------------------------------------------------
# Out-of-scope (OOS) boundary tests
#
# pivots = [100, 105, 110, 115, 120]
# lower_boundary = 100 * (1 - 0.0075) = 99.25
# upper_boundary = 120 * (1 + 0.0075) = 120.9
# ---------------------------------------------------------------------------

def test_oos_is_out_of_scope_above() -> None:
    """close above upper boundary → _is_out_of_scope returns True."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    assert _is_out_of_scope(121.0, pivots) is True


def test_oos_is_out_of_scope_below() -> None:
    """close below lower boundary → _is_out_of_scope returns True."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    assert _is_out_of_scope(99.0, pivots) is True


def test_oos_in_scope_not_flagged() -> None:
    """close within boundaries → _is_out_of_scope returns False."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    assert _is_out_of_scope(110.0, pivots) is False
    assert _is_out_of_scope(120.5, pivots) is False  # just inside upper boundary


def test_oos_extend_above_adds_sorted_virtual_pivots() -> None:
    """OOS above: _extend_pivots appends virtual pivots at 1.5% spacing, list stays sorted."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    # close=122 → j=round(log(122/120)/log(1.015))=round(1.11)=1 → adds 120*1.015=121.8
    extended = _extend_pivots(122.0, pivots)
    assert extended[:5] == pivots                          # original intact
    assert len(extended) == 6
    assert extended[-1] == pytest.approx(120.0 * 1.015)   # 121.8
    assert extended == sorted(extended)                    # still ascending


def test_oos_extend_below_prepends_sorted_virtual_pivots() -> None:
    """OOS below: _extend_pivots prepends virtual pivots at 1.5% spacing, list stays sorted."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    # close=98 → j=round(log(98/100)/log(0.985))=round(1.35)=1 → prepends 100*0.985=98.5
    extended = _extend_pivots(98.0, pivots)
    assert extended[-5:] == pivots                         # original intact at tail
    assert len(extended) == 6
    assert extended[0] == pytest.approx(100.0 * 0.985)    # 98.5
    assert extended == sorted(extended)                    # still ascending


def test_oos_above_long_watermark_advances_through_virtual_pivot(
    bk: Bookkeeper,
) -> None:
    """
    LONG + OOS above: virtual pivots are added, watermark can advance through them.
    close=122 > virtual_pivot[5]=121.8 → UPDATE_STOPLOSS emitted at correct price.
    """
    _give_shares(bk)
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    # close=122 → extended to [..., 120, 121.8]; close > 121.8 → wm advances from 4 to 5
    status = MachineStatus(
        position="LONG",
        watermark_level=4,
        initial_nav=10_000.0,
        session_date=date(2026, 1, 15),
        pending_order_ids=["existing-stoploss"],
        bar_count=0,
    )
    result = analyse(_bar(122.0), status, pivots, _recent_bars(122.0), bk)
    sl = next((o for o in result if o.type == "UPDATE_STOPLOSS"), None)
    assert sl is not None
    assert sl.price == pytest.approx((120.0 * 1.015 + 120.0) / 2)  # (121.8 + 120) / 2
    assert status.watermark_level == 5


def test_oos_idle_entry_orders_emitted_via_virtual_pivot(
    idle_status: MachineStatus, bk: Bookkeeper,
) -> None:
    """
    IDLE + OOS above: virtual pivot is added; if VWAP and close both map to it,
    entry orders are emitted (no index-0 crash or silent skip).
    close=122, VWAP≈122 → both closest to virtual pivot 121.8 (idx 5) → entry orders.
    """
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    result = analyse(_bar(122.0), idle_status, pivots, _recent_bars(122.0), bk)
    types = [o.type for o in result]
    assert "CANCEL_ALL_BUYS" in types
    assert "BUY_LIMIT" in types
    assert "SELL_LIMIT" in types


def test_oos_recalc_hook_resolves_scope(
    idle_status: MachineStatus, bk: Bookkeeper, caplog: pytest.LogCaptureFixture,
) -> None:
    """When recalc_hook returns pivots that cover close, no virtual extension occurs."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    fresh_pivots = [100.0, 105.0, 110.0, 115.0, 120.0, 122.0, 124.0]
    hook_called = []

    def hook() -> list[float]:
        hook_called.append(True)
        return fresh_pivots

    with caplog.at_level(logging.INFO, logger="src.analyst.analyst"):
        analyse(_bar(122.0), idle_status, pivots, _recent_bars(122.0), bk, recalc_hook=hook)

    assert hook_called, "recalc_hook must be called when OOS is detected"
    assert not any("extending pivot grid" in r.message for r in caplog.records), (
        "virtual extension must not happen when recalc resolves scope"
    )


def test_oos_recalc_hook_still_oos_falls_back_to_extension(
    idle_status: MachineStatus, bk: Bookkeeper, caplog: pytest.LogCaptureFixture,
) -> None:
    """When recalc_hook returns pivots that still don't cover close, virtual extension runs."""
    pivots = [100.0, 105.0, 110.0, 115.0, 120.0]
    still_oos_pivots = [105.0, 110.0, 115.0, 120.0]  # close=122 still OOS

    def hook() -> list[float]:
        return still_oos_pivots

    with caplog.at_level(logging.WARNING, logger="src.analyst.analyst"):
        analyse(_bar(122.0), idle_status, pivots, _recent_bars(122.0), bk, recalc_hook=hook)

    assert any("extending pivot grid" in r.message for r in caplog.records)

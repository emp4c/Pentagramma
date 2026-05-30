"""
Entry Interface — Streaming Mode.

Responsibility:
    Called by the live data feed once per closed bar. Orchestrates the full
    per-bar pipeline: scriber → pivot builder (every 30 bars) → analyst →
    trader → live broker bus. Maintains the bar counter used to schedule
    pivot recalculations. Holds the single MachineStatus instance for the session.

Public interface:
    on_bar(bar: OHLCVBar) -> None
        Main callback invoked by the live feed on each new 1-minute bar.

# REVIEW: CLAUDE.md names the dual entry points as run_stream(bar: OHLCVBar)
# and run_batch(df: pd.DataFrame), but architecture.md defines stream_entry.py
# with on_bar() and a separate batch_runner. Using on_bar() here to match
# architecture.md (the more detailed spec). Reconcile CLAUDE.md if on_bar is correct.
"""

from __future__ import annotations

from src.models import OHLCVBar, MachineStatus


def on_bar(bar: OHLCVBar, status: MachineStatus) -> None:
    """
    Process one incoming live bar through the full pipeline.

    Steps:
      1. Write bar to long-term DB via scriber.
      2. If status.bar_count % PIVOT_INTERVAL_BARS == 0: rebuild pivot list.
      3. Call analyst(bar, status, pivots, recent_bars) → AnalystOrders.
      4. Call trader(AnalystOrders, status, bookkeeper) → BrokerOrders.
      5. Send BrokerOrders via live_bus.
      6. Increment status.bar_count.

    Args:
        bar:    The latest closed 1-minute OHLCV bar from the live feed.
        status: Current machine state (mutated in place by this function).
    """
    raise NotImplementedError

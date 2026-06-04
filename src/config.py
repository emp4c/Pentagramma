"""
System-wide constants for the algo trader.
All tunable parameters live here. Import this module wherever a constant is needed.
No environment variables or config files for now — values are hardcoded and version-controlled.
"""

from datetime import time

# ---------------------------------------------------------------------------
# Database paths
# ---------------------------------------------------------------------------
DB_PATH: str = "data/ohlcv.db"
# Path to the long-term SQLite OHLCV bar store, relative to the project root.
# Overridden in tests by passing db_path explicitly to db / scriber functions.

# ---------------------------------------------------------------------------
# Trading session boundaries
# ---------------------------------------------------------------------------

STOP_TRADING_TIME: time = time(15, 40)
# No new orders (and no new IDLE entries) after this time (Eastern Time / EST).
# Post-hours: if IDLE, analyst returns nothing. If LONG, a market-sell is issued.

# ---------------------------------------------------------------------------
# Risk / position sizing
# ---------------------------------------------------------------------------

DAILY_LOSS_LIMIT: float = 0.03
# Maximum fraction of initial NAV that can be lost in a session before all
# trading halts. E.g. 0.03 = stop if current_nav < initial_nav * (1 - 0.03).

MIN_CASH_RESERVE: float = 100.0
# USD amount permanently held back and never deployed into a trade.
# The trader subtracts this before computing share quantity.

# ---------------------------------------------------------------------------
# Order pricing
# ---------------------------------------------------------------------------

BUY_LIMIT_OFFSET: float = 0.001
# Fractional offset applied below the pivot when placing a buy-limit order.
# E.g. 0.001 = buy at pivot * (1 - 0.001), i.e. 0.1% below the pivot.

# ---------------------------------------------------------------------------
# Pivot builder
# ---------------------------------------------------------------------------

PIVOT_RANGE: float = 0.12
# Pivots are filtered to within ±12% of the last bar's close price.
# Any pivot outside this band is discarded before being returned to the analyst.

PIVOT_INTERVAL_BARS: int = 30
# The pivot builder is called every N bars (both streaming and batch).
# At 1-bar-per-minute this equals 30 minutes between recalculations.

PIVOT_LOOKBACK_DAYS: int = 90
# Number of calendar days of historical OHLCV bars fed to the pivot builder.
# ~3 months of data. Used for both the live lookback query and batch warm-up.

# ---------------------------------------------------------------------------
# Analyst calculations
# ---------------------------------------------------------------------------

VWAP_WINDOW_BARS: int = 5
# Number of recent bars used to compute the VWAP check in the IDLE branch.
# VWAP = sum(typical_price_i * volume_i) / sum(volume_i) over the last N bars.

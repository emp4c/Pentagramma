"""
Alpaca connectivity smoke test.

Verifies:
  1. REST trading API  — account reachable, credentials valid
  2. SIP data feed     — historical bars accessible via the SIP (all-exchange) feed
                         Works outside market hours; live streaming requires open market.

Usage:
    python dev_tools/alpaca_smoke_test.py [TICKER]

    TICKER defaults to ASML (Nasdaq-listed European company).
    Supply any Alpaca-supported ticker; European ADRs such as ASML, SAP, BP
    have 24/7 historical data availability via the REST endpoint.

Does NOT submit any real orders.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from src.bus.live_bus import LiveBus
from src.config_env import ALPACA_API_KEY, ALPACA_API_SECRET

TICKER = sys.argv[1] if len(sys.argv) > 1 else "ASML"

# ── 1. REST trading API ───────────────────────────────────────────────────────
bus = LiveBus()
print(f"Paper mode:      {bus.paper}")
account = bus.client.get_account()
print(f"Cash:            {account.cash}")
print(f"Buying power:    {account.buying_power}")
print(f"Portfolio value: {account.portfolio_value}")
print("REST trading API  PASSED\n")

# ── 2. SIP data feed (historical REST — works outside market hours) ───────────
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_API_SECRET)

end   = datetime.now(tz=timezone.utc)
start = end - timedelta(days=7)   # enough to span a weekend

req = StockBarsRequest(
    symbol_or_symbols=[TICKER],
    timeframe=TimeFrame.Minute,
    start=start,
    end=end,
    feed=DataFeed.SIP,
)
bars = data_client.get_stock_bars(req)
df   = bars.df

if df.empty:
    print(f"SIP data ({TICKER}): no bars returned for the last 7 days.")
    print("  → Check that the ticker is supported on Alpaca and the account has SIP access.")
    sys.exit(1)

latest = df.iloc[-1]
print(f"SIP data ({TICKER}): {len(df)} minute-bars retrieved.")
print(f"  Latest bar  close={latest['close']:.4f}  volume={latest['volume']:.0f}")
print(f"  Timestamp   {df.index[-1]}")
print(f"SIP data feed  PASSED\n")

print("Smoke test PASSED")
"""
Alpaca LiveBus smoke test.

Verifies that credentials load correctly and the Alpaca account endpoint
responds.  Does NOT submit any real orders.

Run:
    python dev_tools/alpaca_smoke_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bus.live_bus import LiveBus

bus = LiveBus()
print(f"Paper mode: {bus.paper}")

account = bus.client.get_account()
print(f"Cash:            {account.cash}")
print(f"Buying power:    {account.buying_power}")
print(f"Portfolio value: {account.portfolio_value}")

print("LiveBus smoke test PASSED")
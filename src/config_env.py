"""
Environment configuration.

Single point of entry for all env vars. Every other module imports from here;
nothing else calls os.environ or load_dotenv directly.
"""

from dotenv import load_dotenv
import os

load_dotenv()

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

ALPACA_TRADING_URL = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER else
    "https://api.alpaca.markets"
)
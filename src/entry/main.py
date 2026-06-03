"""
Main entry point — live streaming mode.

Starts two daemon threads:
    1. bar-stream     — receives 1-min OHLCV bars via Alpaca WebSocket
    2. trade-updates  — receives order fill events via Alpaca trade-update stream

Both threads block on their own asyncio event loops; the main thread joins them.

Usage:
    python src/entry/main.py --ticker AAPL --cash 10000
    venv/Scripts/python.exe -m src.entry.main --ticker ONDS --cash 10000
"""

from __future__ import annotations

import argparse
import logging
import threading

from src.bookkeeper.bookkeeper import Bookkeeper
from src.bus.live_bus import LiveBus
from src.config_env import ALPACA_PAPER
from src.entry.alpaca_stream import AlpacaBarStream
from src.entry.alpaca_trade_updates import AlpacaTradeUpdateStream
from src.entry.stream_entry import TradingSession


def main(ticker: str, initial_cash: float) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    mode = "PAPER" if ALPACA_PAPER else "LIVE"
    logging.info("Starting algo trader — %s — %s mode", ticker, mode)

    bus = LiveBus()
    bookkeeper = Bookkeeper(initial_cash)
    session = TradingSession(ticker=ticker, initial_cash=initial_cash, bus=bus)

    bar_stream = AlpacaBarStream(session, ticker)
    trade_updates = AlpacaTradeUpdateStream(bookkeeper, session._status)

    t1 = threading.Thread(target=bar_stream.run, daemon=True, name="bar-stream")
    t2 = threading.Thread(target=trade_updates.run, daemon=True, name="trade-updates")
    t1.start()
    t2.start()
    t1.join()
    t2.join()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Algo trader — live streaming mode")
    p.add_argument("--ticker", required=True, help="Ticker symbol, e.g. AAPL")
    p.add_argument("--cash", type=float, default=10_000.0, help="Initial cash (default 10000)")
    args = p.parse_args()
    main(args.ticker, args.cash)
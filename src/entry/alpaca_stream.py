"""
Market Data Stream — Alpaca WebSocket.

Subscribes to 1-minute bar updates for a single ticker and feeds each closed
bar into the TradingSession pipeline via on_bar().

Public interface:
    AlpacaBarStream(session, ticker) — construct once per session
    AlpacaBarStream.run()            — blocks; reconnects automatically on failure
"""

from __future__ import annotations

import logging
import time

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

from src.config_env import ALPACA_API_KEY, ALPACA_API_SECRET
from src.entry.stream_entry import TradingSession
from src.models import OHLCVBar
from src.scriber.scriber import write_bar

_logger = logging.getLogger(__name__)


class AlpacaBarStream:
    def __init__(self, session: TradingSession, ticker: str) -> None:
        self.session = session
        self.ticker = ticker
        self.wss_client = StockDataStream(ALPACA_API_KEY, ALPACA_API_SECRET, feed=DataFeed.SIP)
        self.wss_client.subscribe_bars(self._on_bar, ticker)

    async def _on_bar(self, bar: Bar) -> None:
        ohlcv = OHLCVBar(
            ticker=bar.symbol,
            timestamp=bar.timestamp,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )
        _logger.info(
            "Bar received: %s %s O=%.4f H=%.4f L=%.4f C=%.4f V=%.0f",
            ohlcv.ticker, ohlcv.timestamp, ohlcv.open, ohlcv.high,
            ohlcv.low, ohlcv.close, ohlcv.volume,
        )
        write_bar(ohlcv)
        self.session.on_bar(ohlcv)

    def _rebuild_client(self) -> None:
        self.wss_client = StockDataStream(ALPACA_API_KEY, ALPACA_API_SECRET, feed=DataFeed.SIP)
        self.wss_client.subscribe_bars(self._on_bar, self.ticker)

    def run(self) -> None:
        backoff = 5
        while True:
            try:
                _logger.info("%s: connecting...", self.__class__.__name__)
                self.wss_client.run()
                _logger.warning(
                    "%s: stream ended — reconnecting in %ds",
                    self.__class__.__name__, backoff,
                )
            except Exception as e:
                _logger.error(
                    "%s: error %s — reconnecting in %ds",
                    self.__class__.__name__, e, backoff,
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            self._rebuild_client()

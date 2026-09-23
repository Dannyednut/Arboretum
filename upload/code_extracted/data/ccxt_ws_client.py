import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List

import ccxt.pro as ccxtpro

from core.interfaces.exchange import BaseWsClient

logger = logging.getLogger("CcxtWS")
Callback = Callable[[Dict[str, Any]], Awaitable[None]]

class CcxtWsClient(BaseWsClient):
    def __init__(self, exchange_id: str, testnet: bool = False):
        self.exchange_id = exchange_id
        self.testnet = testnet
        self._exchange_class = getattr(ccxtpro, exchange_id)
        self.exchange = self._exchange_class({
            'enableRateLimit': True,
        })
        if self.testnet:
            self.exchange.set_sandbox_mode(True)
            
        self._is_running = False
        self._tasks: List[asyncio.Task] = []

    async def connect(self) -> None:
        self._is_running = True
        logger.info(f"Connecting to {self.exchange_id} (testnet={self.testnet})")
        # Load markets early so symbols are recognized
        await self.exchange.load_markets()

    async def disconnect(self) -> None:
        self._is_running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.exchange.close()
        logger.info(f"Disconnected from {self.exchange_id}")

    async def subscribe_l2_book(self, coins: List[str], callback: Callback) -> None:
        for symbol in coins:
            task = asyncio.create_task(self._watch_order_book_loop(symbol, callback))
            self._tasks.append(task)

    async def subscribe_funding_rates(self, coins: List[str], callback: Callback) -> None:
        for symbol in coins:
            task = asyncio.create_task(self._watch_funding_rate_loop(symbol, callback))
            self._tasks.append(task)

    async def _watch_order_book_loop(self, symbol: str, callback: Callback) -> None:
        while self._is_running:
            try:
                orderbook = await self.exchange.watch_order_book(symbol)
                # CCXT orderbook structure: {'bids': [[price, size]], 'asks': [[price, size]], 'symbol': 'BTC/USDT'}
                # We normalize it so the callback can handle it.
                bids = [{"px": str(p), "sz": str(s)} for p, s in orderbook.get("bids", [])]
                asks = [{"px": str(p), "sz": str(s)} for p, s in orderbook.get("asks", [])]
                
                await callback({
                    "coin": symbol,
                    "levels": [bids, asks],
                    "time": orderbook.get("timestamp", int(asyncio.get_event_loop().time() * 1000))
                })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error watching order book for {symbol}: {e}")
                await asyncio.sleep(1)

    async def _watch_funding_rate_loop(self, symbol: str, callback: Callback) -> None:
        while self._is_running:
            try:
                if self.exchange.has['watchFundingRate']:
                    funding_rate = await self.exchange.watch_funding_rate(symbol)
                elif self.exchange.has['watchTicker']:
                    # Fallback
                    ticker = await self.exchange.watch_ticker(symbol)
                    funding_rate = {
                        "symbol": symbol,
                        "fundingRate": ticker.get("info", {}).get("fundingRate", ticker.get("info", {}).get("lastFundingRate", 0))
                    }
                else:
                    logger.warning(f"No watchFundingRate support on {self.exchange_id}, polling every 10s")
                    funding_rate = await self.exchange.fetch_funding_rate(symbol)
                    await asyncio.sleep(10)
                
                await callback({
                    "coin": symbol,
                    "ctx": {
                        "fundingRate": funding_rate.get("fundingRate", 0.0)
                    }
                })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error watching funding rate for {symbol}: {e}")
                await asyncio.sleep(1)

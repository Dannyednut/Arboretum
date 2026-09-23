import asyncio
import json
import logging
from collections import defaultdict
from time import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets

from core.interfaces.exchange import BaseWsClient

logger = logging.getLogger("HyperliquidWS")
Callback = Callable[[Dict[str, Any]], Awaitable[None]]


class HyperliquidWsClient(BaseWsClient):
    def __init__(self, uri: str, connect_timeout_seconds: float = 15.0):
        self.uri = uri
        self.connect_timeout_seconds = connect_timeout_seconds
        self.ws = None
        self._callbacks: Dict[str, List[Callback]] = {
            "l2Book": [],
            "activeAssetCtx": [],
        }
        self._active_subscriptions: List[Dict[str, Any]] = []
        self._is_running = False
        self._connected: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._last_error: Optional[Exception] = None
        self.message_counts: Dict[str, int] = defaultdict(int)
        self.last_message_at: Dict[str, float] = {}
        self._last_msg_time = time()

    async def connect(self) -> None:
        self._is_running = True
        self._connected = asyncio.Event()
        self._last_msg_time = time()
        self._task = asyncio.create_task(self._maintain_connection())
        self._ping_task = asyncio.create_task(self._ping_loop())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.connect_timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._is_running = False
            if self._task:
                self._task.cancel()
            if self._ping_task:
                self._ping_task.cancel()
            await asyncio.gather(*[t for t in [self._task, self._ping_task] if t], return_exceptions=True)
            detail = f": {self._last_error}" if self._last_error else ""
            raise ConnectionError(
                f"Timed out connecting to Hyperliquid WebSocket {self.uri}{detail}"
            ) from exc

    async def disconnect(self) -> None:
        self._is_running = False
        if self.ws:
            await self.ws.close()
        tasks = []
        if self._task:
            self._task.cancel()
            tasks.append(self._task)
        if self._ping_task:
            self._ping_task.cancel()
            tasks.append(self._ping_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("WebSocket disconnected")

    async def _maintain_connection(self) -> None:
        while self._is_running:
            try:
                async with websockets.connect(self.uri, ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    self._last_msg_time = time()
                    if self._connected:
                        self._connected.set()
                    logger.info("Connected to %s", self.uri)
                    for sub in self._active_subscriptions:
                        await self._send_subscribe(sub)
                    await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = exc
                if self._connected:
                    self._connected.clear()
                logger.warning("WebSocket dropped: %s. Reconnecting in 2 seconds", exc)
                await asyncio.sleep(2)

    async def _ping_loop(self) -> None:
        last_ping = time()
        while self._is_running:
            try:
                await asyncio.sleep(5)
                now = time()
                # Watchdog check (reconnect if idle too long)
                if self.ws and self.ws.open:
                    if now - self._last_msg_time > 45.0:
                        logger.warning("No messages received for 45s. Forcing reconnect.")
                        await self.ws.close()
                        continue
                    # Ping check (send application ping every 30s)
                    if now - last_ping >= 30.0:
                        await self.ws.send(json.dumps({"method": "ping"}))
                        last_ping = now
                        logger.debug("Sent application ping")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WebSocket ping/watchdog error: %s", exc)

    async def _listen(self) -> None:
        async for message in self.ws:
            self._last_msg_time = time()
            data = json.loads(message)
            channel = data.get("channel")
            self.message_counts[channel or "unknown"] += 1
            self.last_message_at[channel or "unknown"] = time()
            if channel == "subscriptionResponse":
                logger.info("Subscription response: %s", data.get("data", data))
                continue
            if channel == "pong":
                logger.debug("Received application pong")
                continue
            callbacks = self._callbacks.get(channel, [])
            if not callbacks:
                logger.debug("Unhandled WebSocket channel %s: %s", channel, data)
                continue
            for callback in callbacks:
                try:
                    await callback(data["data"])
                except Exception:
                    logger.exception("Callback failed for channel %s", channel)

    async def subscribe_l2_book(self, coins: List[str], callback: Callback) -> None:
        self._callbacks["l2Book"].append(callback)
        for coin in coins:
            await self._subscribe({"type": "l2Book", "coin": coin})

    async def subscribe_funding_rates(self, coins: List[str], callback: Callback) -> None:
        self._callbacks["activeAssetCtx"].append(callback)
        for coin in coins:
            await self._subscribe({"type": "activeAssetCtx", "coin": coin})

    async def _subscribe(self, subscription: Dict[str, Any]) -> None:
        if subscription not in self._active_subscriptions:
            self._active_subscriptions.append(subscription)
        if self.ws:
            await self._send_subscribe(subscription)

    async def _send_subscribe(self, subscription: Dict[str, Any]) -> None:
        await self.ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))

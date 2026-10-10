"""
Live Hyperliquid WebSocket client.

Subscribes to trades, l2Book and activeAssetCtx for each coin on one socket, keeps it
alive with pings, and reconnects with exponential backoff. Every raw message is
handed to `on_message` untouched: parsing happens downstream so live and
replay share one code path.
"""
import asyncio
import json
import logging
import time
from typing import Any, Callable

import websockets

from config import HL_WS_URL, PING_INTERVAL_S
from ingestion.feedStatus import FeedStatus
from ingestion.parsers import subscriptions
from ingestion.recorder import HourlyRecorder, JsonlRecorder

log = logging.getLogger("analytix.ingestion")

OnMessage = Callable[[dict[str, Any]], None]


class HyperliquidClient:
    def __init__(
        self,
        coins: list[str],
        on_message: OnMessage,
        status: FeedStatus,
        url: str = HL_WS_URL,
        recorder: JsonlRecorder | HourlyRecorder | None = None,
    ):
        self.coins = coins
        self.on_message = on_message
        self.status = status
        self.url = url
        self.recorder = recorder
        self.status.source = url

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None, open_timeout=10, max_size=2**23) as ws:
                    for coin in self.coins:
                        for sub in subscriptions(coin):
                            await ws.send(json.dumps(sub))
                    self.status.connected = True
                    self.status.last_error = None
                    log.info("connected to %s, subscribed to %s", self.url, ", ".join(self.coins))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, proxy refusals, server closes
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("feed error: %s, reconnecting in %.0fs", self.status.last_error, backoff)
            self.status.connected = False
            self.status.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        if msg.get("channel") == "pong":
            return
        self.status.touch()
        if self.recorder:
            self.recorder.write(msg, int(time.time() * 1000))
        try:
            self.on_message(msg)
        except Exception:  # one bad message must not kill the feed
            log.exception("failed to process message on channel %s", msg.get("channel"))

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send(json.dumps({"method": "ping"}))

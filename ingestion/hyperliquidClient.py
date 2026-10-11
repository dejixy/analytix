"""
Live Hyperliquid WebSocket client.

Subscribes to trades, l2Book and activeAssetCtx for each coin on one socket, keeps it
alive with pings, and reconnects with exponential backoff. What a socket listens to can
change while it runs (set_subscriptions): the second, unrecorded socket follows the
scanner's most traded coins and any coin opened from More coins. Every raw message is
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

SEND_BATCH = 25                   # subscribe messages sent back to back before a short pause, so a socket with
SEND_PAUSE_S = 0.05               # hundreds of feeds (the prediction markets) doesn't flood Hyperliquid at once

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
        self._subs: dict[str, dict] = {_key(s): s for c in coins for s in subscriptions(c)}
        self._ws = None
        self.server_error: str | None = None      # the last error Hyperliquid sent (e.g. a rejected subscription)
        self._error_logged = 0.0

    def set_subscriptions(self, subs: list[dict]) -> None:
        """Listen to exactly these feeds from now on; takes effect at once if connected, else on connect."""
        wanted = {_key(s): s for s in subs}
        added = [s for k, s in wanted.items() if k not in self._subs]
        removed = [s for k, s in self._subs.items() if k not in wanted]
        self._subs = wanted
        ws = self._ws
        if ws is None or not (added or removed):
            return
        msgs = [{"method": "unsubscribe", "subscription": s["subscription"]} for s in removed] + added
        asyncio.create_task(self._send_all(ws, msgs))

    @staticmethod
    async def _send_all(ws, msgs: list[dict]) -> None:
        try:
            for i, msg in enumerate(msgs):
                if i and i % SEND_BATCH == 0:
                    await asyncio.sleep(SEND_PAUSE_S)
                await ws.send(json.dumps(msg))
        except Exception as exc:                  # the socket is closing: the next connect resubscribes
            log.debug("couldn't send subscriptions: %s", exc)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None, open_timeout=10, max_size=2**23) as ws:
                    for i, sub in enumerate(list(self._subs.values())):
                        if i and i % SEND_BATCH == 0:
                            await asyncio.sleep(SEND_PAUSE_S)
                        await ws.send(json.dumps(sub))
                    self._ws = ws
                    self.status.connected = True
                    self.status.last_error = None
                    coins = sorted({s["subscription"].get("coin", "") for s in self._subs.values()})
                    log.info("connected to %s, subscribed to %s", self.url, ", ".join(coins) or "nothing yet")
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        self._ws = None
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
        ch = msg.get("channel")
        if ch == "pong":
            return
        if ch == "error":                         # e.g. a subscription Hyperliquid doesn't accept
            self.server_error = str(msg.get("data"))[:300]
            if time.time() - self._error_logged > 60:
                self._error_logged = time.time()
                log.warning("Hyperliquid replied with an error on %s: %s", self.url, self.server_error)
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


def _key(sub: dict) -> str:
    return json.dumps(sub.get("subscription"), sort_keys=True)

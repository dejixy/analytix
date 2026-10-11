"""
Other exchanges' prices for the same coins, so explanations can say where a move started.

    Binance USDⓈ-M futures   bookTicker (best bid and ask, every change)   wss://fstream.binance.com
    Bybit linear perps       orderbook.1 (best bid and ask)               wss://stream.bybit.com/v5/public/linear

Both are public market data: no account or key. Each quote is stamped with the exchange's own time and
passed on at most every QUOTE_EVERY_MS per coin, as a small normalised message that the recorder saves
next to Hyperliquid's, so replays and the study see the same thing:

    {"channel": "ext", "data": {"venue": "binance", "coin": "ETH", "ts": 1790000000123, "bid": 2650.1, "ask": 2650.2}}

Coins are mapped to each exchange's symbol (ETH → ETHUSDT; Hyperliquid's "k" coins, priced per 1,000, to
1000…USDT). A coin an exchange doesn't list simply never sends anything.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets

from config import BINANCE_WS_URL, BYBIT_WS_URL

log = logging.getLogger("analytix.external")

QUOTE_EVERY_MS = 250
BYBIT_PING_S = 20


def symbol(coin: str) -> str:
    if coin.startswith("k") and coin[1:].isupper():
        return f"1000{coin[1:]}USDT"
    return f"{coin}USDT"


@dataclass(slots=True)
class VenueStatus:
    connected: bool = False
    quotes: int = 0
    last_ms: int = 0
    last_error: str | None = None
    reconnects: int = 0


@dataclass(slots=True)
class Quoter:
    """Turns raw best bid/ask updates into at most one message per coin every QUOTE_EVERY_MS."""
    venue: str
    emit: Callable[[dict], None]
    by_symbol: dict[str, str]                      # exchange symbol → Hyperliquid coin
    status: VenueStatus = field(default_factory=VenueStatus)
    _last: dict[str, int] = field(default_factory=dict)

    def quote(self, sym: str, ts: int, bid: float, ask: float) -> None:
        coin = self.by_symbol.get(sym)
        if coin is None or not (bid > 0 and ask >= bid):
            return
        if ts - self._last.get(coin, 0) < QUOTE_EVERY_MS:
            return
        self._last[coin] = ts
        self.status.quotes += 1
        self.status.last_ms = ts
        self.emit({"channel": "ext", "data": {"venue": self.venue, "coin": coin, "ts": ts, "bid": bid, "ask": ask}})


def parse_binance(msg: dict, q: Quoter) -> None:
    d = msg.get("data", msg)                       # combined streams wrap the payload in {"stream", "data"}
    if d.get("e") != "bookTicker":
        return
    try:
        q.quote(d["s"], int(d.get("T") or d.get("E")), float(d["b"]), float(d["a"]))
    except (KeyError, TypeError, ValueError):
        pass


@dataclass(slots=True)
class _Top:
    bid: float = 0.0
    ask: float = 0.0


def parse_bybit(msg: dict, q: Quoter, book: dict[str, _Top]) -> None:
    topic = msg.get("topic", "")
    if not topic.startswith("orderbook.1."):
        return
    d = msg.get("data") or {}
    sym = d.get("s") or topic.rsplit(".", 1)[-1]
    top = book.setdefault(sym, _Top())
    if msg.get("type") == "snapshot":
        top.bid = top.ask = 0.0
    try:
        for side, levels in (("bid", d.get("b") or []), ("ask", d.get("a") or [])):
            for px, sz in levels:                    # depth 1: a size is the new best; a zero removes it
                px, sz = float(px), float(sz)
                if sz > 0:
                    setattr(top, side, px)
                elif px == getattr(top, side):
                    setattr(top, side, 0.0)
        ts = int(msg.get("cts") or msg.get("ts"))
    except (TypeError, ValueError):
        return
    if top.bid and top.ask:
        q.quote(sym, ts, top.bid, top.ask)


class ExternalFeed:
    """One socket per exchange, reconnecting with backoff; quotes go to `on_message` (and the recorder)."""

    def __init__(self, coins: list[str], on_message: Callable[[dict], None], recorder=None,
                 venues: tuple[str, ...] = ("binance", "bybit")):
        self.coins = list(coins)
        self.on_message = on_message
        self.recorder = recorder
        self.venues = venues
        by_symbol = {symbol(c): c for c in self.coins}
        self.quoters = {v: Quoter(v, self._emit, by_symbol) for v in venues}

    def _emit(self, msg: dict) -> None:
        if self.recorder:
            self.recorder.write(msg, int(time.time() * 1000))
        try:
            self.on_message(msg)
        except Exception:
            log.exception("failed to process an external quote")

    def status(self) -> dict:
        return {v: {"connected": q.status.connected, "quotes": q.status.quotes, "last_error": q.status.last_error}
                for v, q in self.quoters.items()}

    async def run(self) -> None:
        tasks = []
        if "binance" in self.venues:
            tasks.append(self._loop("binance", self._binance))
        if "bybit" in self.venues:
            tasks.append(self._loop("bybit", self._bybit))
        await asyncio.gather(*tasks)

    async def _loop(self, venue: str, session) -> None:
        st = self.quoters[venue].status
        backoff = 1.0
        while True:
            try:
                await session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # blocked region, network, a server close
                st.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("%s feed error: %s, reconnecting in %.0fs", venue, st.last_error, backoff)
            st.connected = False
            st.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _binance(self) -> None:
        q = self.quoters["binance"]
        streams = "/".join(f"{symbol(c).lower()}@bookTicker" for c in self.coins)
        async with websockets.connect(f"{BINANCE_WS_URL}?streams={streams}", open_timeout=10, max_size=2**22) as ws:
            q.status.connected, q.status.last_error = True, None
            log.info("connected to Binance for %s", ", ".join(self.coins))
            async for raw in ws:
                parse_binance(json.loads(raw), q)

    async def _bybit(self) -> None:
        q = self.quoters["bybit"]
        book: dict[str, _Top] = {}
        async with websockets.connect(BYBIT_WS_URL, ping_interval=None, open_timeout=10, max_size=2**22) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.1.{symbol(c)}" for c in self.coins]}))
            q.status.connected, q.status.last_error = True, None
            log.info("connected to Bybit for %s", ", ".join(self.coins))
            pinger = asyncio.create_task(self._bybit_ping(ws))
            try:
                async for raw in ws:
                    parse_bybit(json.loads(raw), q, book)
            finally:
                pinger.cancel()

    @staticmethod
    async def _bybit_ping(ws) -> None:
        while True:
            await asyncio.sleep(BYBIT_PING_S)
            await ws.send(json.dumps({"op": "ping"}))

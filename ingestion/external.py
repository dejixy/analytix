"""
Other exchanges' prices for the same coins, so explanations can say where a move started.

    Binance USDⓈ-M futures   bookTicker (best bid and ask, every change)   wss://fstream.binance.com
    Bybit linear perps       orderbook.1 (best bid and ask)               wss://stream.bybit.com/v5/public/linear
    Lighter perps            ticker (best bid and ask) and trade          wss://mainnet.zklighter.elliot.ai/stream

Both are public market data: no account or key. Each quote is stamped with the exchange's own time and
passed on at most every QUOTE_EVERY_MS per coin, as a small normalised message that the recorder saves
next to Hyperliquid's, so replays and the study see the same thing:

    {"channel": "ext", "data": {"venue": "binance", "coin": "ETH", "ts": 1790000000123, "bid": 2650.1, "ask": 2650.2}}

A quote that arrives more than STALE_QUOTE_MS after its own timestamp is dropped (and counted): Bybit delivers about
3.5% of its messages 5 to 30 s late, which stamped old prices as fresh moves.

Lighter (zero fees, but orders held back 200 to 300 ms on a free account) is recorded only: its quotes and trades
go to the recorder, never to the dashboard, so the live reads are unchanged. Its quotes are passed on every
LIGHTER_QUOTE_EVERY_MS, and its trades as

    {"channel": "ext_trade", "data": {"venue": "lighter", "coin": "ETH", "ts": ..., "px": ..., "sz": ..., "side": 1,
                                      "liq": false, "id": ...}}          side +1: the taker bought

Lighter numbers its markets; the symbol -> market id map is read from its REST API at connect (or set with
ANALYTIX_LIGHTER_MARKETS).

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

import httpx

from config import BINANCE_WS_URL, BYBIT_WS_URL, LIGHTER_API_URL, LIGHTER_MARKETS, LIGHTER_WS_URL

log = logging.getLogger("analytix.external")

QUOTE_EVERY_MS = 250
STALE_QUOTE_MS = 2_000
LIGHTER_QUOTE_EVERY_MS = 100     # finer than the others: the study is about how fast Lighter follows
LIGHTER_PING_S = 60              # Lighter closes a socket that sends nothing for 2 minutes
RECORD_ONLY = ("lighter",)
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
    stale: int = 0                                  # quotes dropped for arriving > STALE_QUOTE_MS after their time


@dataclass(slots=True)
class Quoter:
    """Turns raw best bid/ask updates into at most one message per coin every QUOTE_EVERY_MS."""
    venue: str
    emit: Callable[[dict], None]
    by_symbol: dict[str, str]                      # exchange symbol → Hyperliquid coin
    status: VenueStatus = field(default_factory=VenueStatus)
    _last: dict[str, int] = field(default_factory=dict)
    every_ms: int = QUOTE_EVERY_MS

    def quote(self, sym: str, ts: int, bid: float, ask: float, recv_ms: float | None = None) -> None:
        coin = self.by_symbol.get(sym)
        if coin is None or not (bid > 0 and ask >= bid):
            return
        if recv_ms is not None and recv_ms - ts > STALE_QUOTE_MS:
            self.status.stale += 1
            return
        if ts - self._last.get(coin, 0) < self.every_ms:
            return
        self._last[coin] = ts
        self.status.quotes += 1
        self.status.last_ms = ts
        self.emit({"channel": "ext", "data": {"venue": self.venue, "coin": coin, "ts": ts, "bid": bid, "ask": ask}})


def parse_binance(msg: dict, q: Quoter, recv_ms: float | None = None) -> None:
    d = msg.get("data", msg)                       # combined streams wrap the payload in {"stream", "data"}
    if d.get("e") != "bookTicker":
        return
    try:
        q.quote(d["s"], int(d.get("T") or d.get("E")), float(d["b"]), float(d["a"]), recv_ms)
    except (KeyError, TypeError, ValueError):
        pass


@dataclass(slots=True)
class _Top:
    bid: float = 0.0
    ask: float = 0.0


def parse_bybit(msg: dict, q: Quoter, book: dict[str, _Top], recv_ms: float | None = None) -> None:
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
        q.quote(sym, ts, top.bid, top.ask, recv_ms)


def to_ms(x) -> int:
    """Lighter's timestamps in milliseconds, whether it sends seconds, milliseconds or microseconds."""
    v = float(x)
    return int(v / 1000 if v > 1e14 else v if v > 1e11 else v * 1000)


def lighter_markets(payload: dict, coins: list[str]) -> dict[int, str]:
    """market id -> coin from Lighter's market list (orderBooks or orderBookDetails), for the coins we follow."""
    want = set(coins)
    out: dict[int, str] = {}
    for key in ("order_books", "order_book_details"):
        for m in payload.get(key) or []:
            sym, mid = m.get("symbol"), m.get("market_id", m.get("market_index"))
            if sym in want and mid is not None:
                out[int(mid)] = sym
    return out


def parse_lighter(msg: dict, q: Quoter, markets: dict[int, str], emit_trade: Callable[[dict], None],
                  recv_ms: float | None = None) -> None:
    """A Lighter ticker update -> a quote; a trade update -> one ext_trade message per trade."""
    ch = str(msg.get("channel", ""))
    kind, _, idx = ch.partition(":")
    try:
        coin = markets.get(int(idx))
    except ValueError:
        return
    if coin is None:
        return
    try:
        if kind == "ticker":
            t = msg.get("ticker") or {}
            bid, ask = t.get("b") or {}, t.get("a") or {}
            ts = to_ms(msg.get("timestamp") or t.get("last_updated_at"))
            q.quote(coin, ts, float(bid["price"]), float(ask["price"]), recv_ms)
        elif kind == "trade":
            for liq, rows in ((False, msg.get("trades") or []), (True, msg.get("liquidation_trades") or [])):
                for tr in rows:
                    emit_trade({"channel": "ext_trade", "data": {
                        "venue": q.venue, "coin": coin, "ts": to_ms(tr["timestamp"]), "px": float(tr["price"]),
                        "sz": float(tr["size"]), "side": 1 if tr.get("is_maker_ask") else -1, "liq": liq,
                        "id": tr.get("trade_id")}})
    except (KeyError, TypeError, ValueError):
        pass


class ExternalFeed:
    """One socket per exchange, reconnecting with backoff; quotes go to `on_message` (and the recorder)."""

    def __init__(self, coins: list[str], on_message: Callable[[dict], None], recorder=None,
                 venues: tuple[str, ...] = ("binance", "bybit")):
        self.coins = list(coins)
        self.on_message = on_message
        self.recorder = recorder
        self.venues = venues
        by_symbol = {symbol(c): c for c in self.coins}
        self.quoters = {v: Quoter(v, self._emit, by_symbol) for v in venues if v not in RECORD_ONLY}
        if "lighter" in venues:
            self.quoters["lighter"] = Quoter("lighter", self._record, {c: c for c in self.coins},
                                             every_ms=LIGHTER_QUOTE_EVERY_MS)
        self.lighter_markets: dict[int, str] = {}

    def _record(self, msg: dict) -> None:
        """Record-only venues: saved for studies, never passed to the dashboard."""
        if self.recorder:
            self.recorder.write(msg, int(time.time() * 1000))

    def _emit(self, msg: dict) -> None:
        if self.recorder:
            self.recorder.write(msg, int(time.time() * 1000))
        try:
            self.on_message(msg)
        except Exception:
            log.exception("failed to process an external quote")

    def status(self) -> dict:
        return {v: {"connected": q.status.connected, "quotes": q.status.quotes, "stale": q.status.stale,
                    "last_error": q.status.last_error}
                for v, q in self.quoters.items()}

    async def run(self) -> None:
        tasks = []
        if "binance" in self.venues:
            tasks.append(self._loop("binance", self._binance))
        if "bybit" in self.venues:
            tasks.append(self._loop("bybit", self._bybit))
        if "lighter" in self.venues:
            tasks.append(self._loop("lighter", self._lighter))
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
                parse_binance(json.loads(raw), q, time.time() * 1000)

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
                    parse_bybit(json.loads(raw), q, book, time.time() * 1000)
            finally:
                pinger.cancel()

    async def _lighter(self) -> None:
        q = self.quoters["lighter"]
        if not self.lighter_markets:
            if LIGHTER_MARKETS:
                self.lighter_markets = {int(i): c for c, i in (x.split(":") for x in LIGHTER_MARKETS.split(","))}
            else:
                async with httpx.AsyncClient(timeout=10) as http:
                    for path in ("/api/v1/orderBooks", "/api/v1/orderBookDetails"):
                        r = await http.get(LIGHTER_API_URL + path)
                        if r.status_code == 200:
                            self.lighter_markets = lighter_markets(r.json(), self.coins)
                            if self.lighter_markets:
                                break
            if not self.lighter_markets:
                raise RuntimeError("couldn't read Lighter's market ids (set ANALYTIX_LIGHTER_MARKETS)")
            log.info("Lighter markets: %s", ", ".join(f"{c}={i}" for i, c in sorted(self.lighter_markets.items())))
        async with websockets.connect(LIGHTER_WS_URL, ping_interval=None, open_timeout=10, max_size=2**22) as ws:
            for i in self.lighter_markets:
                for ch in ("ticker", "trade"):
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"{ch}/{i}"}))
            q.status.connected, q.status.last_error = True, None
            log.info("connected to Lighter for %s", ", ".join(self.lighter_markets.values()))
            pinger = asyncio.create_task(self._lighter_ping(ws))
            try:
                async for raw in ws:
                    parse_lighter(json.loads(raw), q, self.lighter_markets, self._record, time.time() * 1000)
            finally:
                pinger.cancel()

    @staticmethod
    async def _lighter_ping(ws) -> None:
        while True:
            await asyncio.sleep(LIGHTER_PING_S)
            await ws.send(json.dumps({"type": "ping"}))

    @staticmethod
    async def _bybit_ping(ws) -> None:
        while True:
            await asyncio.sleep(BYBIT_PING_S)
            await ws.send(json.dumps({"op": "ping"}))

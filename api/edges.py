"""
Edge service: the live data behind the edge terminal (/terminal), from trade.xyz's builder markets on Hyperliquid
and the schedule in engine/edges.py.

    markets     every EDGE_COINS market's mark, oracle, funding, open interest and volume (metaAndAssetCtxs per
                builder, every CTX_EVERY_S); a minute-by-minute mark history in memory for 1-hour moves
    candles     each market's last 5000 hourly candles (about 7 months), every CANDLES_EVERY_S: the weekend record
    futures     the oil roll's front and next contracts from Yahoo Finance, every FUTURES_EVERY_S (optional)
    trades      every taker order on these markets, live (fills merged by block, taker and side), kept an hour:
                from the long-tail socket that records them, or a socket of its own when recording is off
    /api/edges  sessions, the live weekend (when shut), the weekend record and fits, the roll, currencies,
                what's coming up and the edge board
    /api/edges/trades?since=ms   the orders since then, for the live bubble map

Live mode only; in replay it still shows the schedule.
"""
import asyncio
import logging
import math
import time
from collections import deque

from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import parse_trade, subscriptions

from config import EDGE_COINS
from engine import edges as E
from ingestion.account import _info, _num
from ingestion.backfill import fetch_candles
from ingestion.futures import fetch_quotes

log = logging.getLogger("analytix.edges")

CTX_EVERY_S = 15
CANDLES_EVERY_S = 1800
FUTURES_EVERY_S = 600
HISTORY_KEEP_MS = 26 * E.HOUR
SPARK_HOURS = 48
TRADES_KEEP_MS = 3_600_000  # live orders kept for the bubble map
TRADES_MIN_USD = 1_000      # smaller orders aren't kept
TRADES_MAX_SEND = 800
BOOK_KEEP_MS = 45 * 60_000  # the liquidity map: order-book snapshots kept this long
BOOK_EVERY_MS = 3_000       # at most one snapshot per market this often (the newest in each slot wins)
BBO_EVERY_MS = 1_000        # best bid and offer, sampled
BOOK_LEVELS = 20
DEPTH_TARGET_BPS = 2.5      # grouped book for the map: price bands near this size (1–8 bps), so 20 levels reach ±0.2–1.6%
OWN_FEED_AFTER_S = 90       # no trades from the long-tail socket by then: open a socket of our own
COMP_HOURS = 168            # the compare chart: a week of hourly closes on one grid
ACTIVITY_DAYS = 30          # the heat grid: average hourly move by hour of day over this many days
CORR_MIN = 24               # hourly returns two markets need in common before their link is shown
LABELS = {"JPY": "USD/JPY", "EUR": "EUR/USD", "GBP": "GBP/USD", "DXY": "Dollar index"}
YEN_WATCH_1H_PCT = 0.5      # USD/JPY down this much in an hour, or YEN_WATCH_24H_PCT in a day: a fast yen rally
YEN_WATCH_24H_PCT = 1.0


async def fetch_dex_ctxs(dex: str) -> dict[str, dict]:
    """{"xyz:GOLD": {"mark", "oracle", "mid", "funding", "oi", "day_volume", "prev_day", "max_lev"}, ...}."""
    meta, ctxs = await _info({"type": "metaAndAssetCtxs", "dex": dex})
    out = {}
    for a, c in zip(meta.get("universe", []), ctxs):
        name = a.get("name")
        if not name:
            continue
        out[name if ":" in name else f"{dex}:{name}"] = {
            "mark": _num(c.get("markPx")), "oracle": _num(c.get("oraclePx")), "mid": _num(c.get("midPx")),
            "funding": _num(c.get("funding")), "oi": _num(c.get("openInterest")),
            "day_volume": _num(c.get("dayNtlVlm")), "prev_day": _num(c.get("prevDayPx")),
            "max_lev": _num(a.get("maxLeverage"))}
    return out


def depth_sig_figs(price: float, target_bps: float = DEPTH_TARGET_BPS) -> int:
    """Hyperliquid's nSigFigs (2 to 5) whose price band is nearest `target_bps` of the price (in log terms)."""
    best, dist = 5, None
    for n in (2, 3, 4, 5):
        band = 10 ** (math.floor(math.log10(price)) + 1 - n)
        d = abs(math.log(band / price * 1e4 / target_bps))
        if dist is None or d < dist:
            best, dist = n, d
    return best


def depth_band(price: float, n: int) -> float:
    return 10 ** (math.floor(math.log10(price)) + 1 - n)


def correlations(series: dict[str, list[float | None]], min_n: int = CORR_MIN) -> list[list[float | None]]:
    """Pearson correlation of hourly log returns, pair by pair, over the hours both markets have."""
    names = list(series)
    rets = {}
    for c, v in series.items():
        rets[c] = [math.log(b / a) if a and b else None for a, b in zip(v, v[1:])]
    m = [[None] * len(names) for _ in names]
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if j < i:
                m[i][j] = m[j][i]
                continue
            pairs = [(x, y) for x, y in zip(rets[a], rets[b]) if x is not None and y is not None]
            if len(pairs) < min_n:
                continue
            mx = sum(x for x, _ in pairs) / len(pairs)
            my = sum(y for _, y in pairs) / len(pairs)
            sxy = sum((x - mx) * (y - my) for x, y in pairs)
            sxx = sum((x - mx) ** 2 for x, _ in pairs)
            syy = sum((y - my) ** 2 for _, y in pairs)
            m[i][j] = round(sxy / math.sqrt(sxx * syy), 2) if sxx > 0 and syy > 0 else None
    return m


def _pct(a, b):
    return round((a / b - 1) * 100, 3) if a and b else None


BOARD = [
    {"key": "weekend", "name": "Weekend snap-back", "markets": "Oil, gold, silver",
     "idea": "While CME and ICE are shut, Hyperliquid alone prices these. At the reopen the real market answers, and "
             "past weekends' big moves gave part of themselves back.",
     "next": "Our own record below; then the same check on 5-minute data from our recordings."},
    {"key": "roll", "name": "Oil roll drift", "markets": "WTI, Brent",
     "idea": "From the 5th to the 10th business day the oracle moves from the front contract to the next. If the next "
             "is cheaper, the oracle drifts down by the gap with nothing else happening.",
     "next": "Watch whether Hyperliquid's price and funding see it coming."},
    {"key": "yen", "name": "Yen unwinds", "markets": "BTC, ETH, SOL, HYPE, gold",
     "idea": "A fast yen rally makes yen loans dearer, and carry trades get sold everywhere at once (Aug 2024).",
     "next": "Test whether sharp yen moves lead crypto over the next hours, on Hyperliquid's own yen history."},
    {"key": "holders", "name": "HYPE holder map", "markets": "HYPE",
     "idea": "Who holds HYPE and how each group behaves. The 7-day unstaking queue is the warning a holder may sell.",
     "next": "Collect balances and stakes for the addresses in our recordings."},
    {"key": "fresh", "name": "Fresh money", "markets": "BTC, ETH, SOL, HYPE",
     "idea": "A metric, not a person: the share of trading and new positions from wallets never seen here before, "
             "and money bridged in. A spike is the footprint (October 2025's crash short came from fresh money).",
     "next": "Measure the first-seen share in our recordings; test whether spikes lead big moves."},
    {"key": "listings", "name": "Listing footprints", "markets": "Long-tail coins",
     "idea": "A token reaching an exchange's wallets, or deposits opening, before the listing notice.",
     "next": "Line up past listing notices with on-chain transfers (Dune)."},
    {"key": "unlocks", "name": "Unlock selling", "markets": "Unlocking tokens",
     "idea": "Team and investor wallets sending unlocked tokens to exchanges or market makers around an unlock.",
     "next": "Unlock calendar plus labelled transfers."},
    {"key": "filings", "name": "SEC filings", "markets": "BTC, ETH, SOL, HYPE, ZEC",
     "idea": "Treasury companies and ETF issuers file before they buy or launch; the filing site posts before the "
             "headlines.",
     "next": "A watcher on the SEC's live filing feed."},
]


class EdgeService:
    def __init__(self, runtime, coins: list[str] | None = None):
        self.rt = runtime
        self.coins = list(EDGE_COINS if coins is None else coins)
        self.markets: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self.candles: dict[str, list[tuple[int, float]]] = {}
        self.futures: dict[str, dict] = {}
        self.status: dict[str, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._cache: tuple[int, dict] | None = None        # (when, weekend record): candles change every 30 min
        self.heavy_v = 0                                     # bumps when the hourly history changes
        self.orders: deque = deque(maxlen=20_000)            # live taker orders, oldest first
        self.trade_source: str | None = None
        self.last_trade_ms: int | None = None
        self.own_feed: HyperliquidClient | None = None
        self.books: dict[str, deque] = {}                    # coin → [(t, bids [(px, sz)], asks [(px, sz)])]
        self.bbos: dict[str, deque] = {}                     # coin → [(t, best bid, best ask)]
        self.depth_feed: HyperliquidClient | None = None     # grouped books for the map, on a socket of their own
        self.depth_sig: dict[str, int] = {}                  # coin → nSigFigs it's grouped by
        self.grouped: set[str] = set()                       # coins whose map book is the grouped one

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live" and bool(self.coins)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks = [asyncio.create_task(self._ctx_loop(), name="edges-ctx"),
                       asyncio.create_task(self._candle_loop(), name="edges-candles"),
                       asyncio.create_task(self._futures_loop(), name="edges-futures"),
                       asyncio.create_task(self._trade_feed(), name="edges-trades"),
                       asyncio.create_task(self._depth_loop(), name="edges-depth")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _ok(self, src: str) -> None:
        self.status[src] = {"ok": True, "last_ms": int(time.time() * 1000), "error": None}

    def _fail(self, src: str, err: str) -> None:
        st = self.status.setdefault(src, {"ok": False, "last_ms": None})
        st.update(ok=False, error=err[:200])

    # ── polling ───────────────────────────────────────────────────────────
    def take(self, ctxs: dict[str, dict], now_ms: int) -> None:
        """One poll's contexts: keep the wanted markets and a mark a minute for the 1-hour moves."""
        for c in self.coins:
            x = ctxs.get(c)
            if not x:
                continue
            self.markets[c] = dict(x, t_ms=now_ms)
            h = self.history.setdefault(c, deque())
            if x.get("mark") and (not h or now_ms - h[-1][0] >= 60_000):
                h.append((now_ms, x["mark"]))
            while h and now_ms - h[0][0] > HISTORY_KEEP_MS:
                h.popleft()

    async def _ctx_loop(self) -> None:
        dexes = list(dict.fromkeys(c.split(":", 1)[0] for c in self.coins if ":" in c))
        while True:
            for dex in dexes:
                try:
                    self.take(await fetch_dex_ctxs(dex), int(time.time() * 1000))
                    self._ok(f"Hyperliquid {dex}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._fail(f"Hyperliquid {dex}", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(CTX_EVERY_S)

    async def _candle_loop(self) -> None:
        while True:
            for c in self.coins:
                try:
                    rows = await fetch_candles(c, "1h", 5000)
                    self.candles[c] = [(int(r["t"]), float(r["c"])) for r in rows if r.get("c")]
                    self._cache = None
                    self.heavy_v += 1
                    self._ok("Hourly candles")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._fail("Hourly candles", f"{c}: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)
            await asyncio.sleep(CANDLES_EVERY_S)

    async def _futures_loop(self) -> None:
        while True:
            now = int(time.time() * 1000)
            symbols = [s for c in self.coins if E.bare(c) in E.ACTIVE for s in E.roll_state(c, now)["symbols"]]
            if symbols:
                got, errors = await fetch_quotes(symbols)
                self.futures.update(got)
                if got:
                    self._ok("Futures prices")
                if errors:
                    self._fail("Futures prices", "; ".join(f"{s}: {e}" for s, e in errors.items()))
            await asyncio.sleep(FUTURES_EVERY_S)

    def on_message(self, msg: dict) -> None:
        """One of our markets' messages: trades (fills from one taker order in one block become one order), book
        snapshots and best bid/offer (for the liquidity map)."""
        ch = msg.get("channel")
        if ch == "l2Book":                                   # the exact book: only until the grouped one arrives
            data = msg.get("data") or {}
            if data.get("coin") not in self.grouped:
                self._take_book(data)
            return
        if ch == "bbo":
            self._take_bbo(msg.get("data") or {})
            return
        if ch != "trades":
            return
        now = int(time.time() * 1000)
        for raw in msg.get("data") or []:
            coin = raw.get("coin")
            if coin not in self.coins:
                continue
            try:
                t = parse_trade(raw)
            except (KeyError, TypeError, ValueError):
                continue
            side = 1 if t.side.value == "B" else -1
            usd = t.price * t.size
            last = self.orders[-1] if self.orders else None
            if last and last["coin"] == coin and last["t"] == t.timestamp and last["side"] == side and last["_taker"] == t.taker:
                last["px"] = (last["px"] * last["usd"] + t.price * usd) / (last["usd"] + usd)
                last["usd"] += usd
                last["fills"] += 1
            else:
                self.orders.append({"id": f"{coin}:{t.timestamp}:{t.tid}", "t": t.timestamp, "coin": coin, "side": side,
                                    "usd": usd, "px": t.price, "fills": 1, "_taker": t.taker})
            self.last_trade_ms = now
        while self.orders and now - self.orders[0]["t"] > TRADES_KEEP_MS:
            self.orders.popleft()

    @staticmethod
    def _keep(dq: deque, t: int, row: tuple, every: int) -> None:
        """Append, or replace the last row while still inside its slot; drop rows older than BOOK_KEEP_MS."""
        if dq and t - dq[-1][0] < every:
            dq[-1] = row
        else:
            dq.append(row)
        while dq and t - dq[0][0] > BOOK_KEEP_MS:
            dq.popleft()

    def _take_book(self, data: dict) -> None:
        coin = data.get("coin")
        if coin not in self.coins:
            return
        try:
            t = int(data["time"])
            bids, asks = data["levels"]
            row = (t, [(float(l["px"]), float(l["sz"])) for l in bids[:BOOK_LEVELS]],
                   [(float(l["px"]), float(l["sz"])) for l in asks[:BOOK_LEVELS]])
        except (KeyError, TypeError, ValueError):
            return
        self._keep(self.books.setdefault(coin, deque()), t, row, BOOK_EVERY_MS)

    def _take_bbo(self, data: dict) -> None:
        coin = data.get("coin")
        if coin not in self.coins:
            return
        try:
            bid, ask = data["bbo"]
            row = (int(data["time"]), float(bid["px"]) if bid else None, float(ask["px"]) if ask else None)
        except (KeyError, TypeError, ValueError):
            return
        self._keep(self.bbos.setdefault(coin, deque()), row[0], row, BBO_EVERY_MS)

    def book_public(self, coin: str, since_ms: int | None = None, minutes: float = 20) -> dict:
        """One market's book snapshots (each level as [price, dollars resting]), best bid/offer and taker orders since
        `since` (or the last `minutes`)."""
        now = int(time.time() * 1000)
        lo = max(since_ms or 0, now - int(min(minutes, BOOK_KEEP_MS / 60_000) * 60_000))
        def usd3(p, z):                                      # dollars resting, to 3 significant figures: enough
            v = p * z                                        # for a colour, and it compresses far better
            return int(float(f"{v:.3g}")) if v >= 1 else round(v, 2)
        cols = [[t, [[p, usd3(p, z)] for p, z in b], [[p, usd3(p, z)] for p, z in a]]
                for t, b, a in self.books.get(coin, ()) if t > lo]
        bbo = [list(r) for r in self.bbos.get(coin, ()) if r[0] > lo]
        trades = [[o["t"], o["side"], round(o["usd"]), round(o["px"], 6)] for o in self.orders
                  if o["coin"] == coin and o["t"] > lo]
        mark = (self.markets.get(coin) or {}).get("mark")
        band = depth_band(mark, self.depth_sig[coin]) if coin in self.grouped and mark and coin in self.depth_sig else None
        return {"coin": coin, "now_ms": now, "cols": cols, "bbo": bbo, "trades": trades, "keep_ms": BOOK_KEEP_MS,
                "band": band,
                "every_ms": BOOK_EVERY_MS, "markets": [c for c in self.coins if self.books.get(c)],
                "enabled": self.enabled}

    def _on_depth(self, msg: dict) -> None:
        """The grouped books' socket: the first grouped book for a coin clears its exact-book history, so the map
        never mixes the two."""
        if msg.get("channel") != "l2Book":
            return
        data = msg.get("data") or {}
        coin = data.get("coin")
        if coin in self.coins and coin not in self.grouped:
            self.grouped.add(coin)
            self.books.pop(coin, None)
        self._take_book(data)
        if coin in self.coins:
            self._ok("Grouped books")

    async def _depth_loop(self) -> None:
        """Once every market has a price, subscribe to each one's book grouped into ~2.5 bps bands. Not recorded."""
        for _ in range(60):
            if all((self.markets.get(c) or {}).get("mark") for c in self.coins):
                break
            await asyncio.sleep(5)
        subs = []
        for c in self.coins:
            mark = (self.markets.get(c) or {}).get("mark")
            if not mark:
                continue
            self.depth_sig[c] = depth_sig_figs(mark)
            subs.append({"method": "subscribe", "subscription": {"type": "l2Book", "coin": c, "nSigFigs": self.depth_sig[c]}})
        if not subs:
            return
        self.depth_feed = HyperliquidClient([], self._on_depth, FeedStatus(mode="live"))
        self.depth_feed.set_subscriptions(subs)
        await self.depth_feed.run()

    async def _trade_feed(self) -> None:
        """Trades come from the long-tail socket when it carries our markets; otherwise from a socket of our own."""
        lt = getattr(self.rt, "longtail", None)
        waited = 0
        while waited < OWN_FEED_AFTER_S:
            if lt is not None and lt.carries_edges:
                self.trade_source = "long-tail socket"
                return
            await asyncio.sleep(5)
            waited += 5
        self.trade_source = "own socket"
        self.own_feed = HyperliquidClient([], self.on_message, FeedStatus(mode="live"))
        self.own_feed.set_subscriptions([x for c in self.coins for x in subscriptions(c)       # trades and best
                                         if x["subscription"]["type"] in ("trades", "bbo")])     # bid/offer
        await self.own_feed.run()

    def trades_public(self, since_ms: int | None = None, min_usd: float = TRADES_MIN_USD) -> dict:
        """New orders since `since` (past TRADES_MAX_SEND, the biggest), and from every order kept: each market's
        flow and the "key order" size (the biggest fifth) over 5, 15 and 60 minutes."""
        now = int(time.time() * 1000)
        lo = max(since_ms or 0, now - TRADES_KEEP_MS)
        # from `since` on, the same millisecond included (more of that block can arrive later; the page drops repeats)
        got = [o for o in self.orders if (o["t"] >= lo if since_ms else o["t"] > lo) and o["usd"] >= min_usd]
        if len(got) > TRADES_MAX_SEND:
            got = sorted(sorted(got, key=lambda o: -o["usd"])[:TRADES_MAX_SEND], key=lambda o: o["t"])
        flow, cut = {}, {}
        for mins in (5, 15, 60):
            recent = [o for o in self.orders if now - o["t"] <= mins * 60_000]
            sizes = sorted(o["usd"] for o in recent)
            cut[mins] = round(sizes[int(len(sizes) * 0.8)]) if sizes else 0
            for o in recent:
                f = flow.setdefault(o["coin"], {}).setdefault(mins, {"net": 0.0, "buy": 0.0, "sell": 0.0, "n": 0})
                f["net"] += o["side"] * o["usd"]
                f["buy" if o["side"] > 0 else "sell"] += o["usd"]
                f["n"] += 1
        for per in flow.values():
            for f in per.values():
                for k in ("net", "buy", "sell"):
                    f[k] = round(f[k])
        clean = [{k: (round(v, 6) if k == "px" else round(v) if k == "usd" else v) for k, v in o.items() if k != "_taker"}
                 for o in got]
        return {"now_ms": now, "orders": clean, "flow": flow, "cut": cut, "source": self.trade_source,
                "last_ms": self.last_trade_ms, "keep_ms": TRADES_KEEP_MS, "enabled": self.enabled}

    # ── reading ───────────────────────────────────────────────────────────
    def _move_1h(self, c: str, now_ms: int) -> float | None:
        mark = (self.markets.get(c) or {}).get("mark")
        h = self.history.get(c)
        if mark and h and now_ms - h[0][0] >= 55 * 60_000:
            then = next((p for t, p in reversed(h) if now_ms - t >= E.HOUR), h[0][1])
            return _pct(mark, then)
        rows = self.candles.get(c)
        if mark and rows:
            then = next((p for t, p in reversed(rows) if t + E.HOUR <= now_ms - E.HOUR), None)
            return _pct(mark, then)
        return None

    def record(self, now_ms: int) -> dict:
        """The weekend record per commodity and pooled: rows, fit, and fading the big moves."""
        if self._cache and now_ms - self._cache[0] < 60_000:
            return self._cache[1]
        per, pooled = {}, []
        for c in self.coins:
            if E.bare(c) not in E.SESSIONS:
                continue
            rows = E.weekend_rows(c, self.candles.get(c, []))
            pooled += rows
            per[c] = {"rows": rows[::-1], "n": len(rows), "fit": E.fit(rows), "fade": E.fade(rows)}
        out = {"markets": per, "pooled": {"n": len(pooled), "fit": E.fit(pooled), "fade": E.fade(pooled)},
               "published": E.PUBLISHED_FIT, "fade_min_bps": E.FADE_MIN_BPS, "cost_bps": 2 * E.TAKER_BPS}
        self._cache = (now_ms, out)
        return out

    def activity(self, days: int = ACTIVITY_DAYS) -> dict:
        """How much each market usually moves in each hour of the day (UTC), from its hourly closes over the last
        `days`: the average size of the hour's move, bps. The heat grid turns it to local time."""
        names, rows = [], []
        for c in self.coins:
            closes = self.candles.get(c, [])[-(days * 24 + 1):]
            if len(closes) < 48:
                continue
            sums, counts = [0.0] * 24, [0] * 24
            for (t0, a), (t1, b) in zip(closes, closes[1:]):
                if a and b and t1 - t0 == E.HOUR:
                    h = int(t0 // E.HOUR) % 24                # the hour (UTC) the move happened in
                    sums[h] += abs(math.log(b / a)) * 1e4
                    counts[h] += 1
            names.append(c)
            rows.append([round(sums[h] / counts[h], 1) if counts[h] else None for h in range(24)])
        return {"names": names, "m": rows, "days": days}

    def compare(self, now_ms: int) -> dict:
        """Every market's hourly closes on one grid for the last week (the live price as the last point), how they
        move together, and when the real market was open, for the shading."""
        end = int(now_ms // E.HOUR) * E.HOUR                  # the hour in progress
        hours = list(range(end - COMP_HOURS * E.HOUR, end + 1, E.HOUR))
        series = {}
        for c in self.coins:
            closes = dict(self.candles.get(c, []))
            if not closes:
                continue
            vals, last = [], None
            for t in hours:
                last = closes.get(t, last)
                vals.append(last)
            mark = (self.markets.get(c) or {}).get("mark")
            if mark:
                vals[-1] = mark
            if sum(v is not None for v in vals) >= 2:
                series[c] = [round(v, 6) if v is not None else None for v in vals]
        t0 = hours[0] + E.HOUR                                # each candle's close is at the end of its hour
        return {"t0": t0, "step": E.HOUR, "t_last": now_ms, "series": series,
                "corr": {"names": list(series), "m": correlations(series)},
                "open": E.open_intervals("CL", t0, now_ms), "market": "WTI, gold and silver"}

    def _market(self, c: str, now_ms: int, rec: dict) -> dict:
        x = self.markets.get(c) or {}
        b = E.bare(c)
        mark, oracle = x.get("mark"), x.get("oracle")
        oi = x.get("oi")
        row = {"name": c, "ticker": b, "label": E.SESSIONS[b].label if b in E.SESSIONS else LABELS.get(b, b),
               "kind": E.SESSIONS[b].kind if b in E.SESSIONS else "fx", "price": mark, "oracle": oracle,
               "basis_bps": round((mark / oracle - 1) * 1e4, 1) if mark and oracle else None,
               "funding_apr": round(x["funding"] * 24 * 365 * 100, 1) if x.get("funding") is not None else None,
               "oi_usd": round(oi * mark) if oi and mark else None, "day_volume": x.get("day_volume"),
               "change_24h_pct": _pct(mark, x.get("prev_day")), "change_1h_pct": self._move_1h(c, now_ms),
               "max_lev": x.get("max_lev"), "seen_ms": x.get("t_ms"),
               "spark": [round(p, 6) for _, p in self.candles.get(c, [])[-SPARK_HOURS:]] + ([mark] if mark else [])}
        if b not in E.SESSIONS:
            return row
        s = E.session_state(c, now_ms)
        row["session"] = s
        if not s["open"] and s["why"] in ("weekend", "holiday"):
            closes = dict(self.candles.get(c, []))
            ref = E._price_at(closes, s["since_ms"] - E.HOUR)
            bound = 1 / x["max_lev"] if x.get("max_lev") else E.SESSIONS[b].bound
            live = {"ref": ref, "bound_pct": round(bound * 100, 1),
                    "band": [round(ref * (1 - bound), 6), round(ref * (1 + bound), 6)] if ref else None}
            if ref and mark:
                w = math.log(mark / ref) * 1e4
                live["w_bps"] = round(w, 1)
                live["expect"] = E.expected_snap(w, (rec["markets"].get(c) or {}).get("fit"))
            row["closed"] = live
        return row

    def _roll(self, c: str, now_ms: int) -> dict:
        r = E.roll_state(c, now_ms)
        front, nxt = (self.futures.get(s) for s in r["symbols"])
        m = self.markets.get(c) or {}
        r.update(name=c, label=E.SESSIONS[E.bare(c)].label, front_px=front and front["px"],
                 next_px=nxt and nxt["px"], quote_ms=front and front["t_ms"],
                 drift=E.roll_drift(r, front and front["px"], nxt and nxt["px"]),
                 basis_bps=round((m["mark"] / m["oracle"] - 1) * 1e4, 1) if m.get("mark") and m.get("oracle") else None,
                 funding_apr=round(m["funding"] * 24 * 365 * 100, 1) if m.get("funding") is not None else None)
        return r

    def _events(self, now_ms: int) -> list[dict]:
        out = E.recurring(now_ms, 7)
        news = getattr(self.rt, "news", None)
        if news is not None:
            for it in news.store.upcoming(now_ms, days=7):
                if "crude oil inventories" in it["title"].lower():          # already there as the EIA report
                    continue
                country = it.get("country", "USD")
                out.append({"t_ms": it["t_ms"], "title": it["title"], "group": "macro",
                            "note": " · ".join(x for x in (
                                "Japan" if country == "JPY" else "US", it.get("impact"),
                                f"forecast {it['forecast']}" if it.get("forecast") else "") if x)})
        return sorted(out, key=lambda e: e["t_ms"])[:40]

    def _board(self, rec: dict, rolls: list[dict]) -> list[dict]:
        out = []
        recording = getattr(getattr(self.rt, "longtail", None), "edge_coins", []) or []
        for card in BOARD:
            c = dict(card, status="planned", line="")
            if card["key"] == "weekend":
                p = rec["pooled"]
                if p["n"]:
                    f = p["fade"]
                    c["status"] = "testing"
                    c["line"] = (f"{p['n']} weekends in Hyperliquid's hourly history. Fading moves of "
                                 f"{f['min_bps']:.0f}+ bps: "
                                 + (f"{f['n']} trades, {f['avg_bps']:+.0f} bps each after costs, {f['win']:.0%} won"
                                    if f["n"] else "none that big yet") + ".")
                else:
                    c["line"] = "Waiting for the hourly history."
            elif card["key"] == "roll" and rolls:
                r = rolls[0]
                c["status"] = "watching"
                c["line"] = (f"{'Now' if r['phase'] == 'during' else 'Next'}: {r['start']} to {r['end']}, "
                             f"{r['front']} → {r['next']}.")
            elif card["key"] == "yen":
                jpy = [x for x in recording if E.bare(x) == "JPY"]
                c["status"] = "watching" if jpy else "planned"
                c["line"] = "Recording USD/JPY from Hyperliquid." if jpy else ""
            elif card["key"] == "fresh":
                c["line"] = "The data is already in our recordings."
            out.append(c)
        return out

    def public(self, now_ms: int | None = None, have: int | None = None) -> dict:
        """Everything the terminal shows. The slow parts (the weekend record, the hourly history and what's built on
        it) are left out when the caller already has this version of them (`have`)."""
        now = int(now_ms or time.time() * 1000)
        light = have is not None and have == self.heavy_v and self.heavy_v > 0
        rec = self.record(now)
        markets = [self._market(c, now, rec) for c in self.coins]
        rolls = [self._roll(c, now) for c in self.coins if E.bare(c) in E.ACTIVE]
        fx = [m for m in markets if m["kind"] == "fx"]
        yen = next((m for m in fx if m["ticker"] == "JPY"), None)
        watch = None
        if yen:
            d1, d24 = yen.get("change_1h_pct"), yen.get("change_24h_pct")
            if (d1 is not None and d1 <= -YEN_WATCH_1H_PCT) or (d24 is not None and d24 <= -YEN_WATCH_24H_PCT):
                watch = "The yen is rallying fast: the kind of move that forced carry-trade selling in August 2024."
        longtail = getattr(self.rt, "longtail", None)
        return {"now_ms": now, "enabled": self.enabled, "mode": self.rt.mode, "coins": list(getattr(self.rt, "coins", [])),
                "markets": [m for m in markets if m["kind"] != "fx"], "fx": fx, "yen_watch": watch,
                "heavy_v": self.heavy_v, "light": light,
                "weekend": None if light else rec, "roll": rolls, "compare": None if light else self.compare(now),
                "activity": None if light else self.activity(), "events": self._events(now), "board": self._board(rec, rolls),
                "recording": {"on": bool(longtail and longtail.client), "markets": getattr(longtail, "edge_coins", [])},
                "fees": {"taker_bps": E.TAKER_BPS, "maker_bps": E.MAKER_BPS},
                "hours": {"from_ms": now - E.DAY, "to_ms": now + 6 * E.DAY, "market": "WTI, gold and silver",
                          "open": E.open_intervals("CL", now - E.DAY, now + 6 * E.DAY)},
                "status": self.status}

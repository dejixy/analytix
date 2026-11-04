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
    flow        every market's dollars bought and sold by takers, minute by minute, kept FLOW_KEEP_MS: live from
                the trades, and on a restart the hours before it read back from the long-tail recordings
    links       how the markets moved together over 7 and 30 days (hourly), and each strong pair's 24-hour rolling
                correlation through that time: whether a link is forming, loosening or flipping
    /api/edges/trades?since=ms   the orders since then (the world map's flow rings)
    /api/edges/flow?minutes=     the flow river: bought above, sold below, each market's normal pace beside it
    change log  what changed (moves, sessions, the roll, flow surges, events soon), written here on every market
                poll and kept on disk (api/edgelog.py), so the page shows the same history after a reload

Live mode only; in replay it still shows the schedule.
"""
import asyncio
import gzip
import json
import logging
import math
import re
import statistics
import time
from collections import deque
from pathlib import Path

from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import parse_trade, subscriptions

from api.edgelog import ChangeLog
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
TRADES_KEEP_MS = 3_600_000  # live orders kept for the world map's flow rings
TRADES_MIN_USD = 1_000      # smaller orders aren't sent
TRADES_MAX_SEND = 800
FLOW_KEEP_MS = 12 * E.HOUR + 30 * 60_000   # the flow river: a minute's bought and sold dollars kept this long
FLOW_BACKFILL_FILES = 14    # on start, the newest long-tail recordings read back (an hour each)
NORMAL_WEEKS = 8            # a market's normal pace: its median dollar volume at this weekday and hour, these weeks
OWN_FEED_AFTER_S = 90       # no trades from the long-tail socket by then: open a socket of our own
COMP_HOURS = 168            # the compare chart: a week of hourly closes on one grid
LINK_DAYS = (7, 30)         # market links: over the last week, or the last month
LINK_ROLL_H = 24            # each link line: the correlation of the 24 hours up to each hour
LINK_MIN = 0.35             # weaker links aren't drawn
LINK_LINES = 6              # at most this many pairs get a line (the strongest)
LINK_SHORT = {"EIA crude stocks": "EIA", "API crude stocks": "API", "CFTC positioning": "CFTC",
              "Baker Hughes rig count": "Rigs", "WTI, gold and silver close": "Close", "Oil, gold and silver reopen": "Reopen"}
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


def _corr(xs: list[tuple[float, float]]) -> float | None:
    n = len(xs)
    mx = sum(x for x, _ in xs) / n
    my = sum(y for _, y in xs) / n
    sxy = sum((x - mx) * (y - my) for x, y in xs)
    sxx = sum((x - mx) ** 2 for x, _ in xs)
    syy = sum((y - my) ** 2 for _, y in xs)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None


def _mean(xs: list) -> float | None:
    return sum(xs) / len(xs) if xs else None


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
     "next": "Run scripts/hypeProbe.py then scripts/hypeMap.py on the server: groups, what unlocks this week, and "
             "whether waves of unstaking come before weak HYPE."},
    {"key": "btc_est", "name": "BTC estimate", "markets": "BTC",
     "idea": "ETF flows, treasury-company buying, Strategy's premium and the order book in one equation for BTC's "
             "next 24 hours, fitted walk-forward.",
     "next": "Run scripts/btcEstimate.py on the server; tune on the first 80% of days, check the last 20% once."},
    {"key": "fresh", "name": "Fresh money", "markets": "BTC, ETH, SOL, HYPE",
     "idea": "A metric, not a person: the share of trading and new positions from wallets never seen here before, "
             "and money bridged in. A spike is the footprint (October 2025's crash short came from fresh money).",
     "next": "Rerun on new days only, same rule, once a month of fresh data has built up; drop it if it fails again."},
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
    def __init__(self, runtime, coins: list[str] | None = None, log_file=None):
        self.rt = runtime
        self.changes = ChangeLog(log_file)                  # None: kept in memory only (tests, replay)
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
        self.volumes: dict[str, dict[int, float]] = {}       # coin → hour → dollars traded (the hourly candles)
        self.flow: dict[str, dict[int, list[float]]] = {}    # coin → minute → [bought, sold] dollars
        self.flow_cut = int(time.time() * 1000)             # trades before this come from the recordings
        self.flow_from_ms: int | None = None                 # the oldest minute the river has
        self.backfilled: int | None = None                   # trades read back from the recordings (None: not yet)
        self._links: tuple[int, int, dict] | None = None     # (heavy_v, hour, links): recomputed when candles change

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live" and bool(self.coins)

    async def start(self) -> None:
        if not self.enabled:
            return
        self.flow_cut = int(time.time() * 1000)
        self.changes.add("sys", f"Server started · {len(self.coins)} markets", self.flow_cut)
        self._tasks = [asyncio.create_task(self._ctx_loop(), name="edges-ctx"),
                       asyncio.create_task(self._candle_loop(), name="edges-candles"),
                       asyncio.create_task(self._futures_loop(), name="edges-futures"),
                       asyncio.create_task(self._trade_feed(), name="edges-trades"),
                       asyncio.create_task(self._flow_backfill(), name="edges-backfill")]

    async def stop(self) -> None:
        self.changes.save(force=True)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _ok(self, src: str) -> None:
        now = int(time.time() * 1000)
        self.changes.status(src, (self.status.get(src) or {}).get("ok"), True, now)
        self.status[src] = {"ok": True, "last_ms": now, "error": None}

    def _fail(self, src: str, err: str) -> None:
        self.changes.status(src, (self.status.get(src) or {}).get("ok"), False, int(time.time() * 1000))
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
        self._watch(now_ms)

    # ── the change log ────────────────────────────────────────────────────
    def short(self, c: str) -> str:
        b = E.bare(c)
        return LABELS.get(b) or {"CL": "WTI", "BRENTOIL": "BRENT"}.get(b, b)

    def _watch(self, now_ms: int) -> None:
        """After every market poll: what changed since the last one goes in the log."""
        ch = self.changes
        states = {}
        for c in self.coins:
            b = E.bare(c)
            kind = E.SESSIONS[b].kind if b in E.SESSIONS else "fx"
            ch.move(self.short(c), c, kind, (self.markets.get(c) or {}).get("mark"), now_ms)
            if b in E.SESSIONS:
                states[c] = (self.short(c), E.session_state(c, now_ms))
            if self.flow.get(c):
                bought, sold = self.recent(c, now_ms, 15)
                norm = self.normal_rate(c, now_ms)
                ch.flow(c, self.short(c), (bought + sold) / 15 / norm if norm else None,
                        bought / (bought + sold) if bought + sold else None, now_ms)
            if b in E.ACTIVE:
                r = E.roll_state(c, now_ms)
                ch.roll(c, E.SESSIONS[b].label, r["front"], r["next"], r["front_weight"], now_ms)
        ch.sessions(states, now_ms)
        ch.soon(self._events(now_ms), now_ms)
        ch.yen_watch(self.yen_on(now_ms), now_ms)
        ch.save()

    def yen_on(self, now_ms: int) -> bool:
        """USD/JPY down YEN_WATCH_1H_PCT in an hour or YEN_WATCH_24H_PCT in a day: a fast yen rally."""
        c = next((x for x in self.coins if E.bare(x) == "JPY"), None)
        x = self.markets.get(c) or {} if c else {}
        if not x.get("mark"):
            return False
        d1, d24 = self._move_1h(c, now_ms), _pct(x["mark"], x.get("prev_day"))
        return (d1 is not None and d1 <= -YEN_WATCH_1H_PCT) or (d24 is not None and d24 <= -YEN_WATCH_24H_PCT)

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
                    self.volumes[c] = {int(r["t"]): float(r["v"]) * float(r["c"]) for r in rows if r.get("c") and r.get("v")}
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
        """One of our markets' messages; only trades are used (fills from one taker order in one block become one
        order), and every fill adds to its minute of the flow river."""
        if msg.get("channel") != "trades":
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
            if t.timestamp >= self.flow_cut:                   # earlier ones (a reconnect's catch-up) are the recordings'
                self._add_flow(self.flow, coin, t.timestamp, side, usd)
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

    # ── the flow river ────────────────────────────────────────────────────
    def _add_flow(self, flow: dict, coin: str, t_ms: int, side: int, usd: float) -> None:
        per = flow.setdefault(coin, {})
        m = t_ms // 60_000 * 60_000
        row = per.get(m)
        if row is None:
            row = per[m] = [0.0, 0.0]
            if flow is self.flow:
                self.flow_from_ms = m if self.flow_from_ms is None else min(self.flow_from_ms, m)
                old = [k for k in per if m - k > FLOW_KEEP_MS]
                for k in old:
                    del per[k]
        row[0 if side > 0 else 1] += usd

    def read_recordings(self, folder: str | Path, cut_ms: int, files: int = FLOW_BACKFILL_FILES) -> tuple[dict, int]:
        """Our markets' trades before `cut_ms` in the newest long-tail recordings, as minute flow (off the event
        loop: a fresh dict, merged in afterwards). Only trade lines naming a trade.xyz market are parsed."""
        stamp = re.compile(r"_(\d{8}_\d{2})00(?:_\d+)?\.jsonl(?:\.gz)?$")
        found = []
        for f in Path(folder).glob("longtail_*.jsonl*"):
            m = stamp.search(f.name)
            if m:
                found.append((m.group(1), f))
        lo = cut_ms - FLOW_KEEP_MS
        flow, n = {}, 0
        for _, f in sorted(found)[-files:]:
            opener = gzip.open if f.suffix == ".gz" else open
            try:
                with opener(f, "rt", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"trades"' not in line or '"coin":"xyz:' not in line:
                            continue
                        try:
                            msg = json.loads(line)["msg"]
                        except (ValueError, KeyError, TypeError):
                            continue                           # a line cut off by a crash
                        if msg.get("channel") != "trades":
                            continue
                        for raw in msg.get("data") or []:
                            if raw.get("coin") not in self.coins:
                                continue
                            try:
                                t = parse_trade(raw)
                            except (KeyError, TypeError, ValueError):
                                continue
                            if lo <= t.timestamp < cut_ms:
                                self._add_flow(flow, raw["coin"], t.timestamp, 1 if t.side.value == "B" else -1,
                                               t.price * t.size)
                                n += 1
            except (OSError, EOFError) as exc:                 # the hour being written, or a damaged file
                log.debug("flow backfill: %s skipped (%s)", f.name, exc)
        return flow, n

    def merge_flow(self, got: dict) -> None:
        for coin, per in got.items():
            mine = self.flow.setdefault(coin, {})
            for m, (b, s_) in per.items():
                row = mine.setdefault(m, [0.0, 0.0])
                row[0] += b
                row[1] += s_
                self.flow_from_ms = m if self.flow_from_ms is None else min(self.flow_from_ms, m)

    async def _flow_backfill(self) -> None:
        lt = getattr(self.rt, "longtail", None)
        folder = getattr(lt, "folder", None)
        if not folder or not Path(folder).exists():
            self.backfilled = 0
            return
        try:
            got, n = await asyncio.to_thread(self.read_recordings, folder, self.flow_cut)
            self.merge_flow(got)
            self.backfilled = n
            log.info("flow river: %d trades read back from the recordings", n)
        except Exception as exc:                               # the river still fills live
            self.backfilled = 0
            log.warning("flow backfill failed: %s: %s", type(exc).__name__, exc)

    def recent(self, coin: str, now_ms: int, mins: int) -> tuple[float, float]:
        """Dollars bought and sold by takers over the last `mins` minutes."""
        b = s_ = 0.0
        for m, (bb, ss) in self.flow.get(coin, {}).items():
            if now_ms - m < mins * 60_000:
                b += bb
                s_ += ss
        return b, s_

    def normal_rate(self, coin: str, t_ms: int) -> float | None:
        """A market's normal pace, dollars a minute: the median hourly dollar volume at this weekday and UTC hour over
        the last NORMAL_WEEKS weeks (oil at 3 pm New York is not oil on a Sunday night). Too few: the median hour."""
        vols = self.volumes.get(coin)
        if not vols:
            return None
        hour = t_ms // E.HOUR * E.HOUR
        same = [vols[h] for w in range(1, NORMAL_WEEKS + 1) if (h := hour - w * 7 * 24 * E.HOUR) in vols]
        if len(same) < 3:
            same = [v for h, v in vols.items() if 0 < hour - h <= 14 * 24 * E.HOUR]
        return statistics.median(same) / 60 if same else None

    def flow_public(self, minutes: float = 60, now_ms: int | None = None) -> dict:
        """The river over the last `minutes`: per market, dollars a minute bought and sold by takers in each step
        (1, 5 or 15 minutes), the last point a trailing step up to now, and the normal pace at each step's hour."""
        now = now_ms or int(time.time() * 1000)
        minutes = max(15, min(minutes, FLOW_KEEP_MS / 60_000 - 30))
        step = 60_000 if minutes <= 90 else 300_000 if minutes <= 360 else 900_000
        end = now // step * step
        n = int(minutes * 60_000 // step)
        t0 = end - n * step
        ts = [t0 + i * step + step // 2 for i in range(n)] + [now]
        tail = (now - step) // 60_000 * 60_000                    # the trailing point's first minute
        per_min = step / 60_000
        out = []
        for c in self.coins:
            b, s_ = [0.0] * (n + 1), [0.0] * (n + 1)
            pace_b = pace_s = 0.0
            for m, (bb, ss) in self.flow.get(c, {}).items():
                i = (m - t0) // step
                if 0 <= i < n:
                    b[i] += bb
                    s_[i] += ss
                if m >= tail:
                    b[n] += bb
                    s_[n] += ss
                if now - m < 15 * 60_000:
                    pace_b += bb
                    pace_s += ss
            tail_min = max((now - tail) / 60_000, 0.5)
            b = [round(x / per_min) for x in b[:n]] + [round(b[n] / tail_min)]
            s_ = [round(x / per_min) for x in s_[:n]] + [round(s_[n] / tail_min)]
            norm = [self.normal_rate(c, t) for t in ts]
            now_norm = norm[-1]
            horizons = {}
            for mins in (5, 15, 60):
                hb, hs = self.recent(c, now, mins)
                horizons[mins] = {"b": round(hb), "s": round(hs),
                                  "buy_x": round(hb / mins / (now_norm / 2), 2) if now_norm else None,
                                  "sell_x": round(hs / mins / (now_norm / 2), 2) if now_norm else None}
            out.append({"coin": c, "name": self.short(c), "horizons": horizons, "b": b, "s": s_, "norm": [round(x) if x else None for x in norm],
                        "bought": round(sum(x * per_min for x in b[:n])), "sold": round(sum(x * per_min for x in s_[:n])),
                        "pace": round((pace_b + pace_s) / 15 / now_norm, 2) if now_norm else None,
                        "pace_buy": round(pace_b / (pace_b + pace_s), 3) if pace_b + pace_s else None})
        return {"now_ms": now, "step_ms": step, "from_ms": t0, "t": ts, "markets": out, "data_from_ms": self.flow_from_ms,
                "events": self._past_events(t0, now), "backfilled": self.backfilled, "source": self.trade_source,
                "last_ms": self.last_trade_ms, "enabled": self.enabled}

    def _past_events(self, lo: int, hi: int) -> list[dict]:
        """The scheduled moments inside the river's window, to see what the flow did around them."""
        out = [e for e in E.recurring(lo, days=1) if not e["all_day"] and lo <= e["t_ms"] <= hi]
        news = getattr(self.rt, "news", None)
        if news is not None:
            for it in news.store.upcoming(lo, days=(hi - lo) / 86_400_000):
                if it["t_ms"] <= hi and "crude oil inventories" not in it["title"].lower():
                    out.append({"t_ms": it["t_ms"], "title": it["title"], "group": "macro"})
        return [{"t_ms": e["t_ms"], "title": e["title"], "group": e["group"]} for e in sorted(out, key=lambda e: e["t_ms"])]

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
        self.own_feed.set_subscriptions([x for c in self.coins for x in subscriptions(c)
                                         if x["subscription"]["type"] == "trades"])
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

    # ── market links ──────────────────────────────────────────────────────
    def links(self, now_ms: int) -> dict:
        """For each of LINK_DAYS: the correlation of every pair's hourly moves over the window, and for the strongest
        pairs (|r| >= LINK_MIN, at most LINK_LINES) the 24-hour rolling correlation at every hour, a verdict (forming,
        loosening, flipped, steady: the last 24 hours against the first), and its weakest hour (and whether the real
        oil and metals markets were shut then). Plus the window's weekend closures and scheduled moments."""
        hour = int(now_ms // E.HOUR)
        if self._links and self._links[0] == self.heavy_v and self._links[1] == hour:
            return self._links[2]
        end = hour * E.HOUR
        out = {}
        for days in LINK_DAYS:
            n = days * 24
            hours = list(range(end - (n + LINK_ROLL_H) * E.HOUR, end + 1, E.HOUR))
            rets, levels = {}, {}
            for c in self.coins:
                closes = dict(self.candles.get(c, []))
                if not closes:
                    continue
                vals, last = [], None
                for t in hours:
                    last = closes.get(t, last)
                    vals.append(last)
                r = [math.log(b / a) if a and b else None for a, b in zip(vals, vals[1:])]
                if sum(x is not None for x in r[-n:]) >= CORR_MIN:
                    rets[c], levels[c] = r, vals[-(n + 1):]
            names = list(rets)
            m = correlations(levels)
            pairs = [(names[i], names[j], m[i][j]) for i in range(len(names)) for j in range(i + 1, len(names))
                     if m[i][j] is not None]
            strong = sorted((p for p in pairs if abs(p[2]) >= LINK_MIN), key=lambda p: -abs(p[2]))[:LINK_LINES]
            t0 = hours[LINK_ROLL_H + 1]                        # the window's first hourly move ends here
            lines = []
            for a, b, r in strong:
                ra, rb = rets[a], rets[b]
                roll = []
                for k in range(LINK_ROLL_H, LINK_ROLL_H + n):
                    xs = [(x, y) for x, y in zip(ra[k - LINK_ROLL_H + 1:k + 1], rb[k - LINK_ROLL_H + 1:k + 1])
                          if x is not None and y is not None]
                    roll.append(_corr(xs) if len(xs) >= LINK_ROLL_H // 2 else None)
                got = [x for x in roll if x is not None]
                if not got:
                    continue
                first = _mean([x for x in roll[:LINK_ROLL_H] if x is not None]) or got[0]
                last = _mean([x for x in roll[-LINK_ROLL_H:] if x is not None]) or got[-1]
                if first * last < 0 and abs(first) > 0.15:
                    verdict = "flipped"
                elif abs(last - first) < 0.1:
                    verdict = "steady"
                else:
                    verdict = "forming" if abs(last) > abs(first) else "loosening"
                pos = [(i, x) for i, x in enumerate(roll) if x is not None]
                wi, wr = min(pos, key=lambda p: p[1]) if r > 0 else max(pos, key=lambda p: p[1])
                wt = t0 + wi * E.HOUR
                lines.append({"a": a, "b": b, "r": r, "series": [round(x, 3) if x is not None else None for x in roll],
                              "verdict": verdict, "first": round(first, 3), "last": round(last, 3),
                              "weakest": {"t_ms": wt, "r": round(wr, 3), "shut": not E.session_state("CL", wt)["open"]}})
            opens = E.open_intervals("CL", t0, now_ms)
            shut, prev = [], t0
            for a_, b_ in opens + [(now_ms, now_ms)]:
                if a_ - prev > 6 * E.HOUR:                        # closures longer than the daily break: weekends, holidays
                    shut.append([prev, a_])
                prev = b_
            events = [{"t_ms": e["t_ms"], "label": LINK_SHORT[e["title"]]} for e in E.recurring(t0, days + 1)
                      if e["title"] in LINK_SHORT and not e["all_day"] and t0 <= e["t_ms"] <= now_ms]
            out[days] = {"t0": t0, "step": E.HOUR, "names": names, "m": m, "lines": lines, "shut": shut, "events": events}
        self._links = (self.heavy_v, hour, out)
        return out

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
            elif card["key"] == "btc_est":
                c["status"] = "testing"
                c["line"] = "Equation written; waiting for the first fit on the server."
            elif card["key"] == "holders":
                c["status"] = "testing"
                c["line"] = "Probe and first map written, pass rule fixed in advance; waiting for the server run."
            elif card["key"] == "fresh":
                c["status"] = "failed"
                c["line"] = ("Tested 4 Nov on 16 days (27 spikes): 4-hour moves after a spike were 0.96x normal (t -0.2) "
                             "and +1 bp in its direction. Most spikes were one new wallet (69-88% of the fresh dollars).")
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
        watch = ("The yen is rallying fast: the kind of move that forced carry-trade selling in August 2024."
                 if self.yen_on(now) else None)
        longtail = getattr(self.rt, "longtail", None)
        return {"now_ms": now, "enabled": self.enabled, "mode": self.rt.mode, "coins": list(getattr(self.rt, "coins", [])),
                "markets": [m for m in markets if m["kind"] != "fx"], "fx": fx, "yen_watch": watch,
                "heavy_v": self.heavy_v, "light": light,
                "weekend": None if light else rec, "roll": rolls, "compare": None if light else self.compare(now),
                "activity": None if light else self.activity(), "links": None if light else self.links(now), "events": self._events(now), "board": self._board(rec, rolls),
                "recording": {"on": bool(longtail and longtail.client), "markets": getattr(longtail, "edge_coins", [])},
                "fees": {"taker_bps": E.TAKER_BPS, "maker_bps": E.MAKER_BPS},
                "hours": {"from_ms": now - E.DAY, "to_ms": now + 6 * E.DAY, "market": "WTI, gold and silver",
                          "open": E.open_intervals("CL", now - E.DAY, now + 6 * E.DAY)},
                "status": self.status,
                "log": self.changes.newest(), "log_since": self.changes.rows[0]["t"] if self.changes.rows else None}

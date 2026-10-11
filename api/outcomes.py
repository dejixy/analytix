"""
Outcomes: Hyperliquid's prediction markets (HIP-4), live. Covers every venue that lists on Hyperliquid's books:
Outcome (outcome.xyz) and trade.xyz today.

    every 60s     outcomeMeta: the market list (new markets in, settled ones out)
    live          a third socket follows each market's Yes book (l2Book) and market data (activeAssetCtx, which
                  replies on the activeSpotAssetCtx channel:
                  24h volume, price a day ago, contracts held), up to MAX_MARKETS markets
    every 30s     each market's odds (and each price market's perp price) kept for the last 6 hours
    every 5 min   how much each price market's perp has been moving (1-minute candles, last 12 hours)
    every 15s     prices of builder perps that markets settle on (trade.xyz's xyz:SILVER), when any do
    on open       a market's page: its trades are followed live; its odds history and recent trades fetched

Live mode only. /api/outcomes lists every market; /api/outcomes/{id} is one market's page.
"""
import asyncio
import bisect
import logging
import os
import time
from collections import Counter, deque
from dataclasses import asdict

from engine.outcomes import (
    VENUES, Book, Market, Question, build_markets, fair_above, fair_odds, fair_series, flow_of, ladder_check, odds,
    parse_levels, push_cost, sigma_per_sqrt_s, sum_check, trade_edge, who, why_line,
)
from api.intel import IntelService
from engine.intel import best_trade, returns, rules_line
from engine.sharp import resolved_value
from ingestion.account import _num
from ingestion.backfill import fetch_candles
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.outcomes import fetch_book, fetch_dex_marks, fetch_outcome_meta, fetch_trades, trade_row

log = logging.getLogger("analytix.outcomes")

META_EVERY_S = 60
SAMPLE_EVERY_S = 30
SAMPLES_KEPT = 6 * 3600 // SAMPLE_EVERY_S
VOL_WINDOWS = {                      # how much a perp moves, measured over a span that suits the market's length
    "short": ("1m", 720, 60, 300),   # closing within SHORT_S: 1-minute candles, the last 12 hours, every 5 min
    "long": ("1h", 336, 3600, 1800), # longer: hourly candles, the last 2 weeks (weekdays and weekends), every 30 min
}
SHORT_S = 6 * 3600
DEX_EVERY_S = 15
MAX_MARKETS = int(os.getenv("ANALYTIX_OUTCOME_MARKETS", "300"))   # two feeds each; Hyperliquid allows 1000 an IP
MAX_OPEN = 4                         # market pages whose trades are followed live at once
OPEN_IDLE_S = 600
TRADES_KEPT = 500
BOOK_DEPTH = 20
CANDLES_TTL_S = 120
RANGES = {"12h": ("1m", 720), "2w": ("1h", 336)}
FLOW_MARKETS = 100                   # the busiest markets also have their trades followed (sharp-wallet markouts)
EDGE_MIN_EV = 0.03                   # the edge board: at least +3% expected value…
EDGE_MIN_USD = 20                    # …and this many dollars available at that price
EDGE_BOARD = 12
EDGE_ALERT_USD = 50
EDGE_WARMUP_SAMPLES = 4              # no edge alerts for the first two minutes: books and sources are still arriving
EVENT_RANGES = {"12h": ("5m", 144), "2w": ("1h", 336)}   # lighter candles for an event's several lines
CHART_LINES = 5                      # an event's chart shows its five likeliest outcomes


def outcome_of(coin: str | None) -> int | None:
    """"#112140" → 11214 (Yes side only; No sides are mirrors and aren't followed)."""
    if not coin or not coin.startswith("#") or not coin[1:].isdigit():
        return None
    enc = int(coin[1:])
    return enc // 10 if enc % 10 == 0 else None


def _sub(kind: str, coin: str) -> dict:
    return {"method": "subscribe", "subscription": {"type": kind, "coin": coin}}


def _at(points, t: int):
    """The value of a time-sorted [(t, v)] series at moment t (the last point at or before it)."""
    if not points:
        return None
    i = bisect.bisect_right(points, t, key=lambda p: p[0]) - 1
    return points[i][1] if i >= 0 else None


class OutcomeService:
    def __init__(self, runtime, max_markets: int = MAX_MARKETS):
        self.rt = runtime
        self.max_markets = max_markets
        self.markets: dict[int, Market] = {}
        self.questions: dict[int, Question] = {}
        self.books: dict[str, Book] = {}                 # Yes coin → its book
        self.ctx: dict[str, dict] = {}                   # Yes coin → volume, price a day ago, contracts held
        self.last_px: dict[str, float] = {}              # Yes coin → last trade price (markets with a page open)
        self.samples: dict[int, deque] = {}              # outcome → [(ms, odds)] every 30s, last 6 hours
        self.under_hist: dict[str, deque] = {}           # perp → [(ms, price)] every 30s, last 6 hours
        self.trades: dict[int, list[dict]] = {}          # outcome with a page open → its recent trades, oldest first
        self.opened: dict[int, float] = {}               # outcome → when its page was last viewed
        self.vol: dict[tuple, tuple[float, float | None]] = {}   # (perp, "short"|"long") → (fetched at, sigma/sqrt s)
        self.dex_marks: dict[str, float] = {}            # "xyz:SILVER" → mark
        self._candles: dict[tuple, tuple[float, list]] = {}
        self._rest_books: dict[str, float] = {}
        self._followed: set[int] = set()
        self._flow: set[int] = set()                     # markets whose trades are followed for sharp-wallet markouts
        self.last_odds: dict[int, float] = {}            # outcome → its latest odds (how it ended, once it's gone)
        self._edge_armed: dict[tuple, bool] = {}
        self._samples_taken = 0
        self.intel = IntelService(self)
        self.updated_ms: int | None = None
        self.error: str | None = None
        self.status = FeedStatus(mode="live")
        self.sock: HyperliquidClient | None = None
        self._tasks: list[asyncio.Task] = []
        # swappable for tests
        self.fetch_meta, self.fetch_book, self.fetch_trades = fetch_outcome_meta, fetch_book, fetch_trades
        self.fetch_candles, self.fetch_dex_marks = fetch_candles, fetch_dex_marks

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live"

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.enabled:
            return
        self.sock = HyperliquidClient([], self.on_message, self.status)
        loops = (("outcomes-feed", self.sock.run()), ("outcomes-meta", self._meta_loop()),
                 ("outcomes-samples", self._sample_loop()), ("outcomes-vol", self._vol_loop()),
                 ("outcomes-dex", self._dex_loop()), ("outcomes-idle", self._idle_loop()))
        self._tasks = [asyncio.create_task(coro, name=name) for name, coro in loops]
        await self.intel.start(getattr(self.rt, "bars_db", None))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.intel.stop()

    async def _meta_loop(self) -> None:
        while True:
            try:
                await self.refresh_meta()
            except Exception as exc:
                self.error = "Couldn't read Hyperliquid's market list just now. Trying again."
                log.warning("outcomeMeta failed: %s", exc)
            await asyncio.sleep(META_EVERY_S)

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_EVERY_S)
            self.sample(int(time.time() * 1000))

    async def _vol_loop(self) -> None:
        while True:
            for perp in self.underlyings():
                for window, (_, _, _, every_s) in VOL_WINDOWS.items():
                    if time.time() - self.vol.get((perp, window), (0.0, None))[0] > every_s:
                        await self.refresh_vol(perp, window)
                        await asyncio.sleep(2)
            await asyncio.sleep(30)

    async def _dex_loop(self) -> None:
        while True:
            for dex in sorted({u.split(":", 1)[0] for u in self.underlyings() if ":" in u}):
                try:
                    self.dex_marks.update(await self.fetch_dex_marks(dex))
                except Exception as exc:
                    log.warning("prices for the %s perps unavailable: %s", dex, exc)
            await asyncio.sleep(DEX_EVERY_S)

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            stale = [o for o, t in self.opened.items() if time.time() - t > OPEN_IDLE_S]
            for o in stale:
                self._close(o)
            if stale:
                self._resubscribe()

    # ── the market list ────────────────────────────────────────────────────
    async def refresh_meta(self) -> None:
        meta = await self.fetch_meta()
        markets, questions = build_markets(meta if isinstance(meta, dict) else {})
        for gone in [o for o in self.markets if o not in markets]:          # settled: note how it ended
            result = resolved_value(self.last_odds.pop(gone, None))
            row = self.intel.calib.resolve(gone, result) if result is not None else self.intel.calib.snaps.pop(gone, None)
            if result is not None and row and self.intel.store:
                self.intel.store.save_result(gone, row[1], result)
        self.markets, self.questions = markets, questions
        live = {m.yes_coin for m in markets.values()}
        for d in (self.books, self.ctx, self.last_px):
            for k in [k for k in d if k not in live]:
                del d[k]
        for d in (self.samples, self.trades, self.opened):
            for k in [k for k in d if k not in markets]:
                del d[k]
        self.updated_ms, self.error = int(time.time() * 1000), None
        self._resubscribe()

    def underlyings(self) -> list[str]:
        return sorted({m.underlying for m in self.markets.values() if m.underlying and not m.settled})

    def followed(self) -> list[Market]:
        """The markets the socket follows live: all of them, or the busiest MAX_MARKETS (then the soonest to close)."""
        live = [m for m in self.markets.values() if not m.settled]
        if len(live) <= self.max_markets:
            return live
        return sorted(live, key=lambda m: (-((self.ctx.get(m.yes_coin) or {}).get("volume") or 0.0),
                                           m.expiry_ms or 1 << 62))[: self.max_markets]

    def _resubscribe(self) -> None:
        followed = self.followed()
        self._followed = {m.outcome for m in followed}
        if self.sock is None:
            return
        subs = [s for m in followed for s in (_sub("l2Book", m.yes_coin), _sub("activeAssetCtx", m.yes_coin))]
        busy = sorted((m for m in followed if ((self.ctx.get(m.yes_coin) or {}).get("volume") or 0) > 0),
                      key=lambda m: -((self.ctx.get(m.yes_coin) or {}).get("volume") or 0))[:FLOW_MARKETS]
        self._flow = {m.outcome for m in busy} | {o for o in self.opened if o in self.markets}
        subs += [_sub("trades", self.markets[o].yes_coin) for o in self._flow]
        self.sock.set_subscriptions(subs)

    # ── the live feed ──────────────────────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        ch, data = msg.get("channel"), msg.get("data")
        if ch in ("activeSpotAssetCtx", "activeAssetCtx") and isinstance(data, dict):
            c = data.get("ctx") or {}
            self.ctx[data.get("coin")] = {"mark": _num(c.get("markPx")), "prev_day": _num(c.get("prevDayPx")),
                                          "volume": _num(c.get("dayBaseVlm")), "open": _num(c.get("circulatingSupply"))}
        elif ch == "l2Book" and isinstance(data, dict):
            lv = data.get("levels") or [[], []]
            if len(lv) == 2:
                self.books[data.get("coin")] = Book(int(data.get("time") or 0), parse_levels(lv[0])[:BOOK_DEPTH],
                                                    parse_levels(lv[1])[:BOOK_DEPTH])
        elif ch == "trades" and isinstance(data, list):
            by: dict[int, list] = {}
            now = int(time.time() * 1000)
            for raw in data:
                oid = outcome_of(raw.get("coin")) if isinstance(raw, dict) else None
                if oid is None or oid not in self.markets:
                    continue
                row = trade_row(raw)
                if row:
                    self.intel.sharp.on_trade(oid, row, now)
                if oid in self.trades:
                    by.setdefault(oid, []).append(raw)
            for oid, rows in by.items():
                self.add_trades(oid, rows)

    def add_trades(self, oid: int, raws: list[dict]) -> None:
        have = self.trades.setdefault(oid, [])
        seen = {t["tid"] for t in have[-TRADES_KEPT:]}
        new = [r for r in (trade_row(x) for x in raws) if r and r["tid"] not in seen]
        if not new:
            return
        have.extend(new)
        have.sort(key=lambda t: t["t"])
        del have[:-TRADES_KEPT]
        self.last_px[self.markets[oid].yes_coin if oid in self.markets else f"#{10 * oid}"] = have[-1]["px"]

    def mid_of(self, oid: int) -> float | None:
        """The mid when the book is tight enough to mean something (for scoring markouts)."""
        m = self.markets.get(oid)
        b = self.books.get(m.yes_coin) if m else None
        if not b or b.bid is None or b.ask is None or b.ask - b.bid > 0.10:
            return None
        return (b.bid + b.ask) / 2

    def sample(self, now: int) -> None:
        cal, store = self.intel.calib, self.intel.store
        for m in self.markets.values():
            if m.outcome in self._followed:
                p = self.quote(m)[4]
                if p is not None:
                    self.samples.setdefault(m.outcome, deque(maxlen=SAMPLES_KEPT)).append((now, p))
                    self.last_odds[m.outcome] = p
                    if cal.note(m.outcome, m.expiry_ms, p, now) and store:
                        store.save_snaps(m.outcome, cal.snaps[m.outcome])
        self.intel.sharp.settle(now, self.mid_of)
        self._samples_taken += 1
        if self._samples_taken % 2 == 0:
            self.intel.save()
        self._edge_alerts(now)
        for perp in self.underlyings():
            px = self.spot(perp)
            if px:
                self.under_hist.setdefault(perp, deque(maxlen=SAMPLES_KEPT)).append((now, px))

    # ── prices ─────────────────────────────────────────────────────────────
    def quote(self, m: Market) -> tuple:
        """(bid, bid size, ask, ask size, odds) for Yes."""
        b = self.books.get(m.yes_coin)
        c = self.ctx.get(m.yes_coin) or {}
        bid, ask = (b.bid, b.ask) if b else (None, None)
        bsz = b.bids[0][1] if b and b.bids else 0.0
        asz = b.asks[0][1] if b and b.asks else 0.0
        last = self.last_px.get(m.yes_coin)
        if last is None and (bid is not None or ask is not None or (c.get("volume") or 0) > 0):
            last = c.get("mark")              # a market with no orders and no trades today has no real price
        return bid, bsz, ask, asz, odds(bid, ask, last)

    def spot(self, perp: str | None) -> float | None:
        """The live price of the perp a market settles on: the dashboard's own feed when it has one, then
        trade.xyz-style builder perps, then the scanner's 10-second market poll."""
        if not perp:
            return None
        pipe = self.rt.pipelines.get(perp)
        if pipe is not None and pipe.state.mid:
            return pipe.state.mid
        if perp in self.dex_marks:
            return self.dex_marks[perp]
        scanner = getattr(self.rt, "scanner", None)
        c = scanner.contexts.get(perp) if scanner is not None else None
        return (c or {}).get("mark")

    async def refresh_vol(self, perp: str, window: str = "short") -> float | None:
        interval, n, step_s, _ = VOL_WINDOWS[window]
        try:
            rows = await self.fetch_candles(perp, interval, n=n)
            sigma = sigma_per_sqrt_s([float(r["c"]) for r in rows], step_s)
        except Exception as exc:
            log.warning("candles for %s unavailable: %s", perp, exc)
            sigma = self.vol.get((perp, window), (0.0, None))[1]
        self.vol[(perp, window)] = (time.time(), sigma)
        return sigma

    @staticmethod
    def window_for(tau_s: float) -> str:
        return "short" if tau_s <= SHORT_S else "long"

    def sigma(self, perp: str | None, tau_s: float = 0.0) -> float | None:
        """How much `perp` moves (per square-root second), measured over the window that suits a market closing in
        tau_s seconds; the other window if that one isn't in yet."""
        if not perp:
            return None
        first = self.window_for(tau_s)
        for w in (first, "long" if first == "short" else "short"):
            s = self.vol.get((perp, w), (0.0, None))[1]
            if s:
                return s
        return None

    def fair(self, m: Market, now: int) -> float | None:
        tau = ((m.expiry_ms or now) - now) / 1000
        return fair_odds(m, self.spot(m.underlying), self.sigma(m.underlying, tau), now)

    def _odds_then(self, m: Market, t: int, candles: list | None = None):
        then = _at(list(self.samples.get(m.outcome) or ()), t)
        return then if then is not None else _at(candles or [], t)

    # ── what the list shows ────────────────────────────────────────────────
    def row(self, m: Market, now: int, candles: list | None = None) -> dict:
        bid, bsz, ask, asz, od = self.quote(m)
        c = self.ctx.get(m.yes_coin) or {}
        prev = c.get("prev_day")
        then = self._odds_then(m, now - 3_600_000, candles)
        model = self.fair(m, now) if m.kind in ("price", "touch") else None
        ref = self.intel.reference(m, now)
        if ref is None and model is not None:
            ref = {"p": min(0.99, max(0.01, model)), "source": "moves",
                   "label": "Recent moves of the perp (no options market for it)"}
        fair = ref["p"] if ref else None
        days = (m.expiry_ms - now) / 86_400_000 if m.expiry_ms and m.expiry_ms > now else None
        flow = self.intel.sharp.flow(m.outcome, now - 6 * 3_600_000)
        news = self.intel.news_for(self.intel.news_key(m))
        return {
            "id": m.outcome, "title": m.title, "label": m.label, "sub": m.sub, "question": m.question, "venue": m.venue,
            "venue_name": m.venue_name, "kind": m.kind, "sides": list(m.sides), "template": m.template,
            "odds": od, "bid": bid, "ask": ask, "bid_sz": bsz, "ask_sz": asz,
            "spread": (ask - bid) * 100 if bid is not None and ask is not None else None,
            "chg_24h": (od - prev) * 100 if od is not None and prev else None,
            "chg_1h": (od - then) * 100 if od is not None and then is not None else None,
            "volume_24h": c.get("volume"), "open": c.get("open"), "expiry_ms": m.expiry_ms,
            "underlying": m.underlying, "spot": self.spot(m.underlying) if m.underlying else None,
            "threshold": m.threshold, "fair": fair, "fair_model": model,
            "edge": trade_edge(fair, bid, ask),
            "ref": ref, "trade": best_trade(fair, bid, bsz, ask, asz, days) if fair is not None else None,
            "yes_returns": returns(ask, days), "no_returns": returns(1 - bid if bid is not None else None, days),
            "game": self.intel.chip(m),
            "news": news if news and news["spike"] else None,
            "sharp": {"net": flow["sharp_net"], "wallets": len(flow["sharp_wallets"])} if flow["sharp_wallets"] else None,
            "followed": m.outcome in self._followed, "settled": m.settled,
        }

    def edges(self, rows: list[dict], now: int) -> list[dict]:
        """The best trades right now: an independent chance against a price you can get, with real size behind it."""
        out = []
        for r in rows:
            t = r.get("trade")
            if not t or r["settled"] or not r["expiry_ms"] or r["expiry_ms"] <= now:
                continue
            if t["ev"] >= EDGE_MIN_EV and t["usd"] >= EDGE_MIN_USD:
                out.append({"id": r["id"], "title": r["title"], "venue_name": r["venue_name"], "kind": r["kind"],
                            "expiry_ms": r["expiry_ms"], "source": r["ref"]["source"], "label": r["ref"]["label"],
                            "question": r["question"], **t})
        return sorted(out, key=lambda e: -e["ev"])[:EDGE_BOARD]

    def _edge_alerts(self, now: int) -> None:
        svc = getattr(self.rt, "alerts", None)
        rule = (svc.cfg["rules"].get("pm_edge") or {}) if svc is not None else {}
        if svc is None or not rule.get("on") or self._samples_taken < EDGE_WARMUP_SAMPLES:
            return
        line = float(rule.get("min_ev_pct", 8.0)) / 100
        sent = 0
        for m in self.markets.values():
            r = self.row(m, now)
            t = r.get("trade")
            if r["settled"] or not r["expiry_ms"] or r["expiry_ms"] <= now:
                continue
            for side in ("Yes", "No"):
                key = (m.outcome, side)
                hit = t is not None and t["side"] == side and t["ev"] >= line and t["usd"] >= EDGE_ALERT_USD
                if hit and self._edge_armed.get(key, True):
                    self._edge_armed[key] = False
                    if sent < 3:
                        svc.push(edge_alert(r, now))
                        sent += 1
                elif not hit and (t is None or t["side"] != side or t["ev"] < line / 2):
                    self._edge_armed[key] = True

    def feed_status(self) -> dict:
        st = self.status
        return {"connected": st.connected, "reconnects": st.reconnects, "messages": st.messages,
                "last_error": st.last_error, "server_said": self.sock.server_error if self.sock else None,
                "books": len(self.books), "contexts": len(self.ctx), "feeds": 2 * len(self._followed)}

    def sum_for(self, q: Question):
        return sum_check([self.quote(self.markets[o]) for o in q.outcomes if o in self.markets])

    def public(self) -> dict:
        now = int(time.time() * 1000)
        venues = Counter(m.venue for m in self.markets.values())
        questions = []
        for q in self.questions.values():
            if not q.outcomes:
                continue
            venue = self.markets[q.outcomes[0]].venue
            questions.append({"id": q.question, "title": q.title, "sub": q.sub, "venue": venue,
                              "venue_name": VENUES.get(venue, venue), "expiry_ms": q.expiry_ms, "starts_ms": q.starts_ms,
                              "outcomes": list(q.outcomes), "sum": asdict(self.sum_for(q))})
        rows = [self.row(m, now) for m in self.markets.values()]
        by_asset: dict[tuple, list] = {}
        for r in rows:
            if r["kind"] == "price" and not r["settled"] and (r["expiry_ms"] or 0) > now:
                # same perp, same venue, same settlement price: only then must the ladder be in order
                by_asset.setdefault((r["underlying"], r["venue"], r["sub"]), []).append(r)
        ladders = [f for group in by_asset.values() for f in ladder_check(group)]
        return {
            "enabled": self.enabled, "updated_ms": self.updated_ms, "error": self.error, "now_ms": now,
            "total": len(self.markets), "followed": len(self._followed), "max_followed": self.max_markets,
            "feed": self.feed_status(),
            "venues": [{"id": v, "name": VENUES.get(v, v or "Hyperliquid"), "count": n} for v, n in venues.most_common()],
            "markets": rows,
            "questions": questions,
            "ladders": ladders,
            "edges": self.edges(rows, now),
            "intel": self.intel.public_status(),
            "calibration": self.intel.calib.table("6h"),
        }

    # ── one market's page ──────────────────────────────────────────────────
    def _close(self, oid: int) -> None:
        self.opened.pop(oid, None)
        self.trades.pop(oid, None)

    async def open(self, oid: int) -> bool:
        m = self.markets.get(oid)
        if m is None:
            return False
        fresh = oid not in self.opened
        self.opened[oid] = time.time()
        if fresh:
            while len(self.opened) > MAX_OPEN:
                self._close(min((o for o in self.opened if o != oid), key=lambda o: self.opened[o]))
            self.trades[oid] = []
            self._resubscribe()
            try:
                self.add_trades(oid, await self.fetch_trades(m.yes_coin) or [])
            except Exception as exc:
                log.warning("recent trades for %s unavailable: %s", m.yes_coin, exc)
        no_book = oid not in self._followed or m.yes_coin not in self.books   # not live, or not arrived yet
        if no_book and time.time() - self._rest_books.get(m.yes_coin, 0) > 5:
            self._rest_books[m.yes_coin] = time.time()
            try:
                self.on_message({"channel": "l2Book", "data": await self.fetch_book(m.yes_coin)})
            except Exception as exc:
                log.warning("book for %s unavailable: %s", m.yes_coin, exc)
        return True

    async def candles(self, coin: str, rng: str, table: dict = RANGES) -> list[tuple[int, float]]:
        """[(ms, close)] for the chart, cached for two minutes."""
        interval, n = table[rng]
        key = (coin, interval, n)
        hit = self._candles.get(key)
        if hit and time.time() - hit[0] < CANDLES_TTL_S:
            return hit[1]
        try:
            rows = await self.fetch_candles(coin, interval, n=n)
            pts = sorted((int(r["t"]), float(r["c"])) for r in rows)
        except Exception as exc:
            log.warning("candles for %s unavailable: %s", coin, exc)
            pts = hit[1] if hit else []
        self._candles[key] = (time.time(), pts)
        return pts

    async def detail(self, oid: int, rng: str = "12h") -> dict | None:
        if rng not in RANGES:
            rng = "12h"
        if not await self.open(oid):
            return None
        m = self.markets[oid]
        now = int(time.time() * 1000)
        hist = await self.candles(m.yes_coin, rng)
        under_hist: list = []
        tau = ((m.expiry_ms or now) - now) / 1000
        window = self.window_for(tau)
        if m.underlying:
            if (m.underlying, window) not in self.vol:
                await self.refresh_vol(m.underlying, window)
            under_hist = await self.candles(m.underlying, rng)
        sigma = self.sigma(m.underlying, tau)
        bid, bsz, ask, asz, od = self.quote(m)
        spot = self.spot(m.underlying)
        fair = self.fair(m, now) if m.kind in ("price", "touch") else None
        hour_ago, span = now - 3_600_000, "the last hour"
        then = self._odds_then(m, hour_ago, hist)
        if then is None and hist and now - hist[0][0] >= 300_000:   # younger than an hour: since its first trade
            hour_ago, then = hist[0][0], hist[0][1]
            span = f"the last {round((now - hour_ago) / 60_000)} minutes"
        spot_then = _at(list(self.under_hist.get(m.underlying) or ()), hour_ago) or _at(under_hist, hour_ago)
        fair_then = None
        if m.kind == "price" and m.expiry_ms and spot_then:
            fair_then = fair_above(spot_then, m.threshold, sigma, (m.expiry_ms - hour_ago) / 1000)
        trades = self.trades.get(oid) or []
        b = self.books.get(m.yes_coin) or Book(0)
        q = self.questions.get(m.question) if m.question is not None else None
        odds_pts = [[t, p] for t, p in hist]
        if od is not None:
            odds_pts.append([now, od])
        since = hist[0][0] if hist else now                 # the fair line starts where the market's history does
        fair_pts = [[t, p] for t, p in fair_series(m, under_hist, sigma) if t >= since]
        if fair is not None and m.kind == "price":
            fair_pts.append([now, fair])
        return {
            "now_ms": now, "range": rng, "row": self.row(m, now, hist), "description": m.fields,
            "book": {"bids": b.bids, "asks": b.asks, "t": b.t,
                     "up5": push_cost(b.asks, 5, up=True), "down5": push_cost(b.bids, 5, up=False)},
            "trades": trades[-100:][::-1],
            "who": who(trades),
            "why": why_line(m, od, then, span, flow_of(trades, hour_ago), fair, fair_then, spot, spot_then),
            "fair": None if fair is None else {
                "odds": fair, "spot": spot, "sigma_day_pct": sigma * (86_400 ** 0.5) * 100 if sigma else None,
                "window": "the last 12 hours" if window == "short" else "the last 2 weeks"},
            "chart": {"odds": odds_pts, "fair": fair_pts},
            "question": None if q is None else {
                "id": q.question, "title": q.title, "sub": q.sub, "sum": asdict(self.sum_for(q)),
                "outcomes": [self.row(self.markets[o], now) for o in q.outcomes if o in self.markets]},
            "intel": self.intel_block(m, q, oid, now),
        }

    def intel_block(self, m: Market, q: Question | None, oid: int, now: int) -> dict:
        """Everything independent about one market, for its page."""
        found = self.intel.game_of(m)
        flow = self.intel.sharp.flow(oid, now - 6 * 3_600_000)
        traded = {t.get("buyer") for t in self.trades.get(oid, [])} | {t.get("seller") for t in self.trades.get(oid, [])}
        known = [self.intel.sharp.wallet_row(w) for w in traded if w]
        known = sorted((w for w in known if w["label"]), key=lambda w: -abs(w["avg_2h"] or 0))[:6]
        return {
            "game": found[0] if found else None, "game_chip": self.intel.chip(m),
            "news": self.intel.headlines_for(self.intel.news_key(m)),
            "sharp": {**flow, "known_wallets": known, "summary": self.intel.sharp.summary()},
            "rules": rules_line({**(q.fields if q else {}), **m.fields}, m.kind),
        }

    # ── one event's page (a question: several outcomes, one ends Yes) ───────
    async def question_detail(self, qid: int, rng: str = "12h") -> dict | None:
        q = self.questions.get(qid)
        if q is None:
            return None
        if rng not in EVENT_RANGES:
            rng = "12h"
        now = int(time.time() * 1000)
        rows = [self.row(self.markets[o], now) for o in q.outcomes if o in self.markets]
        likeliest = sorted((r for r in rows if r["odds"] is not None), key=lambda r: -r["odds"])[:CHART_LINES]
        order = {o: i for i, o in enumerate(q.outcomes)}                # the listing's order: colours stay put
        series = []
        for r in sorted(likeliest, key=lambda r: order.get(r["id"], 0)):
            pts = [[t, p] for t, p in await self.candles(self.markets[r["id"]].yes_coin, rng, EVENT_RANGES)]
            pts.append([now, r["odds"]])
            series.append({"id": r["id"], "label": r["label"], "odds": r["odds"], "points": pts})
        venue = self.markets[q.outcomes[0]].venue if q.outcomes else ""
        return {
            "now_ms": now, "range": rng,
            "question": {"id": q.question, "title": q.title, "sub": q.sub, "venue_name": VENUES.get(venue, venue or "Hyperliquid"),
                         "expiry_ms": q.expiry_ms, "starts_ms": q.starts_ms, "sum": asdict(self.sum_for(q))},
            "rows": rows, "series": series,
            "game": (self.intel.matches.get(f"q{qid}") or (None,))[0],
            "game_chip": self.intel.chip(self.markets[q.outcomes[0]]) if q.outcomes else None,
            "news": self.intel.headlines_for(f"q{qid}"),
            "rules": rules_line(q.fields, "sports"),
            "volume_24h": sum(r["volume_24h"] or 0 for r in rows), "open": sum(r["open"] or 0 for r in rows),
        }


def edge_alert(r: dict, now: int):
    from engine.alerts import Alert
    t, ref = r["trade"], r["ref"]
    closes = ""
    if r.get("expiry_ms"):
        mins = (r["expiry_ms"] - now) / 60_000
        closes = f" Closes in {mins:.0f} minutes." if mins < 120 else f" Closes in {mins / 60:.0f} hours." if mins < 2880 \
            else f" Closes in {mins / 1440:.0f} days."
    title = f"{r['title']}: buy {t['side']} at {t['price'] * 100:.1f}¢"
    lines = (f"Independent estimate {t['p'] * 100:.0f}% for {t['side']} ({ref['label']}).",
             f"Expected value {t['ev'] * 100:+.0f}% per $1. A quarter-Kelly stake is {t['quarter_kelly'] * 100:.1f}% of a bankroll.",
             f"{t['contracts']:,.0f} contracts (${t['usd']:,.0f}) available at that price.{closes}",
             "Open it from Outcomes on the dashboard.")
    return Alert(f"pm-{r['id']}-{t['side']}-{now}", "PM", "pm_edge", now, "neutral", title, lines)

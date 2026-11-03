"""
News service: reads the economic calendar and crypto headlines (ingestion/news.py), keeps them, and measures how
each coin reacted, from the live minute bars.

    polling     each headline feed every RSS_EVERY_S (staggered), the calendar every CALENDAR_EVERY_S. Live mode only
    reaction    for an item at time t0, per coin it concerns (named coins; all followed coins for macro, Fed,
                regulation, ETF news and scheduled releases; otherwise the first followed coin as the market):
                    before      the move over the 10 minutes before t0 (markets often move before the headline)
                    after       the move from t0 to 30 minutes later (or to now, while it's still running)
                    activity    trades in those 30 minutes against the average 30 minutes of the day before
                from the live minute bars, which reach back about a day
    calendar    High-impact US releases are handed to engine/calendar.py, so the loud-market gauge expects them
                (Japan's are shown and kept, not fed to the gauge it was fitted without)
    /api/news   what's coming up (7 days), and the latest items with their reactions
"""
import asyncio
import logging
import time

import httpx

from config import NEWS, NEWS_FILE
from engine import calendar
from ingestion.news import CALENDAR_FEEDS, MARKET_WIDE, RSS_FEEDS, NewsStore, parse_calendar, parse_rss

log = logging.getLogger("analytix.news")

RSS_EVERY_S = 120
CALENDAR_EVERY_S = 1800
AFTER_MS = 30 * 60_000
BEFORE_MS = 10 * 60_000
STALE_MS = 5 * 60_000
DAY_MS = 86_400_000
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Analytix news reader)"}


def reaction(bars, t0: int, now: int) -> dict | None:
    """How one coin moved and traded around t0, from its minute bars (oldest first)."""
    closes = [(b.end_ms, b.close, b.fills) for b in bars if getattr(b, "span_s", 60) == 60]
    if not closes:
        return None

    def price_at(t):
        """The close of the last bar ending by t, if it ended within STALE_MS of t (no bars then: no price)."""
        best = None
        for end, close, _ in closes:
            if end <= t:
                best = (end, close)
            else:
                break
        return best[1] if best and t - best[0] <= STALE_MS else None

    p0, pb = price_at(t0), price_at(t0 - BEFORE_MS)
    end = min(now, t0 + AFTER_MS)
    p1 = price_at(end)
    if p0 is None or p1 is None:
        return None
    fills = sum(f for e, _, f in closes if t0 < e <= t0 + AFTER_MS)
    day = [f for e, _, f in closes if t0 - DAY_MS < e <= t0]
    usual = sum(day) / len(day) * 30 if day else None
    span = max(1, (end - t0) / 60_000)
    return {"before_bps": round((p0 / pb - 1) * 1e4, 1) if pb else None, "after_bps": round((p1 / p0 - 1) * 1e4, 1),
            "activity_x": round(fills * 30 / span / usual, 1) if usual else None, "done": now >= t0 + AFTER_MS}


class NewsService:
    def __init__(self, runtime, path=NEWS_FILE, enabled: bool = NEWS):
        self.rt = runtime
        self.on = enabled
        self.store = NewsStore(path)
        self.status: dict[str, dict] = {}
        self._done: dict[tuple[str, str], dict] = {}           # finished reactions, by (item id, coin)
        self._tasks: list[asyncio.Task] = []
        self._feed_calendar(list(self.store.items.values()))

    @property
    def enabled(self) -> bool:
        return self.on and self.rt.mode == "live"

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks = [asyncio.create_task(self._calendar_loop(), name="news-calendar")]
        for i, (src, url) in enumerate(RSS_FEEDS.items()):
            self._tasks.append(asyncio.create_task(self._rss_loop(src, url, i * RSS_EVERY_S / len(RSS_FEEDS)),
                                                   name=f"news-{src}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── reading ───────────────────────────────────────────────────────────
    def _ok(self, src: str, n: int) -> None:
        self.status[src] = {"ok": True, "last_ms": int(time.time() * 1000), "new": n, "error": None}

    def _fail(self, src: str, exc: Exception) -> None:
        st = self.status.setdefault(src, {"ok": False, "last_ms": None, "new": 0})
        st.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200])

    async def _rss_loop(self, src: str, url: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as http:
            while True:
                try:
                    r = await http.get(url)
                    r.raise_for_status()
                    new = self.store.add(parse_rss(r.text, src, int(time.time() * 1000)))
                    self._ok(src, len(new))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._fail(src, exc)
                await asyncio.sleep(RSS_EVERY_S)

    async def _calendar_loop(self) -> None:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as http:
            while True:
                got = 0
                for url in CALENDAR_FEEDS:
                    try:
                        r = await http.get(url)
                        if r.status_code == 404:                   # next week's isn't always published yet
                            continue
                        r.raise_for_status()
                        now = int(time.time() * 1000)
                        payload = r.json()
                        items = parse_calendar(payload, now) + parse_calendar(payload, now, ("JPY",), ("High",))
                        new = self.store.add(items)
                        self._feed_calendar(items)
                        got += len(new)
                        self._ok("Calendar", got)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._fail("Calendar", exc)
                await asyncio.sleep(CALENDAR_EVERY_S)

    @staticmethod
    def _feed_calendar(items: list[dict]) -> None:
        """US releases only: the loud-market forecast was fitted on US releases."""
        calendar.add_live([(it["t_ms"], it["title"], it.get("impact")) for it in items
                           if it.get("kind") == "scheduled" and it.get("country", "USD") == "USD"])

    # ── reactions ─────────────────────────────────────────────────────────
    def coins_for(self, it: dict) -> list[str]:
        mine = [c for c in it.get("coins", []) if c in self.rt.coins]
        if mine:
            return mine
        if it["kind"] == "scheduled" or it.get("category") in MARKET_WIDE:
            return list(self.rt.coins)
        return list(self.rt.coins[:1])

    def reactions(self, it: dict, now: int) -> dict:
        out = {}
        for c in self.coins_for(it):
            key = (it["id"], c)
            if key in self._done:
                out[c] = self._done[key]
                continue
            pipe = self.rt.pipelines.get(c)
            r = reaction(list(pipe.state.bars), it["t_ms"], now) if pipe is not None else None
            if r is not None:
                out[c] = r
                if r["done"]:
                    self._done[key] = r
        return out

    def public(self, hours: float = 24, coin: str | None = None) -> dict:
        now = int(time.time() * 1000)

        def mine(it):
            return coin is None or coin in self.coins_for(it)
        recent = [dict(it, reaction=self.reactions(it, now)) for it in self.store.recent(now, hours) if mine(it)][:80]
        return {"now_ms": now, "upcoming": [it for it in self.store.upcoming(now) if mine(it)][:30], "recent": recent,
                "status": self.status, "enabled": self.enabled}

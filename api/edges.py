"""
Edge service: the live data behind the edge terminal (/terminal), from trade.xyz's builder markets on Hyperliquid
and the schedule in engine/edges.py.

    markets     every EDGE_COINS market's mark, oracle, funding, open interest and volume (metaAndAssetCtxs per
                builder, every CTX_EVERY_S); a minute-by-minute mark history in memory for 1-hour moves
    candles     each market's last 5000 hourly candles (about 7 months), every CANDLES_EVERY_S: the weekend record
    futures     the oil roll's front and next contracts from Yahoo Finance, every FUTURES_EVERY_S (optional)
    /api/edges  sessions, the live weekend (when shut), the weekend record and fits, the roll, currencies,
                what's coming up and the edge board

Live mode only; in replay it still shows the schedule.
"""
import asyncio
import logging
import math
import time
from collections import deque

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
LABELS = {"JPY": "USD/JPY", "EUR": "EUR/USD", "GBP": "GBP/USD"}
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
    {"key": "fresh", "name": "Fresh whales", "markets": "BTC, ETH, SOL, HYPE",
     "idea": "A brand-new wallet bridging in big money and opening one large position, like October 2025.",
     "next": "Find them in the recordings and measure the next hours."},
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

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live" and bool(self.coins)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks = [asyncio.create_task(self._ctx_loop(), name="edges-ctx"),
                       asyncio.create_task(self._candle_loop(), name="edges-candles"),
                       asyncio.create_task(self._futures_loop(), name="edges-futures")]

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
               "max_lev": x.get("max_lev"), "seen_ms": x.get("t_ms")}
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

    def public(self, now_ms: int | None = None) -> dict:
        now = int(now_ms or time.time() * 1000)
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
                "weekend": rec, "roll": rolls, "events": self._events(now), "board": self._board(rec, rolls),
                "recording": {"on": bool(longtail and longtail.client), "markets": getattr(longtail, "edge_coins", [])},
                "fees": {"taker_bps": E.TAKER_BPS, "maker_bps": E.MAKER_BPS},
                "status": self.status}

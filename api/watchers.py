"""
The watchers, live (engine/watchers.py has the rules): every minute they read the edge terminal's trade.xyz markets
and futures prices, the scanner's main perps, Hyperliquid's spot prices and each carry coin's funding history, open
tickets when a rule fires, follow every open ticket as a paper trade, and close it by its own rule.

Each ticket opened or closed is an alert (the "Watchers" rule in alert settings; Telegram when it's set up) and a
line in the terminal's change log. Tickets are kept in data/watchers.json (live mode only), so the record survives
restarts. The terminal reads it all from /api/edges ("watch").
"""
import asyncio
import logging
import time

from engine import edges as E
from engine import watchers as W
from engine.alerts import Alert
from ingestion.account import fetch_funding_history, fetch_spot_prices

log = logging.getLogger("analytix.watchers")

EVERY_S = 60
SPOT_EVERY_S = 60
HISTORY_EVERY_S = 3600
HISTORY_DAYS = 4
RULE_TEXT = {
    "roll": (f"Takes the side of the oil roll's oracle move that Hyperliquid hasn't priced in, when it expects "
             f"{W.ROLL_MIN_BPS:.0f}+ bps after funding and an {W.ROLL_COST_BPS:.0f} bps round trip. Opens only while the "
             f"real market is open; closes 30 minutes after the last step."),
    "carry": (f"Buy spot and short the perp when funding has averaged {W.CARRY_ENTER_APR:.0f}%+ a year over 3 days and "
              f"is {W.CARRY_NOW_APR:.0f}%+ now. Closes when a day averages under {W.CARRY_EXIT_APR:.0f}%, or after "
              f"{W.CARRY_MAX_DAYS} days."),
}


class WatcherService:
    def __init__(self, runtime, path=None):
        self.rt = runtime
        self.book = W.Book(path)
        self.spot: dict[str, dict] = {}
        self.hist: dict[str, list[tuple[int, float]]] = {}
        self.reads: dict[str, list[dict]] = {"roll": [], "carry": []}
        self.updated_ms: int | None = None
        self.errors: dict[str, str] = {}
        self._spot_at = 0.0
        self._hist_at: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.fetch_spot, self.fetch_hist = fetch_spot_prices, fetch_funding_history     # swappable in tests

    async def start(self) -> None:
        if self.rt.mode == "live":
            self._tasks = [asyncio.create_task(self._loop(), name="watchers")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.book.save()

    async def _loop(self) -> None:
        await asyncio.sleep(90)                          # let the markets, futures and scanner fill in first
        while True:
            try:
                await self.refresh()
                self.step(int(time.time() * 1000))
            except asyncio.CancelledError:
                raise
            except Exception:                            # one bad minute must not stop the watchers for good
                log.exception("watchers step failed")
            await asyncio.sleep(EVERY_S)

    async def refresh(self) -> None:
        now = time.time()
        if now - self._spot_at >= SPOT_EVERY_S:
            try:
                self.spot = await self.fetch_spot()
                self._spot_at = now
                self.errors.pop("spot", None)
            except Exception as exc:                     # noqa: BLE001
                self.errors["spot"] = f"{type(exc).__name__}: {exc}"[:160]
        for coin in W.CARRY_SPOT:
            if now - self._hist_at.get(coin, 0) < HISTORY_EVERY_S:
                continue
            try:
                self.hist[coin] = await self.fetch_hist(coin, int(now * 1000) - HISTORY_DAYS * W.DAY)
                self._hist_at[coin] = now
                self.errors.pop(f"funding {coin}", None)
            except Exception as exc:                     # noqa: BLE001
                self.errors[f"funding {coin}"] = f"{type(exc).__name__}: {exc}"[:160]
            await asyncio.sleep(1)

    # ── one pass ───────────────────────────────────────────────────────────
    def step(self, now: int) -> dict:
        """Read every market, open what fires, follow and close what's open. {"opened": [...], "closed": [...]}."""
        edges, scanner = self.rt.edges, getattr(self.rt, "scanner", None)
        ctxs = getattr(scanner, "contexts", {}) or {}
        opened, closed = [], []
        rolls = []
        for c in edges.coins:
            if E.bare(c) not in E.ACTIVE:
                continue
            st = E.roll_state(c, now)
            fq, nq = (edges.futures.get(s) for s in st["symbols"])
            read = W.roll_read(c, st, fq and fq.get("px"), nq and nq.get("px"), (fq or {}).get("t_ms"),
                               edges.markets.get(c) or {}, now)
            t = W.roll_ticket(read, E.session_state(c, now)["open"], now, W.day_move_pct(edges.candles.get(c, [])))
            if t and not self.book.has(t["key"]):
                opened.append(self.book.add(t))
            read["ticket_open"] = any(x["market"] == c for x in self.book.open_tickets("roll"))
            rolls.append(read)
        carries = []
        for coin, tok in W.CARRY_SPOT.items():
            read = W.carry_read(coin, self.hist.get(coin, []), ctxs.get(coin) or {}, self.spot.get(tok), now)
            mine = [t for t in self.book.open_tickets("carry") if t["market"] == coin]
            if mine and W.carry_done(read):
                closed += [self.book.close(t, now, "funding faded") for t in mine]
            elif not self.book.resting("carry", coin, now, W.CARRY_REST_H):
                t = W.carry_ticket(read, now)
                if t and not self.book.has(t["key"]):
                    opened.append(self.book.add(t))
            read["ticket_open"] = any(x["market"] == coin for x in self.book.open_tickets("carry"))
            carries.append(read)
        prices, funding = {}, {}
        for c, m in edges.markets.items():
            prices[("perp", c)] = m.get("mark")
            funding[c] = m.get("funding")
        for c, m in ctxs.items():
            prices[("perp", c)] = m.get("mark")
            funding[c] = m.get("funding")
        for tok, s in self.spot.items():
            prices[("spot", tok)] = s.get("px")
        self.book.mark(prices, funding, now)
        closed += self.book.expire(now)
        self.reads = {"roll": rolls, "carry": carries}
        self.updated_ms = now
        for t in opened:
            self._say(t, now, opened=True)
        for t in closed:
            self._say(t, now, opened=False)
        if opened or closed:
            self.book.save()
        return {"opened": opened, "closed": closed}

    def _say(self, t: dict, now: int, opened: bool) -> None:
        if opened:
            title = f"WATCH · {t['title']}"
            lines = [*t["why"], f"Exit: {t['exit']}", "Paper-tracked on the terminal (WATCH)."]
            log_text = t["title"]
            direction = "up" if t["side"] == "long" else "down" if t["side"] == "short" else "neutral"
        else:
            r = t["result"]
            title = f"WATCH · Close: {t['title'].split(':')[0]}: {r['net_bps']:+.0f} bps after costs"
            lines = [f"Closed ({t['closed_why']}). Price {r['price_bps']:+.0f} bps, funding {r['funding_bps']:+.0f} bps, "
                     f"costs {r['cost_bps']:.0f} bps; it expected {t['expect_bps']:+.0f} bps."]
            log_text = f"Closed: {t['title'].split(':')[0]}, {r['net_bps']:+.0f} bps after costs (expected {t['expect_bps']:+.0f})"
            direction = "neutral"
        ch = getattr(self.rt.edges, "changes", None)
        if ch is not None:
            ch.add("watch", log_text, now)
        svc = getattr(self.rt, "alerts", None)
        if svc is not None and (svc.cfg["rules"].get("watch") or {}).get("on", True):
            svc.push(Alert(f"watch-{t['id']}-{'open' if opened else 'close'}", t["market"], "watch", now, direction,
                           title, tuple(lines)))

    # ── for the terminal ──────────────────────────────────────────────────
    def public(self, now_ms: int | None = None) -> dict:
        now = int(now_ms or time.time() * 1000)
        open_ = [{**self._slim(t), "pnl": W.pnl(t)} for t in self.book.open_tickets()]
        closed = [self._slim(t) for t in self.book.tickets if t["status"] == "closed"][-12:][::-1]
        return {"updated_ms": self.updated_ms, "now_ms": now, "errors": self.errors,
                "watchers": [{"key": k, "name": W.NAMES[k], "rule": RULE_TEXT[k], "record": self.book.record(k),
                              "reads": self.reads.get(k, [])} for k in ("roll", "carry")],
                "open": open_, "closed": closed}

    @staticmethod
    def _slim(t: dict) -> dict:
        keep = ("id", "watcher", "market", "title", "side", "opened_ms", "expires_ms", "expect_bps", "cost_bps", "why",
                "exit", "legs", "status", "closed_ms", "closed_why", "result", "gaps")
        return {k: t.get(k) for k in keep}

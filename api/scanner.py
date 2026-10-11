"""
Scanner service: keeps every perp's market data fresh, learns each coin's normal, follows the trades of the
most traded coins, and ranks them all.

    every 10s        one metaAndAssetCtxs call: price, index, funding, open interest, 24h volume, all coins
    every 5s         one coin's last 7 days of hourly candles, busiest coins first, each refreshed every 6h
    every 30 min     re-pick the 30 most traded coins; the runtime's second socket follows their trades
                     (coins that already have a dashboard are read from it instead)

Live mode only. Rankings are rebuilt after every market poll and served by /api/scanner.

Alerts (the "Unusual coin" rule, threshold N): a coin outside the main tabs (those have their own alerts)
whose score passes N, i.e. a reading N times past its "worth a look" line. Once per episode: it re-arms when
the score falls back below half of N, so a coin that stays unusual (funding high all day) pings once. Coins
already past the line when the server starts are marked as seen without a ping, and at most
MAX_ALERTS_PER_POLL go out at once (a market-wide move would otherwise ping for every coin).
"""
import asyncio
import logging
import time
from dataclasses import asdict

from engine.scanner import Baseline, CoinHistory, Row, baseline_from_candles, score_coin, trade_reads
from ingestion.account import fetch_contexts
from ingestion.backfill import fetch_candles
from ingestion.parsers import parse_message
from state import MarketState

log = logging.getLogger("analytix.scanner")

POLL_S = 10
CANDLE_EVERY_S = 5
BASELINE_TTL_S = 6 * 3600
TOP_N = 30
TOP_EVERY_S = 1800
CANDLES_7D = 7 * 24 + 2
MAX_ALERTS_PER_POLL = 3


class Scanner:
    def __init__(self, runtime):
        self.rt = runtime
        self.contexts: dict[str, dict] = {}
        self.history: dict[str, CoinHistory] = {}
        self.baselines: dict[str, Baseline] = {}
        self.states: dict[str, MarketState] = {}          # trades-only state for top coins without a dashboard
        self.top: list[str] = []
        self.rows: list[Row] = []
        self.updated_ms: int | None = None
        self.error: str | None = None
        self._top_at = 0.0
        self._armed: dict[str, bool] = {}                 # coin → may alert (False until it calms down again)
        self._alerts_ready = False                        # the first full poll only marks what's already unusual
        self._skip: dict[str, float] = {}                 # coin → when its candles last failed or fell short
        self._tasks: list[asyncio.Task] = []
        self.on_top_change = None                         # set by the runtime: re-subscribe the trades feed
        self.fetch_contexts, self.fetch_candles = fetch_contexts, fetch_candles   # swappable for tests and demos

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live"

    def knows(self, coin: str) -> bool:
        return coin in self.contexts

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.enabled:
            self._tasks = [asyncio.create_task(self._poll_loop(), name="scanner"),
                           asyncio.create_task(self._baseline_loop(), name="scanner-candles")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception as exc:
                self.error = "Couldn't read Hyperliquid's market data just now. Trying again."
                log.warning("scanner poll failed: %s", exc)
            await asyncio.sleep(POLL_S)

    async def _baseline_loop(self) -> None:
        while True:
            coin = self._next_baseline()
            if coin is None:
                await asyncio.sleep(60)
                continue
            try:
                rows = await self.fetch_candles(coin, "1h", n=CANDLES_7D)
                base = baseline_from_candles(rows, int(time.time() * 1000))
                if base:
                    self.baselines[coin] = base
                else:                                       # too new to have a week: try again in 6 hours
                    self.baselines.pop(coin, None)
                    self._skip[coin] = time.time()
            except Exception as exc:
                log.warning("candles for %s unavailable: %s", coin, exc)
                self._skip[coin] = time.time() - BASELINE_TTL_S + 600      # retry in 10 minutes
            await asyncio.sleep(CANDLE_EVERY_S)

    def _next_baseline(self) -> str | None:
        now = time.time()
        due = [c for c in self.contexts
               if (c not in self.baselines or now * 1000 - self.baselines[c].fetched_ms > BASELINE_TTL_S * 1000)
               and now - self._skip.get(c, 0) > BASELINE_TTL_S]
        return max(due, key=lambda c: self.contexts[c].get("day_volume") or 0.0) if due else None

    # ── the poll ───────────────────────────────────────────────────────────
    async def poll(self) -> None:
        ctxs = await self.fetch_contexts()
        now = int(time.time() * 1000)
        self.contexts = ctxs
        self.error = None
        for coin, c in ctxs.items():
            self.history.setdefault(coin, CoinHistory()).add(now, c.get("mark"), c.get("oi") or 0.0)
        for gone in [c for c in self.history if c not in ctxs]:
            del self.history[gone]
        if time.time() - self._top_at >= TOP_EVERY_S or not self.top:
            self._pick_top()
        t = time.perf_counter()
        self.rows = self.rank(now)
        self.updated_ms = now
        perf = getattr(self.rt, "perf", None)
        if perf is not None:
            perf.scanner.add((time.perf_counter() - t) * 1000)
        self._alerts(now)

    def _alerts(self, now: int) -> None:
        svc = getattr(self.rt, "alerts", None)
        rule = (svc.cfg["rules"].get("scanner") or {}) if svc is not None else {}
        if svc is None or not rule.get("on"):
            return
        line = float(rule.get("min_score", 3.0))
        main = set(self.rt.coins)
        fired = []
        for r in self.rows:
            if r.coin in main:
                continue
            if r.score >= line:
                if self._armed.get(r.coin, True):
                    self._armed[r.coin] = False
                    if self._alerts_ready:
                        fired.append(r)
            elif r.score < line / 2:
                self._armed[r.coin] = True
        self._alerts_ready = True
        sent = fired[:MAX_ALERTS_PER_POLL]
        for i, r in enumerate(sent):
            svc.push(scanner_alert(r, now, len(fired) - len(sent) if i == len(sent) - 1 else 0))

    def _pick_top(self) -> None:
        self._top_at = time.time()
        top = sorted(self.contexts, key=lambda c: -(self.contexts[c].get("day_volume") or 0.0))[:TOP_N]
        if top != self.top:
            self.top = top
            for c in [c for c in self.states if c not in top]:
                del self.states[c]
            if self.on_top_change:
                self.on_top_change()

    def trade_coins(self) -> list[str]:
        """Top coins whose trades the scanner follows itself (the rest already have a dashboard)."""
        return [c for c in self.top if c not in self.rt.pipelines]

    def on_message(self, msg: dict) -> None:
        coin = (msg.get("data") or [{}])[0].get("coin") if isinstance(msg.get("data"), list) else None
        if coin is None or coin not in self.top:
            return
        st = self.states.get(coin)
        if st is None:
            st = self.states[coin] = MarketState(coin)
        st.apply_many(parse_message(msg, coin))

    def _state(self, coin: str):
        pipe = self.rt.pipelines.get(coin)
        return pipe.state if pipe is not None else self.states.get(coin)

    def rank(self, now: int) -> list[Row]:
        rows = []
        for coin, ctx in self.contexts.items():
            followed = coin in self.top or coin in self.rt.pipelines
            reads = trade_reads(self._state(coin), now) if followed else None
            rows.append(score_coin(coin, ctx, self.history.get(coin), self.baselines.get(coin), reads, now))
        rows.sort(key=lambda r: -r.score)
        return rows

    def public(self) -> dict:
        main = set(self.rt.coins)
        return {
            "enabled": self.enabled, "updated_ms": self.updated_ms, "error": self.error, "poll_s": POLL_S,
            "coins": len(self.contexts), "learned": len(self.baselines), "followed": TOP_N,
            "rows": [{**asdict(r), "main": r.coin in main, "open": r.coin in self.rt.pipelines} for r in self.rows],
        }


def scanner_alert(r: Row, now: int, more: int = 0):
    from engine.alerts import Alert
    from signals.base import fmt_px, fmt_usd
    reasons = list(r.reasons)
    title = f"{r.coin}: {reasons[0]}" if reasons else f"{r.coin} is unusual right now"
    lines = []
    if len(reasons) > 1:
        lines.append("Also: " + "; ".join(reasons[1:]) + ".")
    chg = f" ({'+' if r.chg_24h >= 0 else '−'}{abs(r.chg_24h):.2f}% in 24h)" if r.chg_24h is not None else ""
    lines.append(f"Now {fmt_px(r.price) if r.price else '-'}{chg}. Traded {fmt_usd(r.volume_24h)} in the last 24h.")
    lines.append("Open it from More coins on the dashboard." + (f" {more} more coins are unusual right now too."
                                                                  if more > 0 else ""))
    return Alert(f"scan-{r.coin}-{now}", r.coin, "scanner", now, r.lean, title, tuple(lines))

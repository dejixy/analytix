"""
Serves the position planner: gathers live inputs (price, book, funding, max leverage),
keeps a fitted risk model per coin and candle size, and runs the maths off the event loop.

Models come from the best source available:
  1. live mode: the coin's last 5000 candles from Hyperliquid (refit every 15 minutes)
  2. otherwise: candles built from the bars in memory (backfill + live minute bars)
  3. too little of either: a rough fat-tailed walk at the volatility seen in the feed
Simulated paths depend only on the model and the hold, so changing size, leverage or side
re-reads the same paths and answers instantly.
"""
import asyncio
import logging
import time

from config import DEFAULT_SIGMA_1S_BPS
from engine.planner import (
    SHORT_HOLD_H,
    Paths,
    Plan,
    RiskModel,
    candles_from_bars,
    candles_from_rows,
    fit_model,
    make_plan,
    rough_model,
    simulate,
)
from engine.summary import slippage_bps
from ingestion.backfill import fetch_candles, fetch_max_leverage

log = logging.getLogger("analytix.planner")

MODEL_TTL_S = 900
ROUGH_TTL_S = 60
META_TTL_S = 6 * 3600
INTERVAL = {300: "5m", 3600: "1h"}


class PlanError(ValueError):
    pass


class PlannerService:
    def __init__(self, runtime):
        self.rt = runtime
        self._models: dict[tuple[str, int], tuple[float, RiskModel]] = {}
        self._paths: dict[tuple[str, int, float], tuple[RiskModel, Paths]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._max_lev: dict[str, float] = {}
        self._meta_at = 0.0

    # ── inputs ─────────────────────────────────────────────────────────────
    async def max_leverage(self, coin: str) -> float | None:
        if self.rt.mode == "live" and time.time() - self._meta_at > META_TTL_S:
            async with self._lock(("meta",)):
                if time.time() - self._meta_at > META_TTL_S:
                    self._meta_at = time.time()
                    try:
                        self._max_lev = await fetch_max_leverage()
                    except Exception as exc:          # network, proxy, format: plan with an assumed max instead
                        log.warning("max leverage unavailable: %s", exc)
        return self._max_lev.get(coin)

    async def model(self, coin: str, step_s: int) -> RiskModel:
        key = (coin, step_s)
        async with self._lock(key):
            hit = self._models.get(key)
            if hit and time.time() - hit[0] < (MODEL_TTL_S if hit[1].kind == "fhs" else ROUGH_TTL_S):
                return hit[1]
            m = await self._build(coin, step_s)
            self._models[key] = (time.time(), m)
            return m

    async def _build(self, coin: str, step_s: int) -> RiskModel:
        if self.rt.mode == "live":
            try:
                rows = await fetch_candles(coin, INTERVAL[step_s])
                m = await asyncio.to_thread(fit_model, candles_from_rows(rows, step_s, int(time.time() * 1000)))
                if m:
                    log.info("planner model for %s %s: %d candles, α=%.2f β=%.2f, vol %.2f× usual",
                             coin, INTERVAL[step_s], m.n, m.alpha, m.beta, m.vol_ratio)
                    return m
            except Exception as exc:
                log.warning("candles for %s unavailable (%s); using the bars in memory", coin, exc)
        pipe = self.rt.pipeline(coin)
        bars = list(pipe.state.bars)
        for step in dict.fromkeys((step_s, 300)):                 # hourly from memory if enough, else 5-minute
            m = await asyncio.to_thread(fit_model, candles_from_bars(bars, step))
            if m:
                return m
        an = pipe.analyzer
        base = an.bar_baseline or an.baseline
        return rough_model(base.sigma_1s_bps if base else DEFAULT_SIGMA_1S_BPS, 300)

    async def paths(self, coin: str, model: RiskModel, hours: float) -> Paths:
        key = (coin, model.step_s, hours)
        hit = self._paths.get(key)
        if hit and hit[0] is model:
            return hit[1]
        p = await asyncio.to_thread(simulate, model, hours)
        if len(self._paths) > 64:
            self._paths.clear()
        self._paths[key] = (model, p)
        return p

    # ── the plan ───────────────────────────────────────────────────────────
    async def plan(self, coin: str, side: int, margin: float, leverage: float, hours: float) -> Plan:
        pipe = self.rt.pipeline(coin)
        st = pipe.state
        mid = st.mid
        if mid is None:
            raise PlanError("No price yet — the feed is still connecting.")
        max_lev = await self.max_leverage(coin)
        if max_lev is not None and leverage > max_lev:
            raise PlanError(f"{coin} allows up to {max_lev:g}× on Hyperliquid.")
        notional = margin * leverage
        book = st.book
        entry_slip = exit_slip = None
        if book:
            entry_slip = slippage_bps(book.asks if side > 0 else book.bids, notional, mid)
            exit_slip = slippage_bps(book.bids if side > 0 else book.asks, notional, mid)
        funding_now = st.context.funding if st.context else None
        hist = pipe.analyzer.positioning.funding
        funding_avg = sum(hist) / len(hist) if hist else None

        model = await self.model(coin, 300 if hours <= SHORT_HOLD_H else 3600)
        paths = await self.paths(coin, model, hours)
        return await asyncio.to_thread(
            make_plan, coin=coin, side=side, margin=margin, leverage=leverage, hours=hours, entry=mid,
            paths=paths, model=model, max_leverage=max_lev, entry_slip_bps=entry_slip, exit_slip_bps=exit_slip,
            funding_now=funding_now, funding_avg=funding_avg)

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

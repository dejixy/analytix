"""
Serves the position planner: gathers live inputs (price, book, funding, max leverage),
keeps a fitted risk model per coin and candle size, and runs the maths off the event loop.

Models come from the best source available:
  1. live mode: the coin's last 5000 candles from Hyperliquid (refit every 15 minutes)
  2. otherwise: candles built from the bars in memory (backfill + live minute bars: at most a
     week, so usually 5-minute candles even for long holds)
  3. too little of either: a rough fat-tailed walk at the volatility seen in the feed
Between refits, every request brings the volatility up to the moment from the bars in memory
and the live price (RiskModel.nowcast), so a move since the last fit is never ignored.
Paths are cached per model, starting volatility, candle, side and liquidation distance;
changing the margin alone re-reads them instantly.
"""
import asyncio
import logging
import math
import time

from config import DEFAULT_SIGMA_1S_BPS
from engine.planner import (
    DEFAULT_MAX_LEVERAGE,
    SHORT_HOLD_H,
    bracket_distances,
    Paths,
    Plan,
    RiskModel,
    candles_from_bars,
    candles_from_rows,
    fit_model,
    liq_distance,
    maintenance_rate,
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
META_RETRY_S = 60
INTERVAL = {300: "5m", 3600: "1h"}


class PlanError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PlannerService:
    def __init__(self, runtime):
        self.rt = runtime
        self._models: dict[tuple[str, int], tuple[float, RiskModel]] = {}
        self._paths: dict[tuple, Paths] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._max_lev: dict[str, float] = {}
        self._meta_at = 0.0

    # ── inputs ─────────────────────────────────────────────────────────────
    async def max_leverage(self, coin: str) -> float | None:
        if self.rt.mode == "live" and time.time() - self._meta_at > META_TTL_S:
            async with self._lock(("meta",)):
                if time.time() - self._meta_at > META_TTL_S:
                    try:
                        fresh = await fetch_max_leverage()
                        if fresh:
                            self._max_lev = fresh
                        self._meta_at = time.time()
                    except Exception as exc:          # network, proxy, format: keep the last good map, retry soon
                        log.warning("max leverage unavailable: %s", exc)
                        self._meta_at = time.time() - META_TTL_S + META_RETRY_S
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
                season = None
                if step_s < 3600:                                  # borrow the daily cycle from ~200 days of hours
                    hourly = await self.model(coin, 3600)
                    season = hourly.season if hourly.kind == "fhs" else None
                rows = await fetch_candles(coin, INTERVAL[step_s])
                m = await asyncio.to_thread(fit_model, candles_from_rows(rows, step_s, int(time.time() * 1000)),
                                            season)
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
        return rough_model(base.sigma_1s_bps if base else DEFAULT_SIGMA_1S_BPS, 300, pipe.state.now_ms)

    async def paths(self, coin: str, model: RiskModel, hours: float, v0: float, start_ms: int,
                    side: int, dist: float | None, bracket: tuple[float | None, float | None] | None = None) -> Paths:
        """Paths for this model, starting volatility, time of day, side and liquidation distance. Kept until
        any of those move: the volatility by ~1%, the clock by one candle."""
        key = (coin, id(model), hours, round(math.log(v0), 2), start_ms // (model.step_s * 1000), side,
               None if dist is None else round(dist, 5),
               None if bracket is None else tuple(None if x is None else round(x, 6) for x in bracket))
        hit = self._paths.get(key)
        if hit is not None:
            return hit
        liq = (side, math.inf if dist is None else dist) if bracket is not None else (None if dist is None else (side, dist))
        p = await asyncio.to_thread(simulate, model, hours, v0, start_ms, liq=liq, bracket=bracket)
        if len(self._paths) > 64:
            self._paths.clear()
        self._paths[key] = p
        return p

    # ── the plan ───────────────────────────────────────────────────────────
    async def plan(self, coin: str, side: int, margin: float, leverage: float, hours: float,
                   stop_pct: float | None = None, target_pct: float | None = None) -> Plan:
        pipe = self.rt.pipeline(coin)
        if pipe.state.mid is None:
            raise PlanError("No price yet — the feed is still connecting.", 503)
        max_lev = await self.max_leverage(coin)
        if max_lev is not None and leverage > max_lev:
            raise PlanError(f"{coin} allows up to {max_lev:g}× on Hyperliquid.")
        if max_lev is None and leverage > DEFAULT_MAX_LEVERAGE:
            raise PlanError(f"Hyperliquid's max leverage for {coin} hasn't loaded (it needs live mode), so plans "
                            f"stop at {DEFAULT_MAX_LEVERAGE}× with {maintenance_rate(DEFAULT_MAX_LEVERAGE) * 100:.0f}% "
                            f"maintenance margin — the cautious case.")
        model = await self.model(coin, 300 if hours <= SHORT_HOLD_H else 3600)

        # Everything live is read together, after the waits, so entry, book and volatility are of one moment.
        st = pipe.state
        mid, book, now_ms = st.mid, st.book, st.now_ms
        if mid is None:
            raise PlanError("No price yet — the feed is still connecting.", 503)
        recent = [b for b in st.bars if b.timestamp >= model.last_t]
        v0 = model.nowcast(candles_from_bars(recent, model.step_s), mid, now_ms)
        maint = maintenance_rate(max_lev or DEFAULT_MAX_LEVERAGE)
        notional = margin * leverage
        entry_slip = exit_slip = None
        if book:
            entry_slip = slippage_bps(book.asks if side > 0 else book.bids, notional, mid)
            exit_slip = slippage_bps(book.bids if side > 0 else book.asks, notional, mid)
        funding_now = st.context.funding if st.context else None
        hist = pipe.analyzer.positioning.funding
        funding_avg = sum(hist) / len(hist) if hist else None

        bracket = bracket_distances(side, stop_pct, target_pct) if stop_pct or target_pct else None
        paths = await self.paths(coin, model, hours, v0, now_ms, side, liq_distance(mid, side, leverage, maint), bracket)
        return await asyncio.to_thread(
            make_plan, coin=coin, side=side, margin=margin, leverage=leverage, hours=hours, entry=mid,
            paths=paths, model=model, max_leverage=max_lev, entry_slip_bps=entry_slip, exit_slip_bps=exit_slip,
            funding_now=funding_now, funding_avg=funding_avg, vol_ratio=model.vol_ratio(v0, now_ms),
            book_seen=book is not None, stop_pct=stop_pct, target_pct=target_pct)

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

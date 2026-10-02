"""
The engine loop: once per exchange-second, slice the buffers into each
window, run every signal, explain the move, estimate the next period, and
update the event log.

Two tiers:
  • tick windows (≤ 60m) read raw trades and book updates,
  • bar windows (6h … 1w) read one-minute bars (plus backfilled candles).

Driven by the exchange clock (MarketState.now_ms), not a timer — so a replay
at 50× produces exactly the same explanations as the live session did.
"""
import math
from dataclasses import replace

from config import (
    ANALYSIS_INTERVAL_MS,
    DEFAULT_SIGMA_1S_BPS,
    MAX_REFRESH_MS,
    MIN_SIGMA_SAMPLES,
    MIN_SWEEP_NOTIONAL,
    SWEEP_NOTIONAL_PCTL,
    TICK_WINDOW_MAX_S,
    WINDOW_REFRESH_DIVISOR,
    WINDOWS,
)
from engine.eventLog import EventLog
from engine.explainer import explain
from engine.outlook import make_outlook
from engine.outlookTracker import OutlookTracker
from models.bookModel import BookSummary
from models.contextModel import AssetContext
from models.explanationModel import Explanation
from signals import DRIVER_SIGNALS, horizon, price_move
from signals.base import Baseline, FlowAgg, SweepAgg, WindowSlice
from state import MarketState

BASELINE_REFRESH_MS = 10_000        # "normal" drifts slowly; no need to recompute every tick
BAR_BASELINE_REFRESH_MS = 60_000
MIN_BAR_RETURNS = 30


class Analyzer:
    def __init__(self, state: MarketState, windows: dict[str, int] = WINDOWS,
                 interval_ms: int = ANALYSIS_INTERVAL_MS):
        self.state = state
        self.windows = windows
        self.interval_ms = interval_ms
        self.latest: dict[str, Explanation] = {}
        self.events = EventLog()
        self.tracker = OutlookTracker()
        self.baseline: Baseline | None = None
        self.bar_baseline: Baseline | None = None
        self.runs = 0
        self._last_run_ms = 0
        self._baseline_ms = 0
        self._bar_baseline_ms = 0
        self._window_ms: dict[str, int] = {}   # when each window was last recomputed

    def reset(self) -> None:
        self.__init__(self.state, self.windows, self.interval_ms)

    def maybe_run(self) -> bool:
        now = self.state.now_ms
        if not now or self.state.book is None or now - self._last_run_ms < self.interval_ms:
            return False
        self.run(now)
        return True

    def run(self, now_ms: int) -> dict[str, Explanation]:
        st = self.state
        if self.baseline is None or now_ms - self._baseline_ms >= BASELINE_REFRESH_MS:
            self.baseline = self.compute_baseline()
            st.sweep_threshold = self.baseline.sweep_threshold
            self._baseline_ms = now_ms
        if self.bar_baseline is None or now_ms - self._bar_baseline_ms >= BAR_BASELINE_REFRESH_MS:
            self.bar_baseline = self.compute_bar_baseline(self.baseline)
            self._bar_baseline_ms = now_ms

        self.tracker.evaluate(now_ms, st.mid)
        book_now = st.books.newest
        refreshed: dict[str, Explanation] = {}
        for label, seconds in self.windows.items():
            every_ms = min(MAX_REFRESH_MS, max(self.interval_ms, seconds * 1000 // WINDOW_REFRESH_DIVISOR))
            if label in self.latest and now_ms - self._window_ms.get(label, 0) < every_ms:
                continue                                  # a 60m view doesn't change in one second
            if seconds <= TICK_WINDOW_MAX_S:
                baseline = self.baseline
                sl = self.build_slice(label, seconds, now_ms, baseline)
            else:
                baseline = self.bar_baseline
                sl = self.build_bar_slice(label, seconds, now_ms, baseline)
            if sl is None:
                continue
            move = price_move(sl)
            signals = [fn(sl) for fn in DRIVER_SIGNALS]
            ex = explain(st.coin, sl, move, signals)

            outlook = make_outlook(ex, book_now.imbalance if book_now else None, baseline.sigma_1s_bps,
                                   horizon(seconds), self.tracker.stats(label))
            self.tracker.record(label, now_ms, st.mid, outlook)
            refreshed[label] = replace(ex, outlook=outlook)
            self._window_ms[label] = now_ms
        self.latest = {**self.latest, **refreshed}
        self.events.update(refreshed, now_ms)
        self._last_run_ms = now_ms
        self.runs += 1
        return self.latest

    # ── baselines: what does "normal" look like right now? ──────────────────
    def compute_baseline(self) -> Baseline:
        st = self.state
        series = st.mid_series(st.history_seconds)
        returns = []
        for (t0, p0), (t1, p1) in zip(series, series[1:]):
            dt = (t1 - t0) / 1000
            if p0 > 0 and p1 > 0 and 0 < dt <= 10:
                returns.append(math.log(p1 / p0) * 10_000 / math.sqrt(dt))
        sigma = _std(returns) if len(returns) >= MIN_SIGMA_SAMPLES else DEFAULT_SIGMA_1S_BPS

        trades = list(st.trades)
        covered = max(1.0, st.trades.covered_seconds())
        notional_per_s = sum(t.notional for t in trades) / covered if trades else 0.0

        sizes = sorted(o.notional for o in st.orders)
        pctl = sizes[min(len(sizes) - 1, int(len(sizes) * SWEEP_NOTIONAL_PCTL))] if sizes else 0.0
        return Baseline(
            sigma_1s_bps=max(sigma, 0.05),
            notional_per_s=notional_per_s,
            sweep_threshold=max(MIN_SWEEP_NOTIONAL, pctl),
            covered_s=covered,
        )

    def compute_bar_baseline(self, tick: Baseline) -> Baseline:
        """Volatility from bar-to-bar returns: the right yardstick for moves measured in hours."""
        bars = list(self.state.bars)
        returns = []
        for a, b in zip(bars, bars[1:]):
            dt = (b.end_ms - a.end_ms) / 1000
            if a.close > 0 and b.close > 0 and 0 < dt <= 2 * b.span_s:
                returns.append(math.log(b.close / a.close) * 10_000 / math.sqrt(dt))
        sigma = _std(returns) if len(returns) >= MIN_BAR_RETURNS else tick.sigma_1s_bps
        flow_bars = [b for b in bars if b.has_flow]
        flow_s = sum(b.span_s for b in flow_bars)
        per_s = sum(b.buy_notional + b.sell_notional for b in flow_bars) / flow_s if flow_s else tick.notional_per_s
        return Baseline(sigma_1s_bps=max(sigma, 0.05), notional_per_s=per_s,
                        sweep_threshold=tick.sweep_threshold, covered_s=float(sum(b.span_s for b in bars)))

    # ── slicing: tick windows ────────────────────────────────────────────────
    def build_slice(self, label: str, seconds: int, now_ms: int, baseline: Baseline) -> WindowSlice | None:
        st = self.state
        start_ms = now_ms - seconds * 1000
        book_end = st.books.latest_at(now_ms)
        if book_end is None:
            return None
        book_start = st.books.latest_at(start_ms) or st.books.oldest
        books = st.books.window(seconds, now_ms)
        mids = [b.mid for b in books] + ([book_start.mid] if book_start else [])

        path = [(b.timestamp, b.mid) for b in ([book_start] if book_start else []) + books]
        bursts = _bursts(path, 60_000) if seconds > 60 else (0.0, 0.0, 0, 0)

        oldest = min(x.timestamp for x in (st.books.oldest, st.trades.oldest) if x is not None)
        coverage = min(1.0, max(0.0, (now_ms - max(start_ms, oldest)) / (seconds * 1000)))

        return WindowSlice(
            label=label,
            seconds=seconds,
            start_ms=start_ms,
            end_ms=now_ms,
            trades=st.trades.window(seconds, now_ms),
            orders=st.orders.window(seconds, now_ms),
            book_start=book_start,
            book_end=book_end,
            ctx_start=st.contexts.latest_at(start_ms) or st.contexts.oldest,
            ctx_end=st.contexts.latest_at(now_ms),
            high=max(mids) if mids else None,
            low=min(mids) if mids else None,
            coverage=coverage,
            baseline=baseline,
            burst_up_bps=bursts[0],
            burst_down_bps=bursts[1],
            burst_up_end_ms=bursts[2],
            burst_down_end_ms=bursts[3],
        )

    # ── slicing: bar windows ─────────────────────────────────────────────────
    def build_bar_slice(self, label: str, seconds: int, now_ms: int, baseline: Baseline) -> WindowSlice | None:
        st = self.state
        start_ms = now_ms - seconds * 1000
        bars = st.bars.window(seconds, now_ms)
        before = st.bars.latest_at(start_ms)
        if not bars and before is None:
            return None
        end_price = st.mid if st.mid is not None else bars[-1].close
        start_price = before.close if before else bars[0].open
        t_start = min(before.end_ms, start_ms) if before else bars[0].timestamp
        scope = ([before] if before else []) + bars

        path = [(t_start, start_price)] + [(b.end_ms, b.close) for b in bars] + [(now_ms, end_price)]
        span_s = max(60, seconds // 12)
        bursts = _bursts(path, span_s * 1000)

        # Depth, OI and flow exist only on live bars; use the earliest one inside the window.
        depth = [b for b in scope if b.bid_notional is not None]
        live_book = st.books.newest
        bid_end = live_book.bid_notional if live_book else (depth[-1].bid_notional if depth else 0.0)
        ask_end = live_book.ask_notional if live_book else (depth[-1].ask_notional if depth else 0.0)
        book_start = BookSummary(t_start, start_price, start_price, start_price, 0.0,
                                 depth[0].bid_notional if depth else 0.0, depth[0].ask_notional if depth else 0.0)
        book_end = BookSummary(now_ms, end_price, end_price, end_price, 0.0, bid_end or 0.0, ask_end or 0.0)

        oi = [b for b in scope if b.open_interest]
        fund = [b for b in scope if b.funding is not None]
        ctx_end = st.context
        ctx_start = None
        if oi or fund:
            ctx_start = AssetContext(
                timestamp=t_start,
                funding=fund[0].funding if fund else (ctx_end.funding if ctx_end else 0.0),
                open_interest=oi[0].open_interest if oi else 0.0,
                mark_price=start_price, oracle_price=start_price,
            )
        if ctx_end is None and scope:
            last = scope[-1]
            ctx_end = AssetContext(now_ms, last.funding or 0.0, last.open_interest or 0.0, end_price, end_price)

        flow_bars = [b for b in bars if b.has_flow]
        flow_s = float(sum(b.span_s for b in flow_bars))
        flow_cov = min(1.0, flow_s / seconds)
        flow = FlowAgg(buy=sum(b.buy_notional for b in flow_bars), sell=sum(b.sell_notional for b in flow_bars),
                       fills=sum(b.fills for b in flow_bars), seconds=flow_s, coverage=flow_cov)
        sweeps = SweepAgg(
            buy_n=sum(b.sweeps_buy for b in flow_bars), sell_n=sum(b.sweeps_sell for b in flow_bars),
            buy_notional=sum(b.sweep_notional_buy for b in flow_bars),
            sell_notional=sum(b.sweep_notional_sell for b in flow_bars),
            total_notional=flow.buy + flow.sell,
        )

        oldest = st.bars.oldest.timestamp if st.bars.oldest else now_ms
        coverage = min(1.0, max(0.0, (now_ms - max(start_ms, oldest)) / (seconds * 1000)))
        highs = [b.high for b in bars] + [start_price, end_price]
        lows = [b.low for b in bars] + [start_price, end_price]

        return WindowSlice(
            label=label, seconds=seconds, start_ms=start_ms, end_ms=now_ms,
            trades=[], orders=[],
            book_start=book_start, book_end=book_end, ctx_start=ctx_start, ctx_end=ctx_end,
            high=max(highs), low=min(lows), coverage=coverage, baseline=baseline,
            burst_up_bps=bursts[0], burst_down_bps=bursts[1],
            burst_up_end_ms=bursts[2], burst_down_end_ms=bursts[3], burst_span_s=span_s,
            resolution="bar", flow=flow, sweep_agg=sweeps, flow_coverage=flow_cov,
        )


def _std(xs: list[float]) -> float:
    mean = sum(xs) / len(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


def _bursts(path: list[tuple[int, float]], span_ms: int) -> tuple[float, float, int, int]:
    """
    Largest rise and largest fall over any `span_ms` stretch of a (time, price) path.
    Two pointers: for each point, compare with the price one span earlier.
    """
    best_up = best_down = 0.0
    up_at = down_at = 0
    j = 0
    for i, (t, p) in enumerate(path):
        while j < i and path[j + 1][0] <= t - span_ms:
            j += 1
        ref_t, ref_p = path[j]
        if ref_p <= 0 or j == i:
            continue
        d = (p / ref_p - 1) * 10_000
        if d > best_up:
            best_up, up_at = d, t
        if d < best_down:
            best_down, down_at = d, t
    return best_up, best_down, up_at, down_at

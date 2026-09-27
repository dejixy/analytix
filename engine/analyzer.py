"""
The engine loop: once per exchange-second, slice the buffers into each
window, run every signal, explain the move, and update the event log.

Driven by the exchange clock (MarketState.now_ms), not a timer — so a replay
at 50× produces exactly the same explanations as the live session did.
"""
import math

from config import (
    ANALYSIS_INTERVAL_MS,
    WINDOW_REFRESH_DIVISOR,
    DEFAULT_SIGMA_1S_BPS,
    MIN_SIGMA_SAMPLES,
    MIN_SWEEP_NOTIONAL,
    SWEEP_NOTIONAL_PCTL,
    WINDOWS,
)
from engine.eventLog import EventLog
from engine.explainer import explain
from models.explanationModel import Explanation
from signals import DRIVER_SIGNALS, price_move
from signals.base import Baseline, WindowSlice
from state import MarketState

BASELINE_REFRESH_MS = 10_000   # "normal" drifts slowly; no need to recompute every tick


class Analyzer:
    def __init__(self, state: MarketState, windows: dict[str, int] = WINDOWS,
                 interval_ms: int = ANALYSIS_INTERVAL_MS):
        self.state = state
        self.windows = windows
        self.interval_ms = interval_ms
        self.latest: dict[str, Explanation] = {}
        self.events = EventLog()
        self.baseline: Baseline | None = None
        self.runs = 0
        self._last_run_ms = 0
        self._baseline_ms = 0
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
        if self.baseline is None or now_ms - self._baseline_ms >= BASELINE_REFRESH_MS:
            self.baseline = self.compute_baseline()
            self._baseline_ms = now_ms
        refreshed: dict[str, Explanation] = {}
        for label, seconds in self.windows.items():
            every_ms = max(self.interval_ms, seconds * 1000 // WINDOW_REFRESH_DIVISOR)
            if label in self.latest and now_ms - self._window_ms.get(label, 0) < every_ms:
                continue                                  # a 60m view doesn't change in one second
            sl = self.build_slice(label, seconds, now_ms, self.baseline)
            if sl is None:
                continue
            move = price_move(sl)
            signals = [fn(sl) for fn in DRIVER_SIGNALS]
            refreshed[label] = explain(self.state.coin, sl, move, signals)
            self._window_ms[label] = now_ms
        self.latest = {**self.latest, **refreshed}
        self.events.update(refreshed, now_ms)
        self._last_run_ms = now_ms
        self.runs += 1
        return self.latest

    # ── baseline: what does "normal" look like right now? ───────────────────
    def compute_baseline(self) -> Baseline:
        st = self.state
        series = st.mid_series(st.history_seconds)
        returns = []
        for (t0, p0), (t1, p1) in zip(series, series[1:]):
            dt = (t1 - t0) / 1000
            if p0 > 0 and p1 > 0 and 0 < dt <= 10:
                returns.append(math.log(p1 / p0) * 10_000 / math.sqrt(dt))
        if len(returns) >= MIN_SIGMA_SAMPLES:
            mean = sum(returns) / len(returns)
            sigma = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
            sigma = max(sigma, 0.05)
        else:
            sigma = DEFAULT_SIGMA_1S_BPS

        trades = list(st.trades)
        covered = max(1.0, st.trades.covered_seconds())
        notional_per_s = sum(t.notional for t in trades) / covered if trades else 0.0

        sizes = sorted(o.notional for o in st.orders)
        pctl = sizes[min(len(sizes) - 1, int(len(sizes) * SWEEP_NOTIONAL_PCTL))] if sizes else 0.0
        return Baseline(
            sigma_1s_bps=sigma,
            notional_per_s=notional_per_s,
            sweep_threshold=max(MIN_SWEEP_NOTIONAL, pctl),
            covered_s=covered,
        )

    # ── slicing ──────────────────────────────────────────────────────────────
    def build_slice(self, label: str, seconds: int, now_ms: int, baseline: Baseline) -> WindowSlice | None:
        st = self.state
        start_ms = now_ms - seconds * 1000
        book_end = st.books.latest_at(now_ms)
        if book_end is None:
            return None
        book_start = st.books.latest_at(start_ms) or st.books.oldest
        books = st.books.window(seconds, now_ms)
        mids = [b.mid for b in books] + ([book_start.mid] if book_start else [])

        bursts = _bursts(([book_start] if book_start else []) + books) if seconds > 60 else (0.0, 0.0, 0, 0)

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


def _bursts(path, span_ms: int = 60_000) -> tuple[float, float, int, int]:
    """
    Largest rise and largest fall over any 60s stretch of the mid path.
    Two pointers: for each point, compare with the price one minute earlier.
    """
    best_up = best_down = 0.0
    up_at = down_at = 0
    j = 0
    for i, cur in enumerate(path):
        while j < i and path[j + 1].timestamp <= cur.timestamp - span_ms:
            j += 1
        ref = path[j]
        if ref.mid <= 0 or ref is cur:
            continue
        d = (cur.mid / ref.mid - 1) * 10_000
        if d > best_up:
            best_up, up_at = d, cur.timestamp
        if d < best_down:
            best_down, down_at = d, cur.timestamp
    return best_up, best_down, up_at, down_at

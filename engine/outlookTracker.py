"""
Scores the outlook against what actually happened.

Each window's lean is sampled several times per horizon. When the horizon has
passed, the realised move is compared with the lean: right direction? inside
the stated range?

Overlap matters. A 10m lean sampled every minute means neighbouring samples
share 9 of their 10 minutes of price action, so a single move grades about ten
samples at once. Sampling densely keeps the hit rate smooth, but the count shown
next to it is the number of independent horizons the samples cover
(`periods`), and the rate stays hidden until there are MIN_PERIODS of them.
"""
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from models.explanationModel import Outlook, TrackRecord
from models.signalModel import Direction

MIN_PERIODS = 30      # independent horizons before a hit rate is shown (±18% at 95% even then)
MAX_SCORED = 3000     # samples kept per window: ~5h of 1m leans, ~50h of 10m leans
TIE_BPS = 1e-6        # a move smaller than this is "no change": neither direction was right


@dataclass(slots=True)
class _Pending:
    start_ms: int
    due_ms: int
    price: float
    lean: Direction
    expected_bps: float
    range_bps: float


@dataclass(frozen=True, slots=True)
class _Scored:
    start_ms: int
    horizon_ms: int
    hit: bool | None      # None: the lean was neutral, or price didn't move
    in_range: bool


def independent_periods(starts: Iterable[int], horizon_ms: int) -> float:
    """How many horizon-lengths of distinct price action a run of samples covers.

    Each sample adds the part of its horizon the previous one didn't already
    cover: evenly spaced at horizon/10 each adds 0.1; after a gap (the app was
    off) a sample adds a full 1. Equivalent to the union of their spans ÷ horizon.
    """
    total, prev = 0.0, None
    for s in starts:
        total += 1.0 if prev is None else min(1.0, (s - prev) / horizon_ms)
        prev = s
    return total


class OutlookTracker:
    def __init__(self, maxlen: int = MAX_SCORED):
        self._maxlen = maxlen
        self._pending: dict[str, deque[_Pending]] = {}
        self._results: dict[str, deque[_Scored]] = {}
        self._last: dict[str, int] = {}
        self._cache: dict[str, TrackRecord] = {}   # stats only change when a sample is scored

    def record(self, window: str, now_ms: int, price: float | None, o: Outlook) -> None:
        if price is None or price <= 0:
            return
        every = max(5_000, o.horizon_s * 1000 // 10)
        if now_ms - self._last.get(window, -every) < every:
            return
        self._last[window] = now_ms
        self._pending.setdefault(window, deque()).append(
            _Pending(now_ms, now_ms + o.horizon_s * 1000, price, o.lean, o.expected_bps, o.range_bps))

    def evaluate(self, now_ms: int, price: float | None) -> None:
        if price is None or price <= 0:
            return
        for window, q in self._pending.items():
            res = self._results.setdefault(window, deque(maxlen=self._maxlen))
            while q and q[0].due_ms <= now_ms:
                p = q.popleft()
                realized = (price / p.price - 1) * 10_000
                if p.lean is Direction.NEUTRAL or abs(realized) < TIE_BPS:
                    hit = None
                else:
                    hit = (realized > 0) == (p.lean is Direction.UP)
                in_range = abs(realized - p.expected_bps) <= p.range_bps
                res.append(_Scored(p.start_ms, p.due_ms - p.start_ms, hit, in_range))
                self._cache.pop(window, None)

    def stats(self, window: str) -> TrackRecord:
        if window in self._cache:
            return self._cache[window]
        res = self._results.get(window, ())
        if not res:
            return TrackRecord()
        horizon_ms = res[0].horizon_ms
        called = [r for r in res if r.hit is not None]
        periods = independent_periods((r.start_ms for r in called), horizon_ms)
        all_periods = independent_periods((r.start_ms for r in res), horizon_ms)
        track = TrackRecord(
            hit_rate=sum(r.hit for r in called) / len(called) if periods >= MIN_PERIODS else None,
            called=len(called),
            periods=periods,
            range_rate=sum(r.in_range for r in res) / len(res) if all_periods >= MIN_PERIODS else None,
        )
        self._cache[window] = track
        return track

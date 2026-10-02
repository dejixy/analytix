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

Restarts. With a store attached (live mode), every sample is saved as it is
taken and as it is scored, so a 24h or 1w track record survives the app being
closed. A sample that came due while the app was off is graded from the bar
history (saved live bars + backfilled candles) — but only when a bar price
lands close enough to the due moment: within 1/30 of the horizon. That works
for 60m and longer; a 1m or 10m sample that came due unobserved is dropped,
never graded with a price from the wrong moment.
"""
import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from models.explanationModel import Outlook, TrackRecord
from models.signalModel import Direction

log = logging.getLogger("analytix.tracker")

MIN_PERIODS = 30      # independent horizons before a hit rate is shown (±18% at 95% even then)
MAX_SCORED = 3000     # samples kept per window: ~5h of 1m leans, ~50h of 10m leans, ~200 days of 1w leans
TIE_BPS = 1e-6        # a move smaller than this is "no change": neither direction was right

ON_TIME_MIN_MS = 5_000          # the engine runs every second; 5s of slack covers a hiccup in the feed
BAR_PRICE_ERROR_MS = 30_000     # a 1m bar's open/close is at best 30s from an arbitrary moment
HISTORY_GRACE_MS = 300_000      # after a restart, wait this long for the backfill before giving up on a sample
TRIM_EVERY = 500                # settled samples between trims of the saved history

PriceAt = Callable[[int], "tuple[float, int] | None"]   # moment → (price, when that price was), or None


@dataclass(slots=True)
class Pending:
    start_ms: int
    due_ms: int
    price: float
    lean: Direction
    expected_bps: float
    range_bps: float


@dataclass(frozen=True, slots=True)
class Scored:
    start_ms: int
    horizon_ms: int
    hit: bool | None      # None: the lean was neutral, or price didn't move
    in_range: bool


class TrackStore(Protocol):
    def add_pending(self, coin: str, window: str, p: Pending) -> None: ...
    def settle(self, coin: str, window: str, start_ms: int, scored: Scored | None) -> None: ...
    def load(self, coin: str, keep: int) -> tuple[dict[str, list[Pending]], dict[str, list[Scored]]]: ...
    def trim(self, coin: str, window: str, keep: int) -> None: ...


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


def grading_tolerance_ms(horizon_ms: int) -> int:
    """How far from the due moment the closing price may be taken: 1/30 of the horizon (≥ 5s)."""
    return max(ON_TIME_MIN_MS, horizon_ms // 30)


_WAIT = object()


class OutlookTracker:
    def __init__(self, maxlen: int = MAX_SCORED):
        self._maxlen = maxlen
        self._pending: dict[str, deque[Pending]] = {}
        self._results: dict[str, deque[Scored]] = {}
        self._last: dict[str, int] = {}
        self._cache: dict[str, TrackRecord] = {}   # stats only change when a sample is scored
        self._store: TrackStore | None = None
        self._coin = ""
        self._first_ms: int | None = None
        self._settled = 0

    # ── persistence ─────────────────────────────────────────────────────────
    def attach(self, store: TrackStore, coin: str) -> int:
        """Load this coin's saved track record and save everything from now on. Returns samples loaded."""
        pending, scored = store.load(coin, self._maxlen)
        for window, items in pending.items():
            q = self._pending.setdefault(window, deque())
            q.extend(sorted(items, key=lambda p: p.start_ms))
            self._last[window] = max(self._last.get(window, 0), q[-1].start_ms)
        for window, items in scored.items():
            res = self._results.setdefault(window, deque(maxlen=self._maxlen))
            res.extend(sorted(items, key=lambda s: s.start_ms))
            self._cache.pop(window, None)
        for window in pending.keys() | scored.keys():
            store.trim(coin, window, self._maxlen)
        self._store, self._coin = store, coin
        return sum(map(len, pending.values())) + sum(map(len, scored.values()))

    def _save(self, fn: str, *args) -> None:
        if self._store is None:
            return
        try:
            getattr(self._store, fn)(self._coin, *args)
        except Exception:      # the track record is nice to keep, never worth stopping the engine for
            log.exception("could not save the outlook track record")

    # ── sampling and scoring ────────────────────────────────────────────────
    def record(self, window: str, now_ms: int, price: float | None, o: Outlook) -> None:
        if price is None or price <= 0:
            return
        every = max(5_000, o.horizon_s * 1000 // 10)
        if now_ms - self._last.get(window, -every) < every:
            return
        self._last[window] = now_ms
        p = Pending(now_ms, now_ms + o.horizon_s * 1000, price, o.lean, o.expected_bps, o.range_bps)
        self._pending.setdefault(window, deque()).append(p)
        self._save("add_pending", window, p)

    def evaluate(self, now_ms: int, price: float | None, price_at: PriceAt | None = None) -> None:
        """Score every sample whose horizon has passed.

        On time (the engine saw the due moment): graded with the current price.
        Late (the app was off): graded from `price_at` — the bar history — if it
        has a price close enough to the due moment; otherwise dropped, after a
        short grace period in case the history backfill hasn't arrived yet.
        """
        if price is None or price <= 0:
            return
        if self._first_ms is None:
            self._first_ms = now_ms
        settling = now_ms - self._first_ms < HISTORY_GRACE_MS
        for window, q in self._pending.items():
            res = self._results.setdefault(window, deque(maxlen=self._maxlen))
            held: list[Pending] = []
            while q and q[0].due_ms <= now_ms:
                p = q.popleft()
                end = self._end_price(p, now_ms, price, price_at, settling)
                if end is _WAIT:
                    held.append(p)
                    continue
                scored = None if end is None else self._score(p, end)
                if scored is not None:
                    res.append(scored)
                    self._cache.pop(window, None)
                self._save("settle", window, p.start_ms, scored)
                self._settled += 1
                if self._settled % TRIM_EVERY == 0:
                    self._save("trim", window, self._maxlen)
            q.extendleft(reversed(held))

    @staticmethod
    def _end_price(p: Pending, now_ms: int, price: float, price_at: PriceAt | None, settling: bool):
        tol = grading_tolerance_ms(p.due_ms - p.start_ms)
        if now_ms - p.due_ms <= tol:
            return price
        if tol < BAR_PRICE_ERROR_MS:          # no bar can price a 1m/10m horizon finely enough
            return None
        near = price_at(p.due_ms) if price_at else None
        if near and abs(near[1] - p.due_ms) <= tol and near[0] > 0:
            return near[0]
        return _WAIT if settling else None

    @staticmethod
    def _score(p: Pending, end_price: float) -> Scored:
        realized = (end_price / p.price - 1) * 10_000
        if p.lean is Direction.NEUTRAL or abs(realized) < TIE_BPS:
            hit = None
        else:
            hit = (realized > 0) == (p.lean is Direction.UP)
        in_range = abs(realized - p.expected_bps) <= p.range_bps
        return Scored(p.start_ms, p.due_ms - p.start_ms, hit, in_range)

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

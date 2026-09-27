"""
Scores the outlook against what actually happened.

Each window's lean is sampled a few times per horizon (not every second — the
samples would all be the same bet). When the horizon has passed, the realised
move is compared with the lean: right direction? inside the stated range?
"""
from collections import deque
from dataclasses import dataclass

from models.explanationModel import Outlook
from models.signalModel import Direction

MIN_SCORED = 10       # don't show a hit rate until this many leans have been scored


@dataclass(slots=True)
class _Pending:
    due_ms: int
    price: float
    lean: Direction
    expected_bps: float
    range_bps: float


class OutlookTracker:
    def __init__(self, maxlen: int = 500):
        self._maxlen = maxlen
        self._pending: dict[str, deque[_Pending]] = {}
        self._results: dict[str, deque[tuple[bool | None, bool]]] = {}
        self._last: dict[str, int] = {}

    def record(self, window: str, now_ms: int, price: float | None, o: Outlook) -> None:
        if price is None or price <= 0:
            return
        every = max(5_000, o.horizon_s * 1000 // 10)
        if now_ms - self._last.get(window, -every) < every:
            return
        self._last[window] = now_ms
        self._pending.setdefault(window, deque()).append(
            _Pending(now_ms + o.horizon_s * 1000, price, o.lean, o.expected_bps, o.range_bps))

    def evaluate(self, now_ms: int, price: float | None) -> None:
        if price is None or price <= 0:
            return
        for window, q in self._pending.items():
            res = self._results.setdefault(window, deque(maxlen=self._maxlen))
            while q and q[0].due_ms <= now_ms:
                p = q.popleft()
                realized = (price / p.price - 1) * 10_000
                hit = None if p.lean is Direction.NEUTRAL else (realized > 0) == (p.lean is Direction.UP)
                res.append((hit, abs(realized - p.expected_bps) <= p.range_bps))

    def stats(self, window: str) -> tuple[float | None, int]:
        """(hit rate of non-neutral leans, how many were scored)."""
        res = self._results.get(window, ())
        called = [h for h, _ in res if h is not None]
        if len(called) < MIN_SCORED:
            return None, len(called)
        return sum(called) / len(called), len(called)

    def range_rate(self, window: str) -> float | None:
        res = self._results.get(window, ())
        return sum(r for _, r in res) / len(res) if len(res) >= MIN_SCORED else None

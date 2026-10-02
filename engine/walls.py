"""
Walls: big resting orders near the price, and whether they're real.

"Asks +94%" says offers stacked up. It doesn't say whether anyone means it.
Large orders that vanish as price approaches are bait — they exist to steer
other traders, not to trade. Large orders that stand and get hit are real
supply or demand. So every wall in the visible book is followed until it goes,
and its exit is classified:

    eaten        traded into: at least half of what disappeared was filled
    pulled near  vanished while price was within 10 bps of it, mostly unfilled
    pulled far   vanished while price was still far away (repositioning — not counted as bait)

A wall is a level holding at least 4× the median level size of the visible
book and at least two sweeps' worth of USD. Walls that live under 10 seconds
are ignored (that's ordinary quote churn). A standing wall that has absorbed at
least 20% of its size is counted as "held". Counts cover the last 30 minutes.
"""
from collections import deque
from dataclasses import dataclass

from models.bookModel import OrderBook
from models.tradeModel import Trade, TradeSide
from signals.base import WallStats

WALL_MULT = 4.0
MIN_WALL_SWEEPS = 2.0
MIN_LIFE_MS = 10_000
APPROACH_BPS = 10.0
GONE_FRACTION = 0.3        # a wall shrunk below 30% of its peak is gone
EATEN_SHARE = 0.5
TESTED_SHARE = 0.2
STATS_WINDOW_MS = 30 * 60_000


@dataclass(slots=True)
class Wall:
    side: str              # "bid" | "ask"
    price: float
    first_ms: int
    last_ms: int
    peak: float            # USD
    notional: float        # USD now
    first_dist_bps: float
    dist_bps: float
    traded: float = 0.0    # USD of aggressive flow filled at this price since it appeared


@dataclass(frozen=True, slots=True)
class WallExit:
    side: str
    price: float
    peak: float
    outcome: str           # "eaten" | "pulled_near" | "pulled_far"
    ts: int
    life_ms: int


def _dist_bps(side: str, price: float, mid: float) -> float:
    return ((mid - price) if side == "bid" else (price - mid)) / mid * 10_000


class WallTracker:
    def __init__(self):
        self._walls: dict[tuple[str, float], Wall] = {}
        self.exits: deque[WallExit] = deque(maxlen=500)
        self._last_ms = 0

    def update(self, book: OrderBook | None, trades: list[Trade], now_ms: int, sweep_threshold: float) -> None:
        if book is None or book.mid is None:
            return
        mid = book.mid
        for t in trades:                                  # fills at a wall's price count against it
            if t.timestamp <= self._last_ms:
                continue
            side = "bid" if t.side is TradeSide.SELL else "ask"
            w = self._walls.get((side, t.price))
            if w:
                w.traded += t.notional
        self._last_ms = max(self._last_ms, now_ms)

        levels = {("bid", l.price): l.notional for l in book.bids} | {("ask", l.price): l.notional for l in book.asks}
        sizes = sorted(levels.values())
        median = sizes[len(sizes) // 2] if sizes else 0.0
        threshold = max(WALL_MULT * median, MIN_WALL_SWEEPS * sweep_threshold)

        for key, notional in levels.items():
            if notional < threshold:
                continue
            w = self._walls.get(key)
            if w is None:
                d = _dist_bps(key[0], key[1], mid)
                self._walls[key] = Wall(key[0], key[1], now_ms, now_ms, notional, notional, d, d)

        for key, w in list(self._walls.items()):
            now_size = levels.get(key, 0.0)
            if now_size >= GONE_FRACTION * w.peak:
                w.notional, w.last_ms = now_size, now_ms
                w.peak = max(w.peak, now_size)
                w.dist_bps = _dist_bps(w.side, w.price, mid)
                continue
            del self._walls[key]
            life = w.last_ms - w.first_ms
            if life < MIN_LIFE_MS:
                continue
            vanished = max(1.0, w.peak - now_size)
            if w.traded >= EATEN_SHARE * vanished:
                outcome = "eaten"
            elif w.dist_bps <= APPROACH_BPS:
                outcome = "pulled_near"
            else:
                outcome = "pulled_far"
            self.exits.append(WallExit(w.side, w.price, w.peak, outcome, now_ms, life))

    def active(self, now_ms: int) -> list[Wall]:
        return [w for w in self._walls.values() if now_ms - w.first_ms >= MIN_LIFE_MS]

    def stats(self, now_ms: int) -> WallStats:
        recent = [e for e in self.exits if now_ms - e.ts <= STATS_WINDOW_MS]
        count = lambda side, outcome: sum(1 for e in recent if e.side == side and e.outcome == outcome)  # noqa: E731
        held = lambda side: sum(1 for w in self.active(now_ms) if w.side == side and w.traded >= TESTED_SHARE * w.peak)  # noqa: E731
        return WallStats(
            pulled_near={s: count(s, "pulled_near") for s in ("bid", "ask")},
            eaten={s: count(s, "eaten") for s in ("bid", "ask")},
            held={s: held(s) for s in ("bid", "ask")},
        )

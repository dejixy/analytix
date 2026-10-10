"""
Absorption levels: the price a passive buyer or seller defended, and when it breaks.

"Sellers pressed but bids absorbed it" is only half useful. The tradeable part
is *where*: the price the bids held, and how much they soaked up there. That
gives a level to lean on (a stop just beyond it) and a trigger (it breaking).

Finding the level. Bin the aggressors' fills by price (2 bps bins), take each
bin with its neighbours as a cluster, and walk the clusters from heaviest to
lightest. The first one where price never traded meaningfully beyond it after
the absorption began is the defended level, provided it took a real share
(≥ 20%) of the aggressors' volume.

Tracking it. Levels from the tick windows (1m, 10m, 60m) that absorbed at least
five sweeps' worth of flow are kept, merged when they sit within a few bps of
each other. A level is
broken when price closes beyond it by half a normal one-minute move and stays
there for 10 seconds: a wick through doesn't count. Only a level that held for
three minutes or more gets a "broke" event; untouched levels expire after four
hours.
"""
import math
from collections import deque
from dataclasses import dataclass

from models.explanationModel import DefendedLevel, MarketEvent
from models.signalModel import Direction
from models.tradeModel import Trade, TradeSide
from signals.base import fmt_px, fmt_usd

BIN_BPS = 2.0
MIN_SHARE = 0.20            # the cluster must have absorbed ≥ 20% of the aggressors' volume
HOLD_TOL_BPS = 4.0          # trading this far beyond the level after it formed = it didn't hold
TRACK_WINDOWS = ("1m", "10m", "60m")
MIN_ABSORBED_SWEEPS = 5     # a tracked level must have absorbed ≥ 5 × the sweep threshold
MERGE_BPS = 5.0
BREAK_MIN_BPS = 3.0
BREAK_CONFIRM_MS = 10_000
LEVEL_TTL_MS = 4 * 3600_000
MIN_HELD_MS = 3 * 60_000    # a level that gives way within 3 minutes was never much of a level: no event
GROUP_MS = 30_000           # levels on one side that break within 30s of each other are one event
BROKEN_COOLDOWN_MS = 30 * 60_000   # a broken level isn't re-tracked for 30 minutes
MAX_LEVELS = 8


def defended_level(trades: list[Trade], aggressor: TradeSide) -> DefendedLevel | None:
    hits = [t for t in trades if t.side is aggressor and t.price > 0]
    if not hits:
        return None
    total = sum(t.notional for t in hits)
    w = hits[0].price * BIN_BPS / 10_000
    bins: dict[int, list[Trade]] = {}
    for t in hits:
        bins.setdefault(math.floor(t.price / w), []).append(t)

    def cluster(k: int) -> list[Trade]:
        return [t for j in (k - 1, k, k + 1) for t in bins.get(j, ())]

    sized = sorted(((sum(t.notional for t in cluster(k)), k) for k in bins), reverse=True)
    for notional, k in sized:
        if notional < MIN_SHARE * total:
            break
        fills = cluster(k)
        price = sum(t.price * t.notional for t in fills) / notional
        first = min(t.timestamp for t in fills)
        after = [t.price for t in trades if t.timestamp >= first]
        tol = price * HOLD_TOL_BPS / 10_000
        held = min(after) >= price - tol if aggressor is TradeSide.SELL else max(after) <= price + tol
        if held:
            return DefendedLevel(side="bid" if aggressor is TradeSide.SELL else "ask",
                                 price=price, absorbed=notional, share=notional / total)
    return None


def level_sentence(lv: DefendedLevel) -> str:
    if lv.side == "bid":
        return (f"Buyers defended {fmt_px(lv.price)}: {fmt_usd(lv.absorbed)} of market selling ({lv.share:.0%} "
                f"of all selling) hit that price, and it never went lower.")
    return (f"Sellers defended {fmt_px(lv.price)}: {fmt_usd(lv.absorbed)} of market buying ({lv.share:.0%} "
            f"of all buying) hit that price, and it never went higher.")


@dataclass(slots=True)
class TrackedLevel:
    id: int
    side: str
    price: float
    absorbed: float
    window: str
    created_ms: int
    updated_ms: int
    beyond_since: int | None = None


class LevelTracker:
    def __init__(self, coin: str):
        self.coin = coin
        self._levels: list[TrackedLevel] = []
        self.events: deque[MarketEvent] = deque(maxlen=50)
        self._next = 1
        self._group: dict[str, tuple[int, str, list[TrackedLevel]]] = {}   # side → (first break ms, event id, levels)
        self._broken: list[tuple[str, float, int]] = []                   # (side, price, broke at ms)

    def observe(self, window: str, lv: DefendedLevel, now_ms: int, sweep_threshold: float,
                sigma_1s_bps: float = 0.0) -> None:
        if window not in TRACK_WINDOWS or lv.absorbed < MIN_ABSORBED_SWEEPS * sweep_threshold:
            return
        near = max(MERGE_BPS, _margin_bps(sigma_1s_bps))
        self._broken = [b for b in self._broken if now_ms - b[2] < BROKEN_COOLDOWN_MS]
        if any(side == lv.side and abs(lv.price / price - 1) * 10_000 <= near for side, price, _ in self._broken):
            return                                   # it just gave way: don't draw it as holding again
        for t in self._levels:
            if t.side == lv.side and abs(lv.price / t.price - 1) * 10_000 <= near:
                t.absorbed = max(t.absorbed, lv.absorbed)
                t.updated_ms = now_ms
                if TRACK_WINDOWS.index(window) > TRACK_WINDOWS.index(t.window):
                    t.window = window            # name it after the longest timeframe that saw it
                return
        self._levels.append(TrackedLevel(self._next, lv.side, lv.price, lv.absorbed, window, now_ms, now_ms))
        self._next += 1
        if len(self._levels) > MAX_LEVELS:
            self._levels.sort(key=lambda t: t.updated_ms)
            self._levels.pop(0)

    def check(self, now_ms: int, mid: float | None, sigma_1s_bps: float) -> list[MarketEvent]:
        """Advance every level against the current mid; returns the levels that broke on this tick."""
        if mid is None:
            return []
        margin = _margin_bps(sigma_1s_bps)
        broke: list[MarketEvent] = []
        keep: list[TrackedLevel] = []
        for t in self._levels:
            beyond = (mid < t.price * (1 - margin / 10_000)) if t.side == "bid" else (mid > t.price * (1 + margin / 10_000))
            if not beyond:
                t.beyond_since = None
            elif t.beyond_since is None:
                t.beyond_since = now_ms
            elif now_ms - t.beyond_since >= BREAK_CONFIRM_MS:
                self._broken.append((t.side, t.price, now_ms))
                if t.beyond_since - t.created_ms >= MIN_HELD_MS:
                    broke.append(self._record_break(t))
                continue
            if now_ms - t.updated_ms <= LEVEL_TTL_MS:
                keep.append(t)
        self._levels = keep
        return broke

    def _record_break(self, t: TrackedLevel) -> MarketEvent:
        """One event per burst of breaks on a side: a slide through three nearby bid levels is one story."""
        at = t.beyond_since or t.updated_ms
        group = self._group.get(t.side)
        if group and at - group[0] <= GROUP_MS:
            first, ev_id, levels = group
            levels.append(t)
            self.events = deque((e for e in self.events if e.id != ev_id), maxlen=self.events.maxlen)
        else:
            first, ev_id, levels = at, f"level-{self.coin}-{t.id}", [t]
        self._group[t.side] = (first, ev_id, levels)
        ev = _break_event(ev_id, first, levels)
        self.events.append(ev)
        return ev

    def active(self) -> list[TrackedLevel]:
        return sorted(self._levels, key=lambda t: -t.price)


def _minutes(ms: int) -> str:
    m = ms / 60_000
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def _margin_bps(sigma_1s_bps: float) -> float:
    """Half a normal one-minute move: how far beyond a level counts as through it."""
    return max(BREAK_MIN_BPS, 0.5 * sigma_1s_bps * math.sqrt(60))


def _break_event(ev_id: str, ts: int, levels: list[TrackedLevel]) -> MarketEvent:
    down = levels[0].side == "bid"
    prices = sorted(t.price for t in levels)
    # the level price was crossed: falling through the highest bid level, or breaking above the lowest offer
    edge = prices[-1] if down else prices[0]
    where = fmt_px(edge) if len(prices) == 1 else f"the {fmt_px(prices[0])} to {fmt_px(prices[-1])} zone"
    who = "buyers" if down else "sellers"
    absorbed = sum(t.absorbed for t in levels)
    longest = max(levels, key=lambda t: TRACK_WINDOWS.index(t.window))
    held = max((t.beyond_since or ts) - t.created_ms for t in levels)
    return MarketEvent(
        id=ev_id, kind="level_break", ts=ts, direction=Direction.DOWN if down else Direction.UP,
        title=(f"Price {'fell through' if down else 'broke above'} {where}, where {who} had soaked up "
               f"{fmt_usd(absorbed)} of {'selling' if down else 'buying'}"),
        detail=f"{who.capitalize()} held it for {_minutes(held)} ({longest.window} chart)", window=longest.window,
        stat=longest.window, price=edge, amount=absorbed, held_ms=held,
    )

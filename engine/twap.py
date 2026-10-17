"""
TWAP hunting (edge idea 3): a TWAP buys or sells a big order in slices at a steady beat. Hyperliquid's own TWAP
sends a market order every 30 seconds from the same wallet, for minutes to hours; bots do the same at other beats.
Once the beat is spotted, the rest of the order is still to come: does price keep drifting its way?

    slice    one wallet's aggressive fills on one coin and side within SLICE_MS of each other (a market order that
             took several price levels is one slice)
    run      slices from one wallet, coin and side at a steady beat: Hyperliquid's 30s (25-35s apart), or any beat
             of 8-120s that stays within 15% of the first gap ("regular")
    spotted  a run is acted on once it has K slices: before that, it can't be told from chance
"""
from dataclasses import dataclass, field

import numpy as np

from engine.holdclock import Grid
from engine.walletstudy import kept

SLICE_MS = 2000
NATIVE = (25_000, 35_000)
REGULAR = (8_000, 120_000)
REGULAR_TOL = 0.15


@dataclass
class Run:
    coin: str
    wallet: str
    side: int
    kind: str                       # "native" (30s) or "regular" (another steady beat)
    times: list = field(default_factory=list)
    usd: list = field(default_factory=list)

    @property
    def beat_s(self) -> float:
        return float(np.median(np.diff(self.times))) / 1000 if len(self.times) > 1 else 0.0


class Slices:
    """Aggressive fills → slices, per (wallet, coin, side)."""

    def __init__(self):
        self.by: dict[tuple, list] = {}             # key → [[t_ms, usd], ...]

    def add(self, wallet: str, coin: str, side: int, t_ms: int, usd: float) -> None:
        s = self.by.setdefault((wallet, coin, side), [])
        if s and t_ms - s[-1][0] <= SLICE_MS:
            s[-1][1] += usd
        else:
            s.append([t_ms, usd])


def runs(slices: Slices, min_slices: int, min_usd: float) -> list[Run]:
    """Every run of at least min_slices slices of min_usd+ each (native 30s and other steady beats)."""
    out = []
    for (wallet, coin, side), ss in slices.by.items():
        ss = [s for s in ss if s[1] >= min_usd]
        if len(ss) < min_slices:
            continue
        ss.sort()
        for kind in ("native", "regular"):
            cur = [ss[0]]
            first_gap = None
            for s in ss[1:]:
                gap = s[0] - cur[-1][0]
                if kind == "native":
                    fits = NATIVE[0] <= gap <= NATIVE[1]
                else:
                    fits = (REGULAR[0] <= gap <= REGULAR[1] and
                            (first_gap is None or abs(gap - first_gap) <= REGULAR_TOL * first_gap)
                            and not (len(cur) == 1 and NATIVE[0] <= gap <= NATIVE[1]))   # a 30s beat is native
                if fits:
                    if first_gap is None:
                        first_gap = gap
                    cur.append(s)
                else:
                    if len(cur) >= min_slices:
                        out.append(Run(coin, wallet, side, kind, [c[0] for c in cur], [c[1] for c in cur]))
                    cur, first_gap = [s], None
            if len(cur) >= min_slices:
                out.append(Run(coin, wallet, side, kind, [c[0] for c in cur], [c[1] for c in cur]))
    out.sort(key=lambda r: r.times[0])
    return out


def ride(g: Grid, run: Run, k: int, lag: int, hold_s: int | None, leverages, coin: str) -> dict | None:
    """Go the run's way `lag` seconds after its k-th slice; out after hold_s, or (hold_s None) 35 seconds after
    its last slice, when the beat has clearly stopped. → move at the mid (bps), the worst the best bid/ask went
    against it (bps), Papertrade per leverage (fraction of margin), and how far price had already gone its way
    during the first k slices (bps)."""
    if len(run.times) < k or not len(g):
        return None
    t0 = int(g.t[0])
    e = run.times[k - 1] // 1000 - t0 + lag
    x = e + hold_s if hold_s is not None else run.times[-1] // 1000 - t0 + 35
    s0 = run.times[0] // 1000 - t0
    if x <= e or s0 < 0 or x >= len(g) or not g.ok[s0:x + 1].all():
        return None
    side = run.side
    move = side * (g.mid[x] / g.mid[e] - 1)
    worst = (g.mid[e] - g.bid[e + 1:x + 1].min()) / g.mid[e] if side > 0 else (g.ask[e + 1:x + 1].max() - g.mid[e]) / g.mid[e]
    pt = {}
    for lev in leverages:
        if worst >= 1 / lev:
            pt[lev] = -1.0
        else:
            pt[lev] = float(kept(np.array([move]), coin)[0] * lev) if move > 0 else max(move * lev, -1.0)
    return {"bps": move * 1e4, "adv": max(worst, 0) * 1e4, "pt": pt, "before": side * (g.mid[e - lag] / g.mid[s0] - 1) * 1e4,
            "hold_s": x - e}

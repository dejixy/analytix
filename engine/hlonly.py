"""
Hyperliquid-only moves (edge idea 2): when Hyperliquid's price jumps and Binance and Bybit don't, the move is
usually local (one whale, one liquidation, a thin book) rather than news. Does it snap back, and by enough to fade?

    a move        Hyperliquid's mid over the last `window` seconds, in bps
    alone         Binance and Bybit both quoted in that time and each moved less than half as far the same way
                  (or the other way): only Hyperliquid moved
    confirmed     both moved at least 3/4 as far the same way: the whole market moved (for comparison)
    one per burst after a move is taken, the next one on that coin needs GAP_S seconds, so a 20-second slide is one
                  event, not twenty

Events come back as walletstudy Episodes (side = the way Hyperliquid moved, usd = the move's size in bps), so the
same pricing (lag, hold, Papertrade, busts) prices them; fading is follow_table(..., flip=True).
"""
import numpy as np

from engine.holdclock import Grid
from engine.walletstudy import Episode

WINDOWS_S = (3, 10, 30)
SIZES = ((3.0, 5.0, "3-5"), (5.0, 8.0, "5-8"), (8.0, 12.0, "8-12"), (12.0, np.inf, "12+"))
FLAT = 0.5
SAME = 0.75
FRESH_S = 2
GAP_S = 30


def venue_series(rows: dict[int, float]) -> tuple[int, np.ndarray, np.ndarray]:
    """{second: mid} → (first second, each second's mid carried forward, seconds since a real quote)."""
    if not rows:
        return 0, np.zeros(0), np.zeros(0)
    ts = np.array(sorted(rows))
    t0 = int(ts[0])
    n = int(ts[-1]) - t0 + 1
    mid = np.full(n, np.nan)
    mid[ts - t0] = [rows[int(t)] for t in ts]
    have = ~np.isnan(mid)
    last = np.where(have, np.arange(n), 0)
    np.maximum.accumulate(last, out=last)
    return t0, mid[last], (np.arange(n) - last).astype(float)


def moves(coin: str, hl: Grid, venues: list[tuple[int, np.ndarray, np.ndarray]], window: int, min_bps: float,
          gap_s: int = GAP_S) -> dict[str, list[Episode]]:
    """Every Hyperliquid move of min_bps+ over `window` seconds, sorted into "alone" and "confirmed"."""
    out = {"alone": [], "confirmed": []}
    n = len(hl)
    if n <= window or not venues:
        return out
    i = np.arange(window, n)
    h = (hl.mid[i] / hl.mid[i - window] - 1) * 1e4
    bad = np.concatenate([[0], np.cumsum(~hl.ok)])
    ok = (bad[i + 1] - bad[i - window]) == 0
    sign = np.sign(h)
    alone = ok & (np.abs(h) >= min_bps)
    confirmed = alone.copy()
    t0 = int(hl.t[0])
    for v0, vmid, age in venues:
        j = i + t0 - v0
        inside = (j - window >= 0) & (j < len(vmid))
        jj = np.clip(j, window, max(window, len(vmid) - 1))
        fresh = inside & (age[jj] <= FRESH_S) & (age[jj - window] <= FRESH_S) if len(vmid) > window else np.zeros(len(i), bool)
        e = np.where(fresh, (vmid[jj] / vmid[jj - window] - 1) * 1e4, np.nan) if len(vmid) > window else np.full(len(i), np.nan)
        along = e * sign                                   # how far this venue went Hyperliquid's way
        alone &= fresh & (along <= FLAT * np.abs(h))
        confirmed &= fresh & (along >= SAME * np.abs(h))
    for kind, mask in (("alone", alone), ("confirmed", confirmed)):
        last = -10**9
        for k in np.nonzero(mask)[0]:
            if i[k] - last < gap_s:
                continue
            last = i[k]
            out[kind].append(Episode(coin, kind, int(hl.t[i[k]]) * 1000, int(sign[k]), float(abs(h[k]))))
    return out

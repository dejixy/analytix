"""
Survival study: when the hold clock says one side lasts much longer than the other, does that side win?

Every STEP_S seconds of the recordings it rebuilds what the Hold clock panel showed at that moment: the survival
curves for a long and a short at the chosen leverage, from the trailing 24 hours (the "like now" starts when there
are enough of them, as the panel does), and the time each side lasts 9 times in 10. The side that lasts longer is
the favoured one, and gap = its time / the other side's. Then it checks the next 5 minutes: entering LAG_S seconds
later at the mid, did the mid reach the target before the best bid/ask reached the bust? Wins pay after
Papertrade's cut (engine/holdclock.papertrade_kept); a bust loses the whole margin.

To stay fast over a week of one-second rows, the trailing counts come from prefix sums over 30-second blocks and
volatility bins (log-spaced so that ±BAND bins is the panel's 2/3 to 1.5× band), not a fresh scan per moment.
"""
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from engine.holdclock import HORIZONS, MIN_STARTS, Grid, papertrade_kept, regime, time_at

STEP_S = 30
LOOKBACK_S = 24 * 3600
MIN_HISTORY_S = 6 * 3600
LAG_S = 2
RACE_S = 300
TREND_S = 600
BAND = 6
BIN_W = np.log(1.5) / BAND
NEVER_S = 1200.0            # still above 9 in 10 at 15 min: counted as 20 min
GAPS = ((0.0, 1.5, "under 1.5x"), (1.5, 2.0, "1.5-2x"), (2.0, 3.0, "2-3x"), (3.0, np.inf, "3x or more"))


def future_extreme(arr: np.ndarray, h: int, fn, chunk: int = 20_000) -> np.ndarray:
    """out[i] = fn(arr[i+1 : i+h+1]) for every i with a full window, in chunks so memory stays small."""
    m = len(arr) - h
    out = np.empty(max(m, 0))
    if m <= 0:
        return out
    v = sliding_window_view(arr[1:], h)
    for i in range(0, m, chunk):
        out[i:i + chunk] = fn(v[i:i + chunk], axis=1)
    return out


def _windows_ok(ok: np.ndarray, h: int) -> np.ndarray:
    bad = np.concatenate([[0], np.cumsum(~ok)])
    s = np.arange(len(ok) - h)
    return (bad[s + h + 1] - bad[s + 1]) == 0


@dataclass(slots=True)
class Moment:
    coin: str
    t: int                  # unix seconds
    long_s: float           # time a long lasts 9 times in 10
    short_s: float
    side: int               # the side that lasts longer (+1 long, -1 short)
    gap: float
    trend: int              # which way the mid moved over the last 10 minutes
    out: dict               # side → (outcome per target: "win" | "bust" | float move when neither)


def hold_times(g: Grid, dist: float, step: int = STEP_S, lookback: int = LOOKBACK_S,
               min_history: int = MIN_HISTORY_S, last: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(decision indices, long 9-in-10 time, short 9-in-10 time) as the panel would have shown them."""
    n = len(g)
    last = n if last is None else last
    vol = regime(g)
    with np.errstate(divide="ignore", invalid="ignore"):
        lv = np.log(vol)
    fin = np.isfinite(lv)
    lo = lv[fin].min() if fin.any() else 0.0
    nb = int(((lv[fin] - lo) / BIN_W).max()) + 1 if fin.any() else 1
    vbin = np.full(n, nb)                                             # bin nb = no volatility yet (all-curve only)
    vbin[fin] = ((lv[fin] - lo) / BIN_W).astype(int)
    nblocks = n // step + 1
    dec = np.arange(((min_history + step - 1) // step) * step, last, step)
    dec = dec[g.ok[dec]]
    sides = {+1: [], -1: []}                                           # per horizon: (all curve, like-now curve)
    for h in HORIZONS:
        if n <= h + 1:
            for s in sides:
                sides[s].append((np.full(len(dec), np.nan), np.full(len(dec), np.nan)))
            continue
        use = _windows_ok(g.ok, h) & g.ok[: n - h]
        hit_l = future_extreme(g.bid, h, np.min) <= g.mid[: n - h] * (1 - dist)
        hit_s = future_extreme(g.ask, h, np.max) >= g.mid[: n - h] * (1 + dist)
        idx = np.nonzero(use)[0]
        key = (idx // step) * (nb + 1) + vbin[idx]

        def table(weights):
            t = np.bincount(key, weights=weights, minlength=nblocks * (nb + 1)).reshape(nblocks, nb + 1)
            p = np.zeros((nblocks + 1, nb + 2))
            p[1:, 1:] = t.cumsum(0).cumsum(1)
            return p

        cnt, hl, hs = table(None), table(hit_l[idx].astype(float)), table(hit_s[idx].astype(float))
        b_hi = np.clip((dec - h) // step, 0, nblocks)                 # starts in [t - lookback, t - h]
        b_lo = np.clip((dec - lookback) // step, 0, nblocks)
        cb = vbin[dec]
        k_lo = np.where(cb < nb, np.clip(cb - BAND, 0, nb), 0)
        k_hi = np.where(cb < nb, np.clip(cb + BAND + 1, 0, nb), 0)

        def rect(p, r0, r1, c0, c1):
            return p[r1, c1] - p[r0, c1] - p[r1, c0] + p[r0, c0]

        all_n = rect(cnt, b_lo, b_hi, 0, nb + 1)
        like_n = rect(cnt, b_lo, b_hi, k_lo, k_hi)
        for s, p in ((+1, hl), (-1, hs)):
            with np.errstate(divide="ignore", invalid="ignore"):
                a = np.where(all_n >= MIN_STARTS, 1 - rect(p, b_lo, b_hi, 0, nb + 1) / all_n, np.nan)
                ln = np.where(like_n >= MIN_STARTS, 1 - rect(p, b_lo, b_hi, k_lo, k_hi) / like_n, np.nan)
            sides[s].append((a, ln))
    times = {}
    for s, per_h in sides.items():
        out = np.empty(len(dec))
        for i in range(len(dec)):
            like = [{"horizon": h, "survive": None if np.isnan(c[1][i]) else float(c[1][i])} for h, c in zip(HORIZONS, per_h)]
            allc = [{"horizon": h, "survive": None if np.isnan(c[0][i]) else float(c[0][i])} for h, c in zip(HORIZONS, per_h)]
            curve = like if any(c["survive"] is not None for c in like) else allc
            if not any(c["survive"] is not None for c in curve):
                out[i] = np.nan
                continue
            t = time_at(curve, 0.9)
            out[i] = NEVER_S if t is None else max(t, 1.0)
        times[s] = out
    return dec, times[+1], times[-1]


def race_from(g: Grid, i: int, side: int, dist: float, targets: list[float], race_s: int = RACE_S) -> list | None:
    """From an entry at the mid at second i: per target, "win" if the mid reached it before the best bid/ask reached
    the bust, "bust" if the bust came first, else the move at the end (fraction, + = in your favour)."""
    if i + race_s >= len(g) or not g.ok[i:i + race_s + 1].all():
        return None
    e = g.mid[i]
    mid = g.mid[i + 1:i + race_s + 1]
    if side > 0:
        bust = np.nonzero(g.bid[i + 1:i + race_s + 1] <= e * (1 - dist))[0]
    else:
        bust = np.nonzero(g.ask[i + 1:i + race_s + 1] >= e * (1 + dist))[0]
    b = bust[0] if len(bust) else race_s + 1
    out = []
    for tg in targets:
        hit = np.nonzero(side * (mid / e - 1) >= tg)[0]
        w = hit[0] if len(hit) else race_s + 1
        if w < b:
            out.append("win")
        elif b <= race_s:
            out.append("bust")
        else:
            out.append(float(side * (mid[-1] / e - 1)))
    return out


def moments(coin: str, g: Grid, leverage: float, targets: list[float], step: int = STEP_S,
            lag: int = LAG_S, min_history: int = MIN_HISTORY_S) -> list[Moment]:
    dist = 1 / leverage
    dec, tl, ts = hold_times(g, dist, step=step, min_history=min_history, last=len(g) - lag - RACE_S - 1)
    out = []
    for i, a, b in zip(dec, tl, ts):
        if np.isnan(a) or np.isnan(b) or a == b:
            continue
        res = {s: race_from(g, i + lag, s, dist, targets) for s in (+1, -1)}
        if res[+1] is None or res[-1] is None:
            continue
        side = 1 if a > b else -1
        j = max(0, i - TREND_S)
        trend = int(np.sign(g.mid[i] - g.mid[j]))
        out.append(Moment(coin, int(g.t[i]), float(a), float(b), side, max(a, b) / min(a, b), trend, res))
    return out


def payoff(result, target: float, leverage: float, coin: str) -> float:
    """What one trade paid, as a fraction of margin, after Papertrade's cut on wins."""
    if result == "win":
        return papertrade_kept(target, coin) * leverage
    if result == "bust":
        return -1.0
    return papertrade_kept(result, coin) * leverage if result > 0 else max(result * leverage, -1.0)


def block_t(times: np.ndarray, values: np.ndarray, block_s: int = 3600) -> float | None:
    """t-stat of the mean from hourly block means: neighbouring moments share the same next 5 minutes, so counting
    each as independent would overstate the certainty."""
    if len(values) < 2:
        return None
    blocks = times // block_s
    _, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=values)
    cnts = np.bincount(inv)
    means = sums / cnts
    if len(means) < 3 or means.std(ddof=1) == 0:
        return None
    return float(values.mean() / (means.std(ddof=1) / np.sqrt(len(means))))


def summarize(ms: list[Moment], pick, k: int, target: float, leverage: float, split_t: float) -> dict | None:
    """Stats for target k over the trades `pick(m)` returns (a list of sides per moment; empty = skip). split_t
    divides the data into an earlier and a later half."""
    rows = [(m, s) for m in ms for s in pick(m)]
    if not rows:
        return None
    res = [m.out[s][k] for m, s in rows]
    pay = np.array([payoff(r, target, leverage, m.coin) for r, (m, _) in zip(res, rows)])
    t = np.array([m.t for m, _ in rows])
    first, second = pay[t <= split_t], pay[t > split_t]
    return {"n": len(rows), "win": float(np.mean([r == "win" for r in res])),
            "bust": float(np.mean([r == "bust" for r in res])),
            "avg": float(pay.mean()), "t": block_t(t, pay),
            "first": float(first.mean()) if len(first) else None, "second": float(second.mean()) if len(second) else None}

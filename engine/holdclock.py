"""
Hold clock: how long a very high-leverage position survives, measured on the real order book.

At 800x the bust price is about 0.12% away, and on Papertrade (and on most venues at that leverage) the trigger
is the best bid or ask, not a candle close. So the right question isn't "how much does price move in an hour"
but "how often does the best bid dip that far within 10 seconds, a minute, five minutes?". This answers it
empirically: no bell curve, just what the book actually did.

    the grid      one row per second: the lowest best bid, the highest best ask and the last mid in that second
                  (api/holdclock.py builds it from the live feed and keeps 24 hours across restarts)
    survival      start at every second of the history; a long busts if the best bid later falls to
                  entry mid x (1 - distance) within the horizon (a short: the best ask rises to x (1 + distance)).
                  Survival = the share of starts that didn't. Gaps in the feed are left out, never guessed.
    like now      the same, using only the starts whose last 5 minutes moved about as much as the last 5
                  minutes have (between 2/3 and 1.5 times), because a quiet tape and a busy one are different games
    fill drift    how far the mid moves in the second or two between clicking and the fill landing on-chain
    the race      the chance the mid reaches your target before the best bid/ask reaches your bust
"""
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HORIZONS = (10, 30, 60, 120, 300, 900)
REGIME_S = 300
RACE_MAX_S = 300
RACE_STEP = 2                      # race starts every 2 seconds (it's a heavier calculation)
MIN_STARTS = 120


@dataclass(slots=True)
class Grid:
    t: np.ndarray        # seconds (int)
    bid: np.ndarray      # lowest best bid in the second
    ask: np.ndarray      # highest best ask in the second
    mid: np.ndarray      # last mid in the second
    ok: np.ndarray       # a real update arrived recently (False across feed gaps)

    def __len__(self) -> int:
        return len(self.t)


def make_grid(rows: list[tuple[int, float, float, float, bool]]) -> Grid:
    """[(second, low bid, high ask, last mid, ok)] sorted by second → arrays, with missing seconds filled from the
    second before and marked not ok."""
    if not rows:
        z = np.zeros(0)
        return Grid(z.astype(int), z, z, z, z.astype(bool))
    t0, t1 = rows[0][0], rows[-1][0]
    n = t1 - t0 + 1
    bid, ask, mid = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    ok = np.zeros(n, dtype=bool)
    for t, b, a, m, good in rows:
        i = t - t0
        bid[i], ask[i], mid[i], ok[i] = b, a, m, good
    for arr in (bid, ask, mid):                       # carry the last value across empty seconds
        idx = np.where(np.isnan(arr), 0, np.arange(n))
        np.maximum.accumulate(idx, out=idx)
        arr[:] = arr[idx]
    return Grid(np.arange(t0, t1 + 1), bid, ask, mid, ok)


def _windows_ok(ok: np.ndarray, h: int) -> np.ndarray:
    """For each start i (0..n-h-1): every second in (i, i+h] had live data."""
    bad = np.concatenate([[0], np.cumsum(~ok)])
    n = len(ok)
    starts = np.arange(n - h)
    return (bad[starts + h + 1] - bad[starts + 1]) == 0


def regime(g: Grid, window: int = REGIME_S) -> np.ndarray:
    """Each second's realized volatility over the previous `window` seconds (std of 1-second log returns)."""
    r = np.zeros(len(g))
    r[1:] = np.diff(np.log(g.mid))
    c1 = np.concatenate([[0.0], np.cumsum(r)])
    c2 = np.concatenate([[0.0], np.cumsum(r * r)])
    out = np.full(len(g), np.nan)
    if len(g) > window:
        s1 = c1[window + 1:] - c1[1:-window]
        s2 = c2[window + 1:] - c2[1:-window]
        out[window:] = np.sqrt(np.maximum(s2 / window - (s1 / window) ** 2, 0.0))
    return out


def survival(g: Grid, side: int, dist: float, horizons=HORIZONS, like_now: bool = False) -> list[dict]:
    """[{horizon, survive, starts}]: the share of past starts at which a position `dist` (fraction of price) from
    its bust lasted the horizon. side +1 = long (busts on the best bid), -1 = short (on the best ask)."""
    n = len(g)
    vol = regime(g) if like_now else None
    cur = vol[-1] if like_now and n else np.nan
    out = []
    for h in horizons:
        if n <= h + 1:
            out.append({"horizon": h, "survive": None, "starts": 0})
            continue
        if side > 0:
            worst = sliding_window_view(g.bid[1:], h).min(axis=1)[: n - h]
            hit = worst <= g.mid[: n - h] * (1 - dist)
        else:
            worst = sliding_window_view(g.ask[1:], h).max(axis=1)[: n - h]
            hit = worst >= g.mid[: n - h] * (1 + dist)
        use = _windows_ok(g.ok, h) & g.ok[: n - h]
        if like_now:
            if not np.isfinite(cur) or cur <= 0:
                out.append({"horizon": h, "survive": None, "starts": 0})
                continue
            v = vol[: n - h]
            use &= np.isfinite(v) & (v >= cur / 1.5) & (v <= cur * 1.5)
        m = int(use.sum())
        out.append({"horizon": h, "survive": float(1 - hit[use].mean()) if m >= MIN_STARTS else None, "starts": m})
    return out


def time_at(curve: list[dict], level: float) -> float | None:
    """The hold time (seconds) at which survival falls to `level`, interpolated between horizons. None if it never
    does within the longest horizon; 0 if it's already below at the shortest."""
    pts = [(0.0, 1.0)] + [(c["horizon"], c["survive"]) for c in curve if c["survive"] is not None]
    for (h0, s0), (h1, s1) in zip(pts, pts[1:]):
        if s1 <= level:
            return h0 + (h1 - h0) * (s0 - level) / (s0 - s1) if s0 != s1 else h1
    return None


def fill_drift(g: Grid, lags=(1, 2)) -> dict:
    """How far the mid moves in the 1-2 seconds between clicking and the fill landing, in bps."""
    out = {}
    for lag in lags:
        if len(g) <= lag + 10:
            continue
        ok = g.ok[lag:] & g.ok[:-lag]
        d = np.abs(np.log(g.mid[lag:] / g.mid[:-lag]))[ok] * 1e4
        if len(d) >= MIN_STARTS:
            out[f"{lag}s"] = {"p50": float(np.percentile(d, 50)), "p90": float(np.percentile(d, 90)),
                              "p99": float(np.percentile(d, 99))}
    return out


def race(g: Grid, side: int, dist: float, target: float, max_s: int = RACE_MAX_S, step: int = RACE_STEP) -> dict | None:
    """From past starts: the share where the mid reached the target (fraction of price, in your favour) before the
    best bid/ask reached the bust, the share where the bust came first, and the share where neither happened within
    max_s. Also the median time to the target when it came first."""
    n = len(g)
    if n <= max_s + 2:
        return None
    starts = np.arange(0, n - max_s - 1, step)
    starts = starts[g.ok[starts]]
    if len(starts) < MIN_STARTS // 2:
        return None
    win_mid = sliding_window_view(g.mid[1:], max_s)[starts]
    entry = g.mid[starts][:, None]
    if side > 0:
        tgt = win_mid >= entry * (1 + target)
        bust = sliding_window_view(g.bid[1:], max_s)[starts] <= entry * (1 - dist)
    else:
        tgt = win_mid <= entry * (1 - target)
        bust = sliding_window_view(g.ask[1:], max_s)[starts] >= entry * (1 + dist)
    big = max_s + 1
    t_first = np.where(tgt.any(axis=1), tgt.argmax(axis=1), big)
    b_first = np.where(bust.any(axis=1), bust.argmax(axis=1), big)
    won = t_first < b_first
    lost = b_first <= t_first
    lost &= b_first < big
    k = len(starts)
    return {"target_first": float(won.mean()), "bust_first": float(lost.mean()),
            "neither": float(1 - won.mean() - lost.mean()), "starts": k, "within_s": max_s,
            "median_s_to_target": float(np.median(t_first[won] + 1)) if won.any() else None}


def now_read(g: Grid, window: int = REGIME_S) -> dict:
    """Is the tape quieter or busier than usual? The last 5 minutes' volatility against the day's typical."""
    vol = regime(g, window)
    fin = vol[np.isfinite(vol)]
    if not len(fin):
        return {"vol_ratio": None, "sigma_1s_bps": None}
    return {"vol_ratio": float(vol[-1] / np.median(fin)) if np.median(fin) > 0 and np.isfinite(vol[-1]) else None,
            "sigma_1s_bps": float(vol[-1] * 1e4) if np.isfinite(vol[-1]) else None}


def papertrade_net(gain_frac_of_margin: float, deadband_bps: float, leverage: float, win_fee: float = 0.02) -> float:
    """What a winning close nets on Papertrade, as a fraction of margin: the gain is measured as if the exit were
    `deadband_bps` worse, then the 2% win fee. (Their asymmetric impact haircut isn't public in a formula here yet,
    so it isn't included: real wins are a little smaller, small ones most.)"""
    g = gain_frac_of_margin - deadband_bps / 1e4 * leverage
    return max(0.0, g) * (1 - win_fee)

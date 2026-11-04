"""
Fresh money: how much of a market's aggressive (taker) volume comes from wallets the recordings had never seen until
recently, and whether a burst of it comes before big moves. A metric, not a list of wallets: no address is followed.

    seen        every wallet's first appearance in the recordings, as taker or maker, on any recorded coin
    fresh       a taker trade by a wallet first seen less than FRESH_MS ago (24 h); "brand new" under NEW_MS (1 h)
    buckets     per coin, per STEP_MS (5 min): taker dollars, fresh dollars, fresh net (bought minus sold), all net,
                brand-new dollars and the last price. Fills the exchange's engine made itself (liquidations) are left
                out: they're forced, not anyone's choice
    burn-in     "never seen" only means something once the recordings know the regulars, so nothing counts before
                BURN_DAYS of history

The test (spikes() and score()) is decided before any result is seen; scripts/freshMoney.py runs it on the fast store.
"""
from dataclasses import dataclass, field

import numpy as np

STEP_MS = 5 * 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
FRESH_MS = 24 * HOUR_MS
NEW_MS = HOUR_MS
BURN_DAYS = 7                   # history before "fresh" counts
LOOKBACK_DAYS = 7               # a day's spike threshold comes from up to this many days before it ...
MIN_LOOKBACK_DAYS = 3           # ... and at least this many (after the burn-in)
WINDOW = 12                     # buckets in the rolling window (60 min)
SPIKE_Q = 0.99                  # a spike: the window's fresh share in the top 1% of the lookback (a day or two each)
COOLDOWN = 48                   # buckets (4 h) before the same coin can spike again
HORIZONS = {"1h": 12, "4h": 48}
NEVER = np.iinfo(np.int64).max


class Seen:
    """First appearance of every wallet. Feed days in time order; within a day the earliest trade wins, so a wallet's
    first-seen time at any trade is never after that trade (no look-ahead)."""

    def __init__(self):
        self.ids: dict[str, int] = {}
        self.first = np.zeros(0, dtype=np.int64)

    def gids(self, wallets: np.ndarray) -> np.ndarray:
        """Global ids for one load's address table (grows the table for addresses not met before)."""
        ids = self.ids
        out = np.fromiter((ids.setdefault(str(a), len(ids)) for a in wallets), dtype=np.int64, count=len(wallets))
        if len(ids) > len(self.first):
            self.first = np.concatenate([self.first, np.full(len(ids) - len(self.first), NEVER, dtype=np.int64)])
        return out

    def update(self, wid: np.ndarray, t: np.ndarray) -> None:
        ok = wid >= 0
        np.minimum.at(self.first, wid[ok], t[ok].astype(np.int64))

    def age(self, wid: np.ndarray, t: np.ndarray) -> np.ndarray:
        """Milliseconds since each wallet was first seen (NEVER where the wallet is missing)."""
        out = np.full(len(wid), NEVER, dtype=np.int64)
        ok = wid >= 0
        out[ok] = t[ok].astype(np.int64) - self.first[wid[ok]]
        return out


@dataclass
class Coin:
    """One coin's buckets, appended day by day, plus its fresh trades (for the concentration check)."""
    k: list = field(default_factory=list)
    cols: dict = field(default_factory=lambda: {c: [] for c in ("total", "fresh", "fnet", "net", "new", "px")})
    fresh_t: list = field(default_factory=list)
    fresh_w: list = field(default_factory=list)
    fresh_usd: list = field(default_factory=list)

    def add(self, t, px, usd, side, taker, age) -> None:
        """One day of a coin's taker trades (time order). `side` +1 the taker bought; `age` from Seen.age."""
        if not len(t):
            return
        k = (t // STEP_MS).astype(np.int64)
        uk, inv = np.unique(k, return_inverse=True)
        fresh = age < FRESH_MS
        new = age < NEW_MS
        n = len(uk)
        self.k.append(uk)
        self.cols["total"].append(np.bincount(inv, weights=usd, minlength=n))
        self.cols["fresh"].append(np.bincount(inv, weights=usd * fresh, minlength=n))
        self.cols["fnet"].append(np.bincount(inv, weights=usd * side * fresh, minlength=n))
        self.cols["net"].append(np.bincount(inv, weights=usd * side, minlength=n))
        self.cols["new"].append(np.bincount(inv, weights=usd * new, minlength=n))
        last = np.zeros(n, dtype=np.int64)
        np.maximum.at(last, inv, np.arange(len(t)))            # the last trade in each bucket (t is sorted)
        self.cols["px"].append(px[last].astype(float))
        self.fresh_t.append(t[fresh])
        self.fresh_w.append(taker[fresh])
        self.fresh_usd.append(usd[fresh] * side[fresh])

    def grid(self) -> dict[str, np.ndarray]:
        """The buckets on a gap-free 5-minute grid (no trades: zero dollars, the price carried forward). A bucket that
        came in two parts (an hour's file holding a few trades past midnight) is summed, its price the later one."""
        if not self.k:
            return {"k": np.zeros(0, dtype=np.int64)}
        k = np.concatenate(self.k)
        uk, inv = np.unique(k, return_inverse=True)
        full = np.arange(uk[0], uk[-1] + 1)
        idx = uk - uk[0]
        out = {"k": full}
        for c, parts in self.cols.items():
            v = np.concatenate(parts)
            col = np.full(len(full), np.nan if c == "px" else 0.0)
            if c == "px":
                order = np.zeros(len(uk), dtype=np.int64)
                np.maximum.at(order, inv, np.arange(len(k)))   # the later part's price
                col[idx] = v[order]
            else:
                col[idx] = np.bincount(inv, weights=v, minlength=len(uk))
            out[c] = col
        px = out["px"]
        last = np.where(~np.isnan(px), np.arange(len(px)), 0)
        np.maximum.accumulate(last, out=last)
        out["px"] = px[last]
        return out

    def fresh_trades(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not self.fresh_t:
            return np.zeros(0), np.zeros(0, dtype=np.int64), np.zeros(0)
        return np.concatenate(self.fresh_t), np.concatenate(self.fresh_w), np.concatenate(self.fresh_usd)


def roll(x: np.ndarray, n: int = WINDOW) -> np.ndarray:
    """Sum of the last n values, the window ending at each position (shorter at the start)."""
    c = np.cumsum(np.r_[0.0, x])
    i = np.arange(1, len(x) + 1)
    return c[i] - c[np.maximum(0, i - n)]


def thresholds(values: np.ndarray, k: np.ndarray, start_k: int, q: float, lookback=LOOKBACK_DAYS,
               min_days=MIN_LOOKBACK_DAYS) -> np.ndarray:
    """Each bucket's threshold: the q-quantile of `values` over the whole days before its day (up to `lookback` of
    them, counted from `start_k` on), NaN until `min_days` are there. Walk-forward: a day never sees itself."""
    per_day = DAY_MS // STEP_MS
    day = k // per_day
    first_day = start_k // per_day + (1 if start_k % per_day else 0)
    out = np.full(len(k), np.nan)
    for d in np.unique(day):
        lo = max(first_day, d - lookback)
        if d - lo < min_days:
            continue
        past = values[(day >= lo) & (day < d) & (k >= start_k) & ~np.isnan(values)]
        if len(past):
            out[day == d] = np.quantile(past, q)
    return out


def spikes(g: dict, burn_k: int, q: float = SPIKE_Q, cooldown: int = COOLDOWN, kind: str = "fresh") -> tuple[np.ndarray, np.ndarray]:
    """Indices into the grid where a spike ends its window.

    fresh   the window's fresh share of taker dollars above its day's threshold (the lookback's top 1%), with at
            least the lookback's median fresh dollars (a big share of a dead hour isn't money arriving)
    volume  the control: taker dollars above their threshold while the fresh share is at or under its median
            (busy, but not with new money)
    One spike per coin per `cooldown` buckets, the first one counting. Also returns the test mask: the buckets that
    had a threshold (the baseline's starts)."""
    k = g["k"]
    tot, fr = roll(g["total"]), roll(g["fresh"])
    share = np.divide(fr, tot, out=np.full(len(tot), np.nan), where=tot > 0)
    valid = (k >= burn_k + WINDOW) & (tot > 0) & ~np.isnan(g["px"])
    s_v = np.where(valid, share, np.nan)
    if kind == "fresh":
        thr = thresholds(s_v, k, burn_k, q)
        floor = thresholds(np.where(valid, fr, np.nan), k, burn_k, 0.5)
        hit = valid & (s_v > thr) & (fr >= floor) & (fr > 0)            # above the threshold, not tied with it
    else:
        thr = thresholds(np.where(valid, tot, np.nan), k, burn_k, q)
        med = thresholds(s_v, k, burn_k, 0.5)
        hit = valid & (tot > thr) & (s_v <= med)
    out, last = [], -10**9
    for i in np.flatnonzero(hit):
        if i - last >= cooldown:
            out.append(i)
            last = i
    return np.array(out, dtype=np.int64), valid & ~np.isnan(thr)


def moves(px: np.ndarray, i: np.ndarray, h: int) -> np.ndarray:
    """Log moves in bps from bucket i to i + h (negative h: from i + h to i); NaN off the grid."""
    j = i + h
    ok = (j >= 0) & (j < len(px))
    out = np.full(len(i), np.nan)
    a, b = (px[i[ok]], px[j[ok]]) if h > 0 else (px[j[ok]], px[i[ok]])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[ok] = np.log(b / a) * 1e4
    return out


def _t(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x)))


def score(g: dict, idx: np.ndarray, direction: np.ndarray, test: np.ndarray) -> dict:
    """What followed the spikes at `idx`: for each horizon, the average size of the move against every 5-minute
    start in the test period (the baseline), and the move in the spike's direction. `test` marks the grid buckets
    that count as test starts."""
    px = g["px"]
    base_i = np.flatnonzero(test)
    out = {"n": int(len(idx))}
    for name, h in HORIZONS.items():
        ev = moves(px, idx, h)
        base = np.abs(moves(px, base_i, h))
        b = float(np.nanmean(base)) if len(base) else float("nan")
        size = np.abs(ev)
        signed = direction * ev
        out[name] = {"abs": float(np.nanmean(size)) if len(idx) else float("nan"), "base_abs": b,
                     "ratio": float(np.nanmean(size) / b) if len(idx) and b else float("nan"),
                     "t_abs": _t(size - b), "signed": float(np.nanmean(signed)) if len(idx) else float("nan"),
                     "t_signed": _t(signed), "win": float(np.nanmean(signed > 0)) if len(idx) else float("nan")}
    before = np.abs(moves(px, idx, -WINDOW))
    base_before = np.abs(moves(px, base_i, -WINDOW))
    out["before_ratio"] = float(np.nanmean(before) / np.nanmean(base_before)) if len(idx) and len(base_i) else float("nan")
    return out


def concentration(fresh: tuple, t0: float, t1: float) -> float | None:
    """The biggest single wallet's share of the fresh dollars in [t0, t1): is a "spike" really one wallet?"""
    t, w, usd = fresh
    m = (t >= t0) & (t < t1)
    if not m.any():
        return None
    uw, inv = np.unique(w[m], return_inverse=True)
    per = np.bincount(inv, weights=np.abs(usd[m]))
    return float(per.max() / per.sum()) if per.sum() else None

"""
Sentiment Check: when the market looked like it does now, what did price do next?

Every past moment of five years of 1-minute candles (three years of 1-second candles for the shortest horizons) is
described by a few conditions, each in a handful of bins, all from data known at that moment:

    vol       how loud the last hour (5 minutes, for the short horizons) was against the 30 days before:
              quiet (under 0.75x), normal, loud (over 1.33x)
    news      a scheduled US release (CPI, jobs, Fed decision) due within the horizon (at least 30 minutes ahead),
              just out (within the last 30 minutes, or the horizon if longer), or neither
    move      the move over the same span as the horizon, in typical moves: strong down, down, flat, up, strong up
              (beyond -1.5, -0.5, 0.5, 1.5)
    volume    the last hour's (minute's) volume against the 30 days before: light (under 0.7x), normal, heavy (1.5x)
    session   Asia (00-07 UTC), Europe (07-13), US (13-20), late (20-24), weekend
    buyers    the takers' buy share of the last 15 minutes (minute): sellers (under 47%), balanced, buyers (over 53%)
    oi        open interest's change over the last hour: falling (under -0.5%), flat, rising
    funding   the last funding rate: negative, normal, high (over 0.02% a period)

and by what followed over each horizon (10 seconds to 24 hours): whether price finished higher, how far, and how far
it dipped and rose on the way. Moves are counted in typical moves (the 30-day normal of a step's move, scaled to the
horizon), so a read is sized to today's volatility rather than 2021's. Horizons are counted on stretches that don't
overlap (one start per horizon length), so "9 of 14" is 14 separate occasions.

    tables    for every combination of the first k conditions (k = 0..8, in the order above), per year: how many
              times, how many finished higher, the spread of the move and how often it dipped or rose past set
              distances. Rows with fewer than MIN_N cases are dropped when saved.
    like now  the most specific match with at least MIN_N past cases: conditions are dropped from the end of the
              list until enough cases remain, and which ones were kept is shown.
    held up   a lean (higher or lower more often than usual) learned from the years before each year, tested on that
              year, pooled. Only a lean with a one-sided z of 2 or more counts as a direction read.
    check     each year scored on the tables from the years before it: the like-now chance of finishing higher and of
              dipping or rising past each distance, against the plain rate, by Brier score. Positive skill = the
              conditions told you something the plain rate didn't.
    drivers   which condition bins came before big moves (beyond 1.5 typical moves) more often than usual.

Candles are Binance trade prices, not Hyperliquid's best bid, so a dip here is a little shallower than the bid's.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

import numpy as np

HORIZONS = {"10s": 10, "30s": 30, "1m": 60, "2m": 120, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "24h": 86400}
SHORT = ("10s", "30s", "1m", "2m")              # from 1-second candles; the rest from 1-minute candles
FEATURES = ("vol", "news", "move", "volume", "session", "buyers", "oi", "funding")
NBINS = {"vol": 3, "news": 3, "move": 5, "volume": 3, "session": 5, "buyers": 3, "oi": 3, "funding": 3}
LABELS = {"vol": ("quiet", "normal", "loud"), "news": ("none due", "release due", "just out"),
          "move": ("strong down", "down", "flat", "up", "strong up"), "volume": ("light", "normal", "heavy"),
          "session": ("Asia", "Europe", "US", "late", "weekend"), "buyers": ("sellers", "balanced", "buyers"),
          "oi": ("falling", "flat", "rising"), "funding": ("negative", "normal", "high")}
NAMES = {"vol": "Volatility", "news": "News", "move": "Move so far", "volume": "Volume", "session": "Session",
         "buyers": "Taker flow", "oi": "Open interest", "funding": "Funding"}
EDGES = {"vol": (0.75, 1.333), "move": (-1.5, -0.5, 0.5, 1.5), "volume": (0.7, 1.5), "buyers": (0.47, 0.53),
         "oi": (-0.005, 0.005), "funding": (0.0, 0.0002)}
WIDTH = {f: NBINS[f] + 1 for f in FEATURES}      # each condition's bins and "unknown"
MIN_N = 30
NORMAL_DAYS = 30
RET_Z = np.arange(-5.0, 5.001, 0.25)              # the move's spread, in typical moves
LADDER = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0])
BIG = 1.5
SHOW = (0.5, 1.0, 2.0)                            # dips and rises shown at these multiples of the typical move
M_UP, M_SIZE = 400, 30                            # how many cases' weight a level's rates borrow from the level above


def windows(short: bool) -> dict:
    """Seconds: volatility, taker flow and volume windows."""
    return {"vol": 300, "flow": 60, "volume": 60} if short else {"vol": 3600, "flow": 900, "volume": 3600}


# ── small array helpers ──────────────────────────────────────────────────────
def binned(x: np.ndarray, edges) -> np.ndarray:
    """Values → bins 0..len(edges); NaN → "unknown" (len(edges) + 1)."""
    x = np.asarray(x, dtype=np.float64)
    out = np.searchsorted(np.asarray(edges), x, side="right").astype(np.int16)
    out[~np.isfinite(x)] = len(edges) + 1
    return out


def session_of(t_s) -> np.ndarray:
    t_s = np.asarray(t_s, dtype=np.int64)
    hour = (t_s // 3600) % 24
    weekday = ((t_s // 86400) + 3) % 7                # 1970-01-01 was a Thursday; 0 = Monday
    s = np.where(hour < 7, 0, np.where(hour < 13, 1, np.where(hour < 20, 2, 3))).astype(np.int16)
    s[weekday >= 5] = 4
    return s


def news_of(t_s, horizon_s: int, releases_s: np.ndarray) -> np.ndarray:
    """0 none due, 1 a release due within the horizon (30 minutes at least), 2 one out within the last 30 minutes
    (or the horizon, if longer)."""
    t_s = np.asarray(t_s, dtype=np.int64)
    w = min(max(horizon_s, 1800), 86400)
    out = np.zeros(len(t_s), dtype=np.int16)
    if not len(releases_s):
        return out
    i = np.searchsorted(releases_s, t_s, side="right")
    nxt = releases_s[np.minimum(i, len(releases_s) - 1)]
    prv = releases_s[np.maximum(i - 1, 0)]
    out[(i > 0) & (t_s - prv < w)] = 2
    out[(i < len(releases_s)) & (nxt - t_s <= w) & (nxt > t_s)] = 1
    return out


def years_of(t_s) -> np.ndarray:
    return np.asarray(t_s, dtype="datetime64[s]").astype("datetime64[Y]").astype(np.int64) + 1970


def _roll_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Sum of the w values ending at each index (NaN for the first w-1)."""
    c = np.concatenate([[0.0], np.cumsum(np.nan_to_num(np.asarray(x, dtype=np.float64)))])
    out = np.full(len(x), np.nan)
    if w <= len(x):
        out[w - 1:] = c[w:] - c[:-w]
    return out


# ── candles on a full grid ───────────────────────────────────────────────────
@dataclass
class Series:
    """One coin's candles on a full time grid (step 1 s or 60 s)."""
    step: int
    t: np.ndarray        # seconds
    c: np.ndarray
    h: np.ndarray
    l: np.ndarray
    v: np.ndarray
    tbv: np.ndarray
    ok: np.ndarray       # a real candle, or a short quiet gap (no trades); False across feed gaps

    def __len__(self) -> int:
        return len(self.t)


def to_grid(t_units, c, h, l, v, tbv, step: int, quiet_s: int = 0) -> Series:
    """Candles keyed by time in units of `step` seconds → a full grid from first to last. Missing candles take the
    last close; gaps of up to quiet_s seconds count as quiet (no trades), longer ones as missing data."""
    t_units = np.asarray(t_units, dtype=np.int64)
    if not len(t_units):
        z = np.zeros(0)
        return Series(step, z.astype(np.int64), z, z, z, z, z, z.astype(bool))
    i = t_units - t_units[0]
    n = int(i[-1]) + 1
    have = np.zeros(n, dtype=bool)
    have[i] = True
    grid = {}
    for name, x in (("c", c), ("h", h), ("l", l), ("v", v), ("tbv", tbv)):
        a = np.full(n, np.nan)
        a[i] = np.asarray(x, dtype=np.float64)
        grid[name] = a
    idx = np.where(have, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    for name in ("c", "h", "l"):
        grid[name] = np.where(have, grid[name], grid["c"][idx])
    for name in ("v", "tbv"):
        grid[name] = np.where(have, grid[name], 0.0)
    ok = have.copy()
    if quiet_s >= step and not have.all():
        nxt = np.where(have, np.arange(n), n)
        nxt = np.minimum.accumulate(nxt[::-1])[::-1]   # the next real candle
        gap = nxt - idx - 1                            # the gap's length, for every step inside it
        ok |= (~have) & (gap * step <= quiet_s)
    return Series(step, (t_units[0] + np.arange(n)) * step, grid["c"], grid["h"], grid["l"], grid["v"], grid["tbv"], ok)


def raw(s: Series, short: bool) -> dict:
    """Each step's log return, recent volatility (per step), volume over the volume window and takers' buy share."""
    w = {k: max(1, v // s.step) for k, v in windows(short).items()}
    lr = np.zeros(len(s))
    if len(s) > 1:
        lr[1:] = np.diff(np.log(s.c))
    lr[~s.ok] = 0.0
    rv = np.sqrt(np.maximum(_roll_sum(lr * lr, w["vol"]) / w["vol"], 0))
    vsum = _roll_sum(s.v, w["volume"])
    fv = _roll_sum(s.v, w["flow"])
    with np.errstate(invalid="ignore", divide="ignore"):
        buy = np.where(fv > 0, _roll_sum(s.tbv, w["flow"]) / fv, np.nan)
    buy[_roll_sum(np.isnan(s.tbv), w["flow"]) > 0] = np.nan     # no taker split from this source
    return {"lr": lr, "rv": rv, "vsum": vsum, "buy": buy}


# ── 30-day normals, from daily sums ──────────────────────────────────────────
class Normals:
    """Per-day sums of recent volatility, volume and squared returns. A time's normal is the average over the
    NORMAL_DAYS days before its own day, so it never uses the day it's in."""

    def __init__(self):
        self.days: dict[int, np.ndarray] = {}           # day → [steps, rv sum, volume sum, lr² sum]

    def add(self, s: Series, r: dict, since_s: int | None = None) -> None:
        good = s.ok & np.isfinite(r["rv"]) & np.isfinite(r["vsum"])
        if since_s is not None:
            good &= s.t >= since_s
        if not good.any():
            return
        day = s.t[good] // 86400
        days, inv = np.unique(day, return_inverse=True)
        sums = np.stack([np.bincount(inv, minlength=len(days)).astype(np.float64),
                         np.bincount(inv, weights=r["rv"][good], minlength=len(days)),
                         np.bincount(inv, weights=r["vsum"][good], minlength=len(days)),
                         np.bincount(inv, weights=(r["lr"] ** 2)[good], minlength=len(days))], axis=1)
        for d, row in zip(days, sums):
            self.days[int(d)] = self.days.get(int(d), 0) + row

    def at(self, t_s) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(volatility normal, volume normal, a step's typical move) at each time."""
        t_s = np.asarray(t_s, dtype=np.int64)
        nan = np.full(len(t_s), np.nan)
        if self._fixed is not None:
            n = self._fixed[0]
            k = len(t_s)
            return np.full(k, self._fixed[1] / n), np.full(k, self._fixed[2] / n), np.full(k, math.sqrt(self._fixed[3] / n))
        if not self.days:
            return nan, nan.copy(), nan.copy()
        D = np.array(sorted(self.days), dtype=np.int64)
        S = np.concatenate([np.zeros((1, 4)), np.cumsum(np.stack([self.days[int(d)] for d in D]), axis=0)])
        day = t_s // 86400
        lo = np.searchsorted(D, day - NORMAL_DAYS, side="left")
        hi = np.searchsorted(D, day, side="left")
        tot = S[hi] - S[lo]
        enough = (hi - lo >= min(3, NORMAL_DAYS)) & (tot[:, 0] > 0)
        n = np.where(enough, tot[:, 0], np.nan)
        return tot[:, 1] / n, tot[:, 2] / n, np.sqrt(tot[:, 3] / n)

    def latest(self) -> "Normals":
        """The same normals, but every time judged on the last NORMAL_DAYS days stored (for the live read, so a
        few days' lag in the stored history doesn't leave it without a normal)."""
        out = Normals()
        last = sorted(self.days)[-NORMAL_DAYS:]
        tot = sum((self.days[d] for d in last), np.zeros(4))
        out.days = dict(self.days)
        out._fixed = tot if len(last) >= 3 and tot[0] > 0 else None
        return out

    _fixed = None

    def to_json(self, keep_days: int = NORMAL_DAYS + 10) -> dict:
        last = sorted(self.days)[-keep_days:]
        return {str(d): [float(x) for x in self.days[d]] for d in last}

    @classmethod
    def from_json(cls, d: dict) -> "Normals":
        out = cls()
        out.days = {int(k): np.array(v, dtype=np.float64) for k, v in (d or {}).items()}
        return out


@dataclass
class Prepared:
    """What every horizon shares: the horizon-free condition bins, log prices and a step's typical move."""
    s: Series
    vol: np.ndarray
    volume: np.ndarray
    buyers: np.ndarray
    oi: np.ndarray
    funding: np.ndarray
    session: np.ndarray
    logc: np.ndarray
    sig: np.ndarray


def _align(minutes, values, t_s) -> np.ndarray:
    """A slow series keyed by minute, as it stood at each time (its last value at or before it)."""
    if minutes is None or not len(minutes):
        return np.full(len(t_s), np.nan)
    j = np.searchsorted(minutes, np.asarray(t_s) // 60, side="right") - 1
    out = np.asarray(values, dtype=np.float64)[np.clip(j, 0, None)]
    out[j < 0] = np.nan
    return out


def prepare(s: Series, r: dict, norms: Normals, oi: dict | None = None, funding: dict | None = None) -> Prepared:
    rv_n, vol_n, sig = norms.at(s.t)
    oi_t = oi.get("t") if oi else None
    oi_v = oi.get("oi") if oi else None
    oi_now, oi_then = _align(oi_t, oi_v, s.t), _align(oi_t, oi_v, s.t - 3600)
    fund = _align(funding.get("t") if funding else None, funding.get("rate") if funding else None, s.t)
    with np.errstate(invalid="ignore", divide="ignore"):
        return Prepared(s, binned(r["rv"] / rv_n, EDGES["vol"]), binned(r["vsum"] / vol_n, EDGES["volume"]),
                        binned(r["buy"], EDGES["buyers"]), binned(oi_now / oi_then - 1, EDGES["oi"]),
                        binned(fund, EDGES["funding"]), session_of(s.t), np.log(s.c), sig)


def bins_at(p: Prepared, idx, horizon_s: int, releases_s: np.ndarray) -> np.ndarray:
    """The conditions at grid indices idx for this horizon: shape (len(idx), 8), in FEATURES order."""
    idx = np.asarray(idx, dtype=np.int64)
    L = max(1, horizon_s // p.s.step)
    back = idx - L
    bad = np.concatenate([[0], np.cumsum(~p.s.ok)])
    ok_back = (back >= 0) & ((bad[idx + 1] - bad[np.maximum(back, 0) + 1]) == 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        move = (p.logc[idx] - p.logc[np.maximum(back, 0)]) / (p.sig[idx] * math.sqrt(L))
    move[~ok_back] = np.nan
    cols = {"vol": p.vol[idx], "news": news_of(p.s.t[idx], horizon_s, releases_s),
            "move": binned(move, EDGES["move"]), "volume": p.volume[idx], "session": p.session[idx],
            "buyers": p.buyers[idx], "oi": p.oi[idx], "funding": p.funding[idx]}
    return np.stack([cols[f] for f in FEATURES], axis=1).astype(np.int16)


def scale_at(p: Prepared, i: int, horizon_s: int) -> float:
    """One typical move's unit for this horizon at index i, in bps (a step's 30-day typical move × √steps)."""
    L = max(1, horizon_s // p.s.step)
    return float(p.sig[i] * math.sqrt(L) * 1e4)


# ── outcomes ─────────────────────────────────────────────────────────────────
def outcomes(p: Prepared, horizon_s: int, first: int = 0):
    """Stretches of the horizon's length starting at `first`, one after another: (start index, move, lowest dip
    (≤ 0) and highest rise (≥ 0), all in typical moves). Stretches with a feed gap or no normal yet are left out."""
    s = p.s
    H = max(1, horizon_s // s.step)
    n = (len(s) - 1 - first) // H
    if n <= 0:
        z = np.zeros(0)
        return z.astype(np.int64), z, z, z
    start = first + np.arange(n) * H
    blk = slice(first + 1, first + n * H + 1)
    ok = s.ok[blk].reshape(n, H).all(axis=1)
    unit = p.sig[start] * math.sqrt(H)
    ok &= np.isfinite(unit) & (unit > 0)
    c0 = s.c[start]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.log(s.c[start + H] / c0) / unit
        dip = np.minimum(np.log(s.l[blk].reshape(n, H).min(axis=1) / c0), 0) / unit
        rise = np.maximum(np.log(s.h[blk].reshape(n, H).max(axis=1) / c0), 0) / unit
    return start[ok], ret[ok], dip[ok], rise[ok]


# ── tables ───────────────────────────────────────────────────────────────────
def _codes(bins: np.ndarray, k: int) -> np.ndarray:
    code = np.zeros(len(bins), dtype=np.int64)
    for f in range(k):
        code = code * WIDTH[FEATURES[f]] + bins[:, f]
    return code


@dataclass
class Table:
    """One coin and horizon: every condition combination's record."""
    horizon: str
    years: np.ndarray                   # calendar years covered
    levels: list                        # per k = 0..8: codes[K], n[K,Y], up[K,Y], s1[K], s2[K], hist[K,B], dip[K,D], rise[K,D]
    drivers: dict                       # feature → [n, big up, big down] per bin

    def rows(self, bins: np.ndarray, min_n: int = MIN_N) -> tuple[np.ndarray, np.ndarray]:
        """For each case's conditions, the most specific level with at least min_n cases: (k, row) arrays
        (k = -1 when the table is empty)."""
        bins = np.atleast_2d(bins)
        K = np.full(len(bins), -1, dtype=np.int64)
        R = np.zeros(len(bins), dtype=np.int64)
        for k in range(len(FEATURES), -1, -1):
            todo = K < 0
            if not todo.any():
                break
            lv = self.levels[k]
            if not len(lv["codes"]):
                continue
            code = _codes(bins[todo], k)
            r = np.minimum(np.searchsorted(lv["codes"], code), len(lv["codes"]) - 1)
            hit = (lv["codes"][r] == code) & (lv["n"][r].sum(axis=1) >= (min_n if k else 1))
            w = np.flatnonzero(todo)[hit]
            K[w], R[w] = k, r[hit]
        return K, R


def _empty_level(Y: int) -> dict:
    return {"codes": np.zeros(0, np.int64), "n": np.zeros((0, Y), np.int32), "up": np.zeros((0, Y), np.int32),
            "s1": np.zeros(0), "s2": np.zeros(0), "hist": np.zeros((0, len(RET_Z) + 1), np.int32),
            "dip": np.zeros((0, len(LADDER)), np.int32), "rise": np.zeros((0, len(LADDER)), np.int32)}


def build_table(horizon: str, bins: np.ndarray, years: np.ndarray, ret, dip, rise) -> Table:
    """The record of every combination of the first k conditions, k = 0..8 (moves in typical moves)."""
    ys = np.unique(years)
    Y = len(ys)
    yi = np.searchsorted(ys, years)
    hb = np.searchsorted(RET_Z, ret, side="right")
    B = len(RET_Z) + 1
    up = (ret > 0).astype(np.float64)
    dip_hit = -dip[:, None] >= LADDER[None, :]
    rise_hit = rise[:, None] >= LADDER[None, :]
    levels = []
    for k in range(len(FEATURES) + 1):
        codes, inv = np.unique(_codes(bins, k), return_inverse=True)
        K = len(codes)
        if not K:
            levels.append(_empty_level(Y))
            continue
        lv = {"codes": codes,
              "n": np.bincount(inv * Y + yi, minlength=K * Y).reshape(K, Y).astype(np.int32),
              "up": np.bincount(inv * Y + yi, weights=up, minlength=K * Y).reshape(K, Y).astype(np.int32),
              "s1": np.bincount(inv, weights=ret, minlength=K), "s2": np.bincount(inv, weights=ret * ret, minlength=K),
              "hist": np.bincount(inv * B + hb, minlength=K * B).reshape(K, B).astype(np.int32)}
        for name, hits in (("dip", dip_hit), ("rise", rise_hit)):
            m = np.zeros((K, len(LADDER)), dtype=np.int32)
            for j in range(len(LADDER)):
                m[:, j] = np.bincount(inv, weights=hits[:, j], minlength=K)
            lv[name] = m
        levels.append(lv)
    big_up, big_dn = (ret > BIG).astype(np.float64), (ret < -BIG).astype(np.float64)
    drivers = {}
    for f, name in enumerate(FEATURES):
        w = WIDTH[name]
        b = bins[:, f].astype(np.int64)
        drivers[name] = np.stack([np.bincount(b, minlength=w), np.bincount(b, weights=big_up, minlength=w),
                                  np.bincount(b, weights=big_dn, minlength=w)]).astype(np.int64)
    return Table(horizon, ys, levels, drivers)


def merge_tables(a: Table | None, b: Table) -> Table:
    """Two record sets of one horizon added together."""
    if a is None:
        return b
    years = np.union1d(a.years, b.years)
    levels = []
    for la, lb in zip(a.levels, b.levels):
        codes = np.union1d(la["codes"], lb["codes"])
        out = {"codes": codes}
        for name in ("n", "up"):
            m = np.zeros((len(codes), len(years)), dtype=np.int32)
            for lv, ys in ((la, a.years), (lb, b.years)):
                if len(lv["codes"]):
                    m[np.ix_(np.searchsorted(codes, lv["codes"]), np.searchsorted(years, ys))] += lv[name]
            out[name] = m
        for name in ("s1", "s2", "hist", "dip", "rise"):
            m = np.zeros((len(codes),) + la[name].shape[1:], dtype=la[name].dtype)
            for lv in (la, lb):
                if len(lv["codes"]):
                    m[np.searchsorted(codes, lv["codes"])] += lv[name]
            out[name] = m
        levels.append(out)
    drivers = {f: a.drivers[f] + b.drivers[f] for f in a.drivers}
    return Table(a.horizon, years, levels, drivers)


def prune(t: Table, min_n: int = MIN_N) -> Table:
    """Drop the rows no lookup can use (fewer than min_n cases), below the top level."""
    levels = [t.levels[0]]
    for lv in t.levels[1:]:
        keep = lv["n"].sum(axis=1) >= min_n
        levels.append({k: v[keep] for k, v in lv.items()})
    return Table(t.horizon, t.years, levels, t.drivers)


# ── reading a table ──────────────────────────────────────────────────────────
def held_up(n: np.ndarray, up: np.ndarray, base_n: np.ndarray, base_up: np.ndarray) -> dict:
    """For each year after the first: the lean (higher or lower more often than the plain rate) from the years
    before it, against that year's own difference from its plain rate, pooled in the lean's direction."""
    num, w, var, tested, agree = 0.0, 0, 0.0, 0, 0
    for y in range(1, len(n)):
        tn, bn = int(n[:y].sum()), int(base_n[:y].sum())
        if tn < MIN_N or n[y] < 5 or not bn or not base_n[y]:
            continue
        lean = np.sign(up[:y].sum() / tn - base_up[:y].sum() / bn)
        if not lean:
            continue
        p = base_up[y] / base_n[y]
        d = (up[y] / n[y] - p) * lean
        num += d * n[y]
        w += int(n[y])
        var += p * (1 - p) * n[y]
        tested += 1
        agree += int(d > 0)
    if not tested or not w or var <= 0:
        return {"tested": 0, "held": False}
    edge = num / w
    z = edge / (math.sqrt(var) / w)
    return {"tested": tested, "agree": agree, "edge": float(edge), "z": float(z),
            "held": bool(z >= 2 and agree * 2 > tested)}


def estimate(t: Table, bins: np.ndarray, m_up: float = M_UP, m_size: float = M_SIZE) -> dict:
    """Like-now estimates for each case's conditions: each level's rates pulled toward the level above it (by m_up
    cases for finishing higher, m_size for the size of the move), from every case down to the most specific level
    with MIN_N cases. A thin cell can't swing far from what its broader cell says, so noise isn't read as a lean."""
    bins = np.atleast_2d(np.asarray(bins, dtype=np.int16))
    K, R = t.rows(bins)
    N = len(bins)
    base = t.levels[0]
    n0 = float(base["n"][0].sum()) if len(base["codes"]) else 0.0
    if not n0:
        return {"k": K, "row": R, "up": np.full(N, np.nan)}
    up = np.full(N, base["up"][0].sum() / n0)
    mean = np.full(N, base["s1"][0] / n0)
    dist = {name: np.tile(base[name][0] / n0, (N, 1)) for name in ("dip", "rise", "hist")}
    for k in range(1, len(FEATURES) + 1):
        act = np.flatnonzero(K >= k)
        if not len(act):
            break
        lv = t.levels[k]
        code = _codes(bins[act], k)
        r = np.minimum(np.searchsorted(lv["codes"], code), max(len(lv["codes"]) - 1, 0))
        hit = lv["codes"][r] == code
        idx, rr = act[hit], r[hit]
        n = lv["n"][rr].sum(axis=1).astype(np.float64)
        up[idx] = (lv["up"][rr].sum(axis=1) + m_up * up[idx]) / (n + m_up)
        mean[idx] = (lv["s1"][rr] + m_size * mean[idx]) / (n + m_size)
        for name, d in dist.items():
            d[idx] = (lv[name][rr] + m_size * d[idx]) / (n + m_size)[:, None]
    return {"k": K, "row": R, "up": up, "mean": mean, **dist}


def past(S_: np.ndarray, z: float) -> float:
    """From the shares that went at least each ladder distance: the share for distance z (between the steps,
    read on a log scale)."""
    if z <= LADDER[0]:
        return float(1 - (1 - S_[0]) * z / LADDER[0])
    if z >= LADDER[-1]:
        return float(S_[-1])
    j = int(np.searchsorted(LADDER, z)) - 1
    a, b = S_[j], S_[j + 1]
    f = (z - LADDER[j]) / (LADDER[j + 1] - LADDER[j])
    if a > 0 and b > 0:
        return float(math.exp(math.log(a) + f * (math.log(b) - math.log(a))))
    return float(a + f * (b - a))


def quantile(h: np.ndarray, q: float) -> float:
    """A quantile of the move (typical moves), from its spread over RET_Z's bins."""
    h = np.asarray(h, dtype=np.float64)
    tot = h.sum()
    if not tot:
        return float("nan")
    cum = np.cumsum(h) / tot
    i = int(np.searchsorted(cum, q))
    if i == 0:
        return float(RET_Z[0])
    if i >= len(RET_Z):
        return float(RET_Z[-1])
    f = (q - cum[i - 1]) / (h[i] / tot) if h[i] else 0.5
    return float(RET_Z[i - 1] + f * (RET_Z[i] - RET_Z[i - 1]))


def read(t: Table, bins, scale_bps: float) -> dict:
    """What happened next, like now: the cases and how many finished higher (as counted), the estimated chance of
    higher against the plain rate, the move's spread, dips and rises (bps, sized by today's unit scale_bps), the
    conditions kept and whether the lean held up on later years."""
    e = estimate(t, bins)
    k, row = int(e["k"][0]), int(e["row"][0])
    if k < 0:
        return {"horizon": t.horizon, "n": 0}
    lv, base = t.levels[k], t.levels[0]
    n_y, up_y = lv["n"][row], lv["up"][row]
    n, up = int(n_y.sum()), int(up_y.sum())
    bn, bu = int(base["n"][0].sum()), int(base["up"][0].sum())
    zsd = math.sqrt(max(base["s2"][0] / bn - (base["s1"][0] / bn) ** 2, 1e-12)) if bn else 1.0
    typical = zsd * scale_bps
    b_dist = {name: base[name][0] / bn for name in ("dip", "rise", "hist")}

    def side(name):
        return [{"bps": m * typical, "chance": past(e[name][0], m * zsd), "base": past(b_dist[name], m * zsd)}
                for m in SHOW]

    def cum(h):
        return (np.cumsum(h)[:-1] / max(h.sum(), 1e-12)).round(4).tolist()
    wf = held_up(n_y, up_y, base["n"][0], base["up"][0]) if k else {"tested": 0, "held": False}
    return {"horizon": t.horizon, "n": n, "up": up, "p_up": float(e["up"][0]),
            "base_n": bn, "base_up": bu, "base_p_up": bu / bn if bn else None,
            "typical_bps": typical, "mean_bps": float(e["mean"][0] * scale_bps),
            "q10_bps": quantile(e["hist"][0], 0.1) * scale_bps, "q50_bps": quantile(e["hist"][0], 0.5) * scale_bps,
            "q90_bps": quantile(e["hist"][0], 0.9) * scale_bps,
            "dips": side("dip"), "rises": side("rise"),
            "curve": {"bps": (RET_Z * scale_bps).round(2).tolist(), "now": cum(e["hist"][0]), "all": cum(b_dist["hist"])},
            "kept": list(FEATURES[:k]), "dropped": list(FEATURES[k:]),
            "years": [int(y) for y in t.years], "by_year": [[int(a), int(b)] for a, b in zip(n_y, up_y)],
            "held_up": wf}


def chance_past(t: Table, bins, dist_bps: float, scale_bps: float, side: int = 1) -> dict | None:
    """For the Hold Clock: how often, like now, price went dist_bps against a long (side 1: a dip) or a short
    (side -1: a rise) within the horizon, and how often in general."""
    e = estimate(t, bins)
    if e["k"][0] < 0 or not scale_bps > 0:
        return None
    base = t.levels[0]
    name = "dip" if side > 0 else "rise"
    z = dist_bps / scale_bps
    lv = t.levels[int(e["k"][0])]
    return {"chance": past(e[name][0], z), "base": past(base[name][0] / base["n"][0].sum(), z),
            "n": int(lv["n"][int(e["row"][0])].sum()), "kept": list(FEATURES[:int(e["k"][0])])}


def drivers(t: Table, top: int = 4, min_n: int = 200) -> dict:
    """The condition bins that came before big moves (beyond 1.5 typical moves) most often, as a multiple of usual."""
    out = {"up": [], "down": []}
    total = int(t.drivers[FEATURES[0]][0].sum())
    for side, j in (("up", 1), ("down", 2)):
        big = int(t.drivers[FEATURES[0]][j].sum())
        if not total or big < 20:
            continue
        rows = []
        for f in FEATURES:
            d = t.drivers[f]
            for b in range(NBINS[f]):
                if d[0, b] < min_n or d[j, b] < 10:
                    continue
                rows.append({"feature": f, "bin": LABELS[f][b], "lift": float((d[j, b] / big) / (d[0, b] / total)),
                             "n": int(d[j, b]), "of": int(d[0, b])})
        out[side] = sorted(rows, key=lambda r: -r["lift"])[:top]
    return out


# ── learning year by year, each year checked on the years before it ─────────
class Learner:
    """Builds one horizon's table from chunks in time order. Before a year's cases are added, they're scored on the
    table as it stood at the end of the year before."""

    def __init__(self, horizon: str):
        self.horizon = horizon
        self.table: Table | None = None
        self.before: Table | None = None
        self.year: int | None = None
        self.n = 0
        self.up = np.zeros(2)                           # Brier sums: like now, plain rate
        self.dip = np.zeros((2, len(LADDER)))
        self.rise = np.zeros((2, len(LADDER)))
        self.seen_dip = np.zeros(len(LADDER))           # how often each distance was passed, in the scored cases
        self.seen_rise = np.zeros(len(LADDER))

    def add(self, bins: np.ndarray, t_s: np.ndarray, ret, dip, rise) -> None:
        if not len(t_s):
            return
        years = years_of(t_s)
        for y in np.unique(years):
            sel = years == y
            if self.year != int(y):
                self.before, self.year = self.table, int(y)
            if self.before is not None:
                self._score(bins[sel], ret[sel], dip[sel], rise[sel])
            self.table = merge_tables(self.table, build_table(self.horizon, bins[sel], years[sel], ret[sel],
                                                              dip[sel], rise[sel]))

    def _score(self, bins, ret, dip, rise) -> None:
        t = self.before
        e = estimate(t, bins)
        g = np.isfinite(e["up"]) & (e["k"] >= 0)
        if not g.any():
            return
        base = t.levels[0]
        bn = base["n"][0].sum()
        up = (ret[g] > 0).astype(np.float64)
        b_up = base["up"][0].sum() / bn
        self.n += int(g.sum())
        self.up += [((e["up"][g] - up) ** 2).sum(), ((b_up - up) ** 2).sum()]
        for name, hits, acc, seen in (("dip", -dip[g, None] >= LADDER, self.dip, self.seen_dip),
                                      ("rise", rise[g, None] >= LADDER, self.rise, self.seen_rise)):
            b = base[name][0] / bn
            acc[0] += ((e[name][g] - hits) ** 2).sum(axis=0)
            acc[1] += ((b[None, :] - hits) ** 2).sum(axis=0)
            seen += hits.sum(axis=0)

    def check(self) -> dict:
        """Skill on later years: 1 - Brier(like now) / Brier(plain rate), for finishing higher and for dips and rises
        past each distance whose rate was between 2% and 98%."""
        if not self.n:
            return {"n": 0}

        def skill(acc, seen):
            rate = seen / self.n
            use = (rate > 0.02) & (rate < 0.98) & (acc[1] > 0)
            if not use.any():
                return None, []
            per = [{"z": float(z), "rate": float(r), "skill": float(1 - a / b)}
                   for z, r, a, b, u in zip(LADDER, rate, acc[0], acc[1], use) if u]
            return float(1 - acc[0][use].sum() / acc[1][use].sum()), per
        d, dp = skill(self.dip, self.seen_dip)
        r, rp = skill(self.rise, self.seen_rise)
        return {"n": self.n, "up": float(1 - self.up[0] / self.up[1]) if self.up[1] else None,
                "dip": d, "rise": r, "dip_by": dp, "rise_by": rp}


# ── saving ───────────────────────────────────────────────────────────────────
def save_tables(path, tables: dict) -> None:
    """{horizon: Table} into one .npz."""
    arrays = {}
    for h, t in tables.items():
        arrays[f"{h}/years"] = t.years
        for k, lv in enumerate(t.levels):
            for name, a in lv.items():
                arrays[f"{h}/L{k}/{name}"] = a
        for f, d in t.drivers.items():
            arrays[f"{h}/D/{f}"] = d
    tmp = str(path) + ".tmp.npz"
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, path)


def load_tables(path) -> dict:
    with np.load(path) as z:
        data = {n: z[n] for n in z.files}
    out = {}
    for h in sorted({n.split("/", 1)[0] for n in data}):
        levels = []
        for k in range(len(FEATURES) + 1):
            pre = f"{h}/L{k}/"
            levels.append({n[len(pre):]: v for n, v in data.items() if n.startswith(pre)})
        drv = {f: data[f"{h}/D/{f}"] for f in FEATURES if f"{h}/D/{f}" in data}
        out[h] = Table(h, data[f"{h}/years"], levels, drv)
    return out


def save_json(path, obj) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)

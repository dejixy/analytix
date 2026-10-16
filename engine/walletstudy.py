"""
Wallet study: are some Hyperliquid wallets consistently early, and does following them pay?

Hyperliquid's trade feed names both wallets on every trade, and the recordings keep them. The aggressor (taker)
is the one whose order moved: their trades are the ones that carry intent.

    episodes     a wallet's taker trades on one coin, one side, with gaps under MERGE_S, are one decision (a sweep
                 across levels, a burst, a TWAP's slices). Scoring decisions, not fills, keeps the counts honest
    markout      how far the mid moved their way from the second after the decision started to h seconds later, in
                 bps. Measured from after their own trade, so it's what's left for someone who follows
    skill        each wallet's average markout and its t-stat, from the earlier part of the data only (train)
    persistence  does a wallet's train score predict its score in the later part (test)? If skill doesn't carry
                 over, no list of wallets can be followed profitably
    follow       in the test part only: copy the chosen wallets' decisions LAG seconds later, hold h seconds; the
                 move at the mid, after crossing the spread, and on Papertrade (cut on wins, bust at the leverage)
"""
from dataclasses import dataclass, field

import numpy as np

from engine.holdclock import PAPERTRADE, PT_DEADBAND, PT_WIN_FEE, Grid
from engine.survivalstudy import block_t, future_extreme

MERGE_S = 30
HORIZONS = (10, 30, 60, 300)
LAGS = (1, 2, 3, 4, 5)
LEVERAGES = (200, 500, 841)
LEAD_WINDOW_S = 3
LEAD_GAP_BPS = 2.0


@dataclass(slots=True)
class Episode:
    coin: str
    wallet: str
    t_ms: int
    side: int            # +1 bought (lifted the ask), -1 sold (hit the bid)
    usd: float
    fills: int = 1


@dataclass
class Episodes:
    """Builds episodes from trades as they stream in."""
    merge_ms: int = MERGE_S * 1000
    done: list = field(default_factory=list)
    open: dict = field(default_factory=dict)          # (coin, wallet) → [Episode, last_ms]
    trades: int = 0

    def add(self, coin: str, wallet: str, t_ms: int, side: int, usd: float) -> None:
        self.trades += 1
        key = (coin, wallet)
        cur = self.open.get(key)
        if cur is not None:
            ep, last = cur
            if ep.side == side and t_ms - last <= self.merge_ms:
                ep.usd += usd
                ep.fills += 1
                cur[1] = t_ms
                return
            self.done.append(ep)
        self.open[key] = [Episode(coin, wallet, t_ms, side, usd), t_ms]

    def finish(self) -> list[Episode]:
        self.done.extend(ep for ep, _ in self.open.values())
        self.open.clear()
        self.done.sort(key=lambda e: e.t_ms)
        return self.done


def taker_of(trade: dict):
    """(wallet, side) of the aggressor in one Hyperliquid trade, or None."""
    users = trade.get("users") or []
    side = trade.get("side")
    if len(users) != 2 or side not in ("B", "A"):
        return None
    return (users[0], 1) if side == "B" else (users[1], -1)


def kept(move: np.ndarray, coin: str) -> np.ndarray:
    """engine/holdclock.papertrade_kept for many moves at once (fraction of notional a winning close pays)."""
    p = PAPERTRADE.get(coin, PAPERTRADE["BTC"])
    m = np.maximum(move - PT_DEADBAND, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        k = m * (1 - p["base_rate"]) / (1 + 1 / (m * p["rate_mult"]) + p["ref_notional"] / (1e6 * m * p["pos_mult"]))
    return np.where(m > 0, k * (1 - PT_WIN_FEE), 0.0)


class Marks:
    """Per-episode moves, computed against one coin's one-second grid."""

    def __init__(self, g: Grid):
        self.g = g
        self.t0 = int(g.t[0]) if len(g) else 0
        self.bad = np.concatenate([[0], np.cumsum(~g.ok)])
        self._worst: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def worst(self, h: int) -> tuple[np.ndarray, np.ndarray]:
        """Lowest best bid and highest best ask over the next h seconds, for every second."""
        if h not in self._worst:
            self._worst[h] = (future_extreme(self.g.bid, h, np.min), future_extreme(self.g.ask, h, np.max))
        return self._worst[h]

    def _clean(self, i: np.ndarray, j: np.ndarray) -> np.ndarray:
        n = len(self.g)
        inside = (i >= 0) & (j < n)
        out = np.zeros(len(i), dtype=bool)
        k = np.nonzero(inside)[0]
        out[k] = (self.bad[j[k] + 1] - self.bad[i[k]]) == 0          # no feed gap from i to j
        return out

    def markout(self, t_ms: np.ndarray, side: np.ndarray, h: int) -> np.ndarray:
        """bps the mid moved the wallet's way from the second after the trade to h seconds later (nan if unknown)."""
        i = t_ms // 1000 - self.t0 + 1
        j = i + h
        ok = self._clean(i, j)
        out = np.full(len(i), np.nan)
        m = self.g.mid
        out[ok] = side[ok] * (m[j[ok]] / m[i[ok]] - 1) * 1e4
        return out

    def follow(self, t_ms: np.ndarray, side: np.ndarray, lag: int, h: int, leverages=LEVERAGES, coin="BTC") -> dict:
        """Copy each decision `lag` seconds later and hold h seconds: the move at the mid, after crossing the
        spread (in at the worst ask/bid of that second, out at the worst bid/ask), and per leverage the Papertrade
        result as a fraction of margin (entry at the mid, bust on the best bid/ask, cut on wins)."""
        g = self.g
        e = t_ms // 1000 - self.t0 + lag
        x = e + h
        ok = self._clean(e, x)
        k = np.nonzero(ok)[0]
        s = side[k]
        ee, xx = e[k], x[k]
        mid = s * (g.mid[xx] / g.mid[ee] - 1)
        entry = np.where(s > 0, g.ask[ee], g.bid[ee])
        exit_ = np.where(s > 0, g.bid[xx], g.ask[xx])
        spread = s * (exit_ / entry - 1)
        lo, hi = self.worst(h)
        ok_w = ee < len(lo)
        pt = {}
        for lev in leverages:
            dist = 1 / lev
            bust = np.ones(len(k), dtype=bool)
            w = ok_w
            bust[w] = np.where(s[w] > 0, lo[ee[w]] <= g.mid[ee[w]] * (1 - dist), hi[ee[w]] >= g.mid[ee[w]] * (1 + dist))
            pay = np.where(mid > 0, kept(mid, coin) * lev, np.maximum(mid * lev, -1.0))
            pt[lev] = np.where(bust, -1.0, pay)
        return {"idx": k, "mid": mid * 1e4, "spread": spread * 1e4, "pt": pt}


def wallet_stats(eps: list[Episode], marks: dict[str, Marks], h: int) -> dict[str, dict]:
    """wallet → {n, mean, t, usd, coins} from its episodes' markouts at h."""
    by: dict[str, list] = {}
    for coin in {e.coin for e in eps}:
        mk = marks.get(coin)
        sub = [e for e in eps if e.coin == coin]
        if mk is None or not sub:
            continue
        mo = mk.markout(np.array([e.t_ms for e in sub]), np.array([e.side for e in sub]), h)
        for e, v in zip(sub, mo):
            if np.isfinite(v):
                by.setdefault(e.wallet, []).append((v, e.usd, coin))
    out = {}
    for w, rows in by.items():
        v = np.array([r[0] for r in rows])
        n = len(v)
        sd = v.std(ddof=1) if n > 1 else 0.0
        out[w] = {"n": n, "mean": float(v.mean()), "t": float(v.mean() / (sd / np.sqrt(n))) if n > 1 and sd > 0 else 0.0,
                  "usd": float(np.median([r[1] for r in rows])),
                  "coins": sorted({r[2] for r in rows}, key=lambda c: -sum(1 for r in rows if r[2] == c))}
    return out


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 5:
        return None
    ra, rb = a.argsort().argsort(), b.argsort().argsort()
    return float(np.corrcoef(ra, rb)[0, 1])


def follow_table(eps: list[Episode], marks: dict[str, Marks], flip: bool = False, lags=LAGS, horizons=HORIZONS,
                 leverages=LEVERAGES) -> dict:
    """(lag, h) → summary of copying (or, with flip, fading) these episodes."""
    out = {}
    for lag in lags:
        for h in horizons:
            mids, spreads, pts, ts = [], [], {lev: [] for lev in leverages}, []
            for coin in {e.coin for e in eps}:
                mk = marks.get(coin)
                sub = [e for e in eps if e.coin == coin]
                if mk is None or not sub:
                    continue
                t = np.array([e.t_ms for e in sub])
                s = np.array([e.side for e in sub]) * (-1 if flip else 1)
                r = mk.follow(t, s, lag, h, leverages, coin)
                mids.append(r["mid"])
                spreads.append(r["spread"])
                ts.append(t[r["idx"]] // 1000)
                for lev in leverages:
                    pts[lev].append(r["pt"][lev])
            if not mids or not sum(len(m) for m in mids):
                out[(lag, h)] = None
                continue
            m, sp, tt = np.concatenate(mids), np.concatenate(spreads), np.concatenate(ts)
            out[(lag, h)] = {"n": len(m), "mid": float(m.mean()), "mid_t": block_t(tt, m),
                             "spread": float(sp.mean()), "win": float((m > 0).mean()),
                             "pt": {lev: float(np.concatenate(pts[lev]).mean()) for lev in leverages}}
    return out


def last_per_second(rows: dict[int, float]) -> tuple[int, np.ndarray]:
    """{second: value} → (first second, array with every second filled from the one before)."""
    if not rows:
        return 0, np.zeros(0)
    ts = np.array(sorted(rows))
    t0 = int(ts[0])
    out = np.full(int(ts[-1]) - t0 + 1, np.nan)
    out[ts - t0] = [rows[int(t)] for t in ts]
    idx = np.where(np.isnan(out), 0, np.arange(len(out)))
    np.maximum.accumulate(idx, out=idx)
    return t0, out[idx]


def lead_gap(t_ms: np.ndarray, side: np.ndarray, hl: Grid, b_t0: int, bmid: np.ndarray,
             window: int = LEAD_WINDOW_S) -> np.ndarray:
    """How far Binance moved the decision's way over the `window` seconds up to the trade, minus how far Hyperliquid
    did (bps). Positive = Binance had already moved their way and Hyperliquid hadn't caught up: the wallet may just
    be trading the Binance lead. nan where either feed is missing."""
    s = t_ms // 1000
    out = np.full(len(s), np.nan)
    if not len(bmid) or not len(hl):
        return out
    bi, hi = s - b_t0, s - int(hl.t[0])
    ok = (bi - window >= 0) & (bi < len(bmid)) & (hi - window >= 0) & (hi < len(hl))
    k = np.nonzero(ok)[0]
    b = bmid[bi[k]] / bmid[bi[k] - window] - 1
    h = hl.mid[hi[k]] / hl.mid[hi[k] - window] - 1
    out[k] = side[k] * (b - h) * 1e4
    return out

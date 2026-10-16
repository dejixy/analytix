"""
Big movers: the last checks before following the wallets whose trades are followed by big moves.

    walk-forward  each test day, wallets are scored only on the days before it (as a live list refreshed daily
                  would be), and the ones that qualify are followed that day. No day is judged by a list that saw it
    alerts        big-mover decisions on one coin and side within MERGE_S of each other are one alert: three of
                  them buying ETH together is one trade for you, not three
    one at a time a person can hold one trade: an alert that comes while you're still in the last one is skipped
    bankroll      the day-by-day result of taking those trades with a given balance, leverage and share of the
                  balance per trade, and a bootstrap of a week from the per-trade results
"""
from dataclasses import dataclass

import numpy as np

from engine.walletstudy import Episode, Marks

DAY_MS = 86_400_000
MERGE_S = 30


@dataclass
class Arrays:
    """Every decision as parallel arrays, with wallets and coins as small ints."""
    t: np.ndarray
    side: np.ndarray
    usd: np.ndarray
    wallet: np.ndarray
    coin: np.ndarray
    wallets: list
    coins: list

    @classmethod
    def of(cls, eps: list[Episode]) -> "Arrays":
        wl, cl = {}, {}
        w = np.array([wl.setdefault(e.wallet, len(wl)) for e in eps], dtype=np.int64)
        c = np.array([cl.setdefault(e.coin, len(cl)) for e in eps], dtype=np.int64)
        return cls(np.array([e.t_ms for e in eps], dtype=np.int64), np.array([e.side for e in eps], dtype=np.int64),
                   np.array([e.usd for e in eps]), w, c, list(wl), list(cl))


def markouts(a: Arrays, marks: dict[str, Marks], h: int) -> np.ndarray:
    """Each decision's move its way over h seconds from the second after it (bps; nan where unknown)."""
    out = np.full(len(a.t), np.nan)
    for ci, coin in enumerate(a.coins):
        mk = marks.get(coin)
        k = np.nonzero(a.coin == ci)[0]
        if mk is None or not len(k):
            continue
        out[k] = mk.markout(a.t[k], a.side[k], h)
    return out


def scores(a: Arrays, mo: np.ndarray, until_ms: int, h: int, since_ms: int | None = None) -> dict:
    """Per wallet (n, mean, t) from the decisions whose whole markout window ended before until_ms (no peeking)."""
    use = np.isfinite(mo) & (a.t + (h + 1) * 1000 <= until_ms)
    if since_ms is not None:
        use &= a.t >= since_ms
    w, v = a.wallet[use], mo[use]
    nw = len(a.wallets)
    n = np.bincount(w, minlength=nw).astype(float)
    s = np.bincount(w, weights=v, minlength=nw)
    ss = np.bincount(w, weights=v * v, minlength=nw)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = s / n
        var = (ss - n * mean * mean) / (n - 1)
        t = mean / np.sqrt(var / n)
    return {"n": n, "mean": np.nan_to_num(mean), "t": np.nan_to_num(t)}


def pick(sc: dict, min_n: int, t_min: float, cutoff_bps: float) -> np.ndarray:
    """Wallet ids that qualify as big movers."""
    return np.nonzero((sc["n"] >= min_n) & (sc["t"] >= t_min) & (sc["mean"] >= cutoff_bps))[0]


@dataclass(slots=True)
class Alert:
    coin: str
    t_ms: int
    side: int
    trigger: int              # wallet id of the first decision
    count: int = 1            # decisions merged into it


def alerts(a: Arrays, idx: np.ndarray, merge_s: int = MERGE_S) -> list[Alert]:
    """Decisions (by index, any order) → alerts: same coin and side within merge_s of the alert's last decision
    joins it."""
    out: list[Alert] = []
    last: dict[tuple, tuple[Alert, int]] = {}
    for i in idx[np.argsort(a.t[idx], kind="stable")]:
        coin, side, t = a.coins[a.coin[i]], int(a.side[i]), int(a.t[i])
        key = (coin, side)
        cur = last.get(key)
        if cur is not None and t - cur[1] <= merge_s * 1000:
            cur[0].count += 1
            last[key] = (cur[0], t)
            continue
        al = Alert(coin, t, side, int(a.wallet[i]))
        out.append(al)
        last[key] = (al, t)
    out.sort(key=lambda x: x.t_ms)
    return out


def price(als: list[Alert], marks: dict[str, Marks], lag: int, hold: int, leverages) -> dict:
    """Per alert: move at the mid (bps) and Papertrade result per leverage (fraction of margin), nan where the feed
    had a gap. Arrays are in the order of `als`."""
    n = len(als)
    mid = np.full(n, np.nan)
    pt = {lev: np.full(n, np.nan) for lev in leverages}
    for coin in {x.coin for x in als}:
        k = np.array([i for i, x in enumerate(als) if x.coin == coin])
        mk = marks.get(coin)
        if mk is None or not len(k):
            continue
        r = mk.follow(np.array([als[i].t_ms for i in k]), np.array([als[i].side for i in k]), lag, hold, leverages, coin)
        kk = k[r["idx"]]
        mid[kk] = r["mid"]
        for lev in leverages:
            pt[lev][kk] = r["pt"][lev]
    return {"mid": mid, "pt": pt}


def one_at_a_time(als: list[Alert], ok: np.ndarray, lag: int, hold: int, cap: int | None = None) -> np.ndarray:
    """Indices of the alerts a person holding one trade at a time would take (in time order), skipping alerts
    that arrive while the last trade is open and alerts that couldn't be priced. cap = most trades per day."""
    taken, free_at, per_day = [], -1, {}
    for i, x in enumerate(als):
        if not ok[i] or x.t_ms < free_at:
            continue
        day = x.t_ms // DAY_MS
        if cap is not None and per_day.get(day, 0) >= cap:
            continue
        taken.append(i)
        per_day[day] = per_day.get(day, 0) + 1
        free_at = x.t_ms + (lag + hold) * 1000
    return np.array(taken, dtype=np.int64)


def bankroll(returns: np.ndarray, start: float, size: float, compound: bool) -> dict:
    """Play the per-trade results (fraction of margin) in order. Margin = size × start (fixed) or size × current
    balance (compound); stops when the balance can't cover a trade."""
    bal, low, n = start, start, 0
    for r in returns:
        margin = size * (bal if compound else start)
        if margin > bal or bal <= 0.01:
            break
        bal += margin * r
        low = min(low, bal)
        n += 1
    return {"end": bal, "low": low, "trades": n, "bust": n < len(returns)}


def bootstrap(returns: np.ndarray, start: float, size: float, compound: bool, trades: int, runs: int = 5000,
              seed: int = 7, chunk: int = 500) -> dict:
    """Many made-up sequences of `trades` trades drawn from the real per-trade results: the spread of outcomes.
    Same rules as bankroll(), done for all runs at once."""
    if not len(returns) or trades <= 0:
        return {}
    rng = np.random.default_rng(seed)
    ends, busts = [], 0
    for k0 in range(0, runs, chunk):
        r = rng.choice(returns, (min(chunk, runs - k0), trades))
        if compound:
            path = start * np.cumprod(1 + size * r, axis=1)
            before = np.concatenate([np.full((len(r), 1), start), path[:, :-1]], axis=1)
            dead = before <= 0.01
        else:
            margin = size * start
            path = start + margin * np.cumsum(r, axis=1)
            before = np.concatenate([np.full((len(r), 1), start), path[:, :-1]], axis=1)
            dead = before < margin
        first = np.where(dead.any(axis=1), dead.argmax(axis=1), -1)
        end = np.where(first >= 0, before[np.arange(len(r)), np.maximum(first, 0)], path[:, -1])
        ends.append(end)
        busts += int((first >= 0).sum())
    ends = np.concatenate(ends)
    return {"p10": float(np.percentile(ends, 10)), "median": float(np.median(ends)), "p90": float(np.percentile(ends, 90)),
            "up": float((ends > start).mean()), "bust": busts / runs}

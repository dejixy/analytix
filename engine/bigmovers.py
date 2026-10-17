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
from array import array
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


def settle(rows: dict[int, list], s0: int, lag: int, hold: int, side: int, stale_s: int = 5) -> dict | None:
    """One alert's result from a coin's per-second rows {second: [lowest best bid, highest best ask, last mid]}:
    in at the mid `lag` seconds after second s0, out at the mid `hold` seconds later.
    → {"bps": the move your way, "adv": the furthest the best bid (long) or ask (short) went against you, in bps},
    or None when the feed had a gap of more than stale_s seconds in that span."""
    e, x = s0 + lag, s0 + lag + hold
    last_s, row = None, None
    for s in range(e - stale_s, e + 1):
        if s in rows:
            last_s, row = s, rows[s]
    if row is None:
        return None
    entry = row[2]
    lo, hi = float("inf"), float("-inf")
    exit_ = entry
    for s in range(e + 1, x + 1):
        r = rows.get(s)
        if r is None:
            if s - last_s > stale_s:
                return None
            continue
        last_s = s
        lo, hi, exit_ = min(lo, r[0]), max(hi, r[1]), r[2]
    if not entry:
        return None
    worst = (entry - lo) / entry if side > 0 else (hi - entry) / entry
    return {"bps": side * (exit_ / entry - 1) * 1e4, "adv": max(worst, 0.0) * 1e4 if np.isfinite(worst) else 0.0}


def pt_of(r: dict | None, leverage: float, coin: str) -> float | None:
    """A settled result on Papertrade at this leverage, as a fraction of margin: a bust (the best bid/ask reached
    1/leverage away) loses it all; a win pays after Papertrade's cut; a loss is the raw move."""
    if not r:
        return None
    from engine.holdclock import papertrade_kept
    if r["adv"] >= 1e4 / leverage:
        return -1.0
    move = r["bps"] / 1e4
    return papertrade_kept(move, coin) * leverage if move > 0 else max(move * leverage, -1.0)


# ── v4: honest timing ──────────────────────────────────────────────────────────────────────────────────────────
# The v3 test timed each decision from its first fill but only kept decisions that went on to reach $1,000, so it
# acted on knowledge it couldn't have had yet. A decision can only be acted on when something about it is known:
# its first order (all the fills at its first timestamp: one market order sweeping levels), or the moment its
# running total reaches the size threshold (what the live bot waits for).

@dataclass(slots=True)
class Decision:
    coin: str
    wallet: str
    side: int
    t_first: int              # its first fill (ms)
    usd_first: float          # its first order: the fills at t_first
    t_cross: int              # when its running total reached min_usd (-1 = never)
    usd: float                # its final total


class Decisions:
    """Taker fills → decisions (same coin, wallet and side, gaps of MERGE_S or less, as the live bot merges them),
    keeping both moments above. Keeps decisions that reached min_usd or whose first order was keep_first_usd+.
    Finished decisions are stored as plain numbers (a week on four coins is millions of them)."""

    def __init__(self, min_usd: float, keep_first_usd: float):
        self.min_usd = min_usd
        self.keep_first_usd = keep_first_usd
        self.open: dict[tuple, list] = {}            # (coin, wallet) → [Decision, last ms]
        self._w: dict[str, int] = {}
        self._c: dict[str, int] = {}
        self._ints = {k: array("q") for k in ("t_first", "t_cross", "side", "wallet", "coin")}
        self._floats = {k: array("d") for k in ("usd_first", "usd")}

    def add(self, coin: str, wallet: str, t_ms: int, side: int, usd: float) -> None:
        key = (coin, wallet)
        cur = self.open.get(key)
        if cur is not None:
            d, last = cur
            if d.side == side and t_ms - last <= MERGE_S * 1000:
                d.usd += usd
                if t_ms == d.t_first:
                    d.usd_first += usd
                if d.t_cross < 0 and d.usd >= self.min_usd:
                    d.t_cross = t_ms
                cur[1] = t_ms
                return
            self._close(d)
        self.open[key] = [Decision(coin, wallet, side, t_ms, usd, t_ms if usd >= self.min_usd else -1, usd), t_ms]

    def _close(self, d: Decision) -> None:
        if d.usd < self.min_usd and d.usd_first < self.keep_first_usd:
            return
        i, fl = self._ints, self._floats
        i["t_first"].append(d.t_first)
        i["t_cross"].append(d.t_cross)
        i["side"].append(d.side)
        i["wallet"].append(self._w.setdefault(d.wallet, len(self._w)))
        i["coin"].append(self._c.setdefault(d.coin, len(self._c)))
        fl["usd_first"].append(d.usd_first)
        fl["usd"].append(d.usd)

    def finish(self) -> "DecArrays":
        for d, _ in self.open.values():
            self._close(d)
        self.open.clear()
        cols = {k: np.frombuffer(v, dtype=np.int64).copy() if len(v) else np.zeros(0, np.int64) for k, v in self._ints.items()}
        cols.update({k: np.frombuffer(v, dtype=np.float64).copy() if len(v) else np.zeros(0) for k, v in self._floats.items()})
        order = np.argsort(cols["t_first"], kind="stable")
        return DecArrays(**{k: v[order] for k, v in cols.items()}, wallets=list(self._w), coins=list(self._c))


@dataclass
class DecArrays:
    t_first: np.ndarray
    usd_first: np.ndarray
    t_cross: np.ndarray
    usd: np.ndarray
    side: np.ndarray
    wallet: np.ndarray
    coin: np.ndarray
    wallets: list
    coins: list


def score_by_wallet(wallet: np.ndarray, value: np.ndarray, ready_ms: np.ndarray, until_ms: int, n_wallets: int) -> dict:
    """Per wallet (n, mean, t) from the values that were fully known before until_ms (ready_ms <= until_ms)."""
    use = np.isfinite(value) & (ready_ms >= 0) & (ready_ms <= until_ms)
    w, v = wallet[use], value[use]
    n = np.bincount(w, minlength=n_wallets).astype(float)
    s = np.bincount(w, weights=v, minlength=n_wallets)
    ss = np.bincount(w, weights=v * v, minlength=n_wallets)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = s / n
        var = (ss - n * mean * mean) / (n - 1)
        t = mean / np.sqrt(var / n)
    return {"n": n, "mean": np.nan_to_num(mean), "t": np.nan_to_num(t)}


def merge_at(t: np.ndarray, side: np.ndarray, coin: np.ndarray, wallet: np.ndarray, idx: np.ndarray,
             coins: list, merge_s: int = MERGE_S) -> list[Alert]:
    """Decisions (by index) acted on at times t → alerts, merged like alerts() but on the acting time."""
    out: list[Alert] = []
    last: dict[tuple, tuple[Alert, int]] = {}
    for i in idx[np.argsort(t[idx], kind="stable")]:
        key = (int(coin[i]), int(side[i]))
        ti = int(t[i])
        cur = last.get(key)
        if cur is not None and ti - cur[1] <= merge_s * 1000:
            cur[0].count += 1
            last[key] = (cur[0], ti)
            continue
        al = Alert(coins[coin[i]], ti, int(side[i]), int(wallet[i]))
        out.append(al)
        last[key] = (al, ti)
    return out

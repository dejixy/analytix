"""
Low-frequency lab (hours to days): classic signals on hourly bars, traded with stops, judged walk-forward.

    signal      worked out at a bar's close from that bar and earlier ones only; +1 long, -1 short, 0 nothing
    a trade     in at the next bar's open (plus costs), one position per coin at a time. Out at the stop (ATR
                multiples from the entry), when the max hold runs out (at a bar's close), or, if asked, when the
                signal flips. If a bar gaps through the stop, out at that bar's open. Costs: Hyperliquid taker fees
                both ways plus slippage (cost_bps for the round trip), and funding for every hour held
    walk-forward  each month, the config is picked using only trades that finished before the month began, then
                that month is traded with it. Only those months count as the honest result
    sizing      risk a fixed share of the balance per trade: leverage = risk / stop distance, capped at what the
                venue allows. Weeks are resampled from the honest trades to show what a week or a month can do
"""
from dataclasses import dataclass, field

import numpy as np

HOUR_MS = 3_600_000
WEEK_MS = 7 * 24 * HOUR_MS
HL_MAX_LEV = {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10}


# ── indicators (each value uses that bar and earlier ones) ─────────────────
def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2 / (n + 1)
    out = np.empty(len(x))
    m = x[0]
    for i, v in enumerate(x):
        m = m + a * (v - m)
        out[i] = m
    return out


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    out = np.empty(len(tr))
    m = tr[0]
    for i, v in enumerate(tr):
        m = m + (v - m) / n
        out[i] = m
    return out


def rsi(c: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up, dn = np.maximum(d, 0), np.maximum(-d, 0)
    au, ad = np.empty(len(c)), np.empty(len(c))
    mu = md = 0.0
    for i in range(len(c)):
        mu += (up[i] - mu) / n
        md += (dn[i] - md) / n
        au[i], ad[i] = mu, md
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
    return np.where(ad == 0, 100.0, 100 - 100 / (1 + rs))


def prior_max(x: np.ndarray, n: int) -> np.ndarray:
    """Highest of the n bars before each bar (not counting it); nan for the first n."""
    out = np.full(len(x), np.nan)
    if len(x) > n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)[:-1]
        out[n:] = w.max(axis=1)
    return out


def prior_min(x: np.ndarray, n: int) -> np.ndarray:
    return -prior_max(-x, n)


def rolling_mean_std(x: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Mean and std of the n bars up to and including each bar; nan before that."""
    m, s = np.full(len(x), np.nan), np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        m[n - 1:] = w.mean(axis=1)
        s[n - 1:] = w.std(axis=1)
    return m, s


# ── trades ──────────────────────────────────────────────────────────────────
@dataclass
class Trades:
    coin: str
    entry_t: np.ndarray
    exit_t: np.ndarray
    side: np.ndarray
    gross: np.ndarray          # the move your way, as a fraction
    net: np.ndarray            # after costs and funding
    stop: np.ndarray           # stop distance as a fraction of the entry
    hold_h: np.ndarray
    reason: list = field(default_factory=list)
    coins: np.ndarray | None = None      # each trade's coin (pooled trades)

    def __post_init__(self):
        if self.coins is None:
            self.coins = np.array([self.coin] * len(self.net), dtype=object)

    def __len__(self) -> int:
        return len(self.net)

    def where(self, keep: np.ndarray) -> "Trades":
        return Trades(self.coin, self.entry_t[keep], self.exit_t[keep], self.side[keep], self.gross[keep],
                      self.net[keep], self.stop[keep], self.hold_h[keep],
                      [r for r, k in zip(self.reason, keep) if k], self.coins[keep])


def simulate(bars, sig: np.ndarray, stop_atr: float, max_hold: int, cost_bps: float, atr_n: int = 24,
             exit_on_flip: bool = False) -> Trades:
    """Trade a signal on one coin's bars (see the module notes)."""
    o, h, l, c, t, fund = bars.o, bars.h, bars.l, bars.c, bars.t, bars.funding
    a = atr(h, l, c, atr_n)
    n = len(c)
    rows, reasons = [], []
    i = atr_n
    cost = cost_bps / 1e4
    while i < n - 1:
        s = int(sig[i])
        if s == 0 or not np.isfinite(a[i]):
            i += 1
            continue
        e = i + 1
        px = o[e]
        dist = stop_atr * a[i]
        stop = px - s * dist
        j, out, why = e, None, "time"
        paid = 0.0
        while True:
            paid += fund[j]
            if s > 0 and l[j] <= stop:
                out, why = min(o[j], stop) if j > e else stop, "stop"
            elif s < 0 and h[j] >= stop:
                out, why = max(o[j], stop) if j > e else stop, "stop"
            elif j - e + 1 >= max_hold or j == n - 1:
                out, why = c[j], "time" if j - e + 1 >= max_hold else "end of data"
            elif exit_on_flip and int(sig[j]) == -s:
                out, why = c[j], "flip"
            if out is not None:
                break
            j += 1
        gross = s * (out / px - 1)
        rows.append((t[e], t[j] + HOUR_MS, s, gross, gross - cost - s * paid, dist / px, j - e + 1))
        reasons.append(why)
        i = j + 1 if why != "flip" else j                      # a flip can open the other way at once
    if not rows:
        z = np.zeros(0)
        return Trades(bars.coin, z.astype(np.int64), z.astype(np.int64), z.astype(np.int64), z, z, z, z, [])
    r = np.array(rows, dtype=float)
    return Trades(bars.coin, r[:, 0].astype(np.int64), r[:, 1].astype(np.int64), r[:, 2].astype(np.int64), r[:, 3],
                  r[:, 4], r[:, 5], r[:, 6], reasons)


def concat(ts: list[Trades]) -> Trades:
    ts = [x for x in ts if len(x)]
    if not ts:
        z = np.zeros(0)
        return Trades("", z.astype(np.int64), z.astype(np.int64), z.astype(np.int64), z, z, z, z, [])
    cat = lambda k: np.concatenate([getattr(x, k) for x in ts])
    order = np.argsort(cat("entry_t"), kind="stable")
    reasons = [r for x in ts for r in x.reason]
    return Trades(",".join(sorted({c for x in ts for c in x.coin.split(",")})),
                  *(cat(k)[order] for k in ("entry_t", "exit_t", "side", "gross", "net", "stop", "hold_h")),
                  [reasons[k] for k in order], cat("coins")[order])


# ── judging ─────────────────────────────────────────────────────────────────
def t_weekly(t_ms: np.ndarray, v: np.ndarray) -> float | None:
    """t-stat of the mean from weekly blocks (trades in one week aren't independent)."""
    if len(v) < 5:
        return None
    _, inv = np.unique(t_ms // WEEK_MS, return_inverse=True)
    m = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(m) < 4 or m.std(ddof=1) == 0:
        return None
    return float(v.mean() / (m.std(ddof=1) / np.sqrt(len(m))))


def summary(tr: Trades, span_ms: float) -> dict | None:
    if not len(tr):
        return None
    net, gross = tr.net, tr.gross
    split = np.median(tr.entry_t)
    wins, losses = net[net > 0].sum(), -net[net < 0].sum()
    eq = np.cumsum(net)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max())
    longs, shorts = net[tr.side > 0], net[tr.side < 0]
    return {"n": len(net), "per_week": len(net) / max(span_ms / WEEK_MS, 1e-9), "win": float((net > 0).mean()),
            "net_bps": float(net.mean() * 1e4), "gross_bps": float(gross.mean() * 1e4), "t": t_weekly(tr.entry_t, net),
            "pf": float(wins / losses) if losses > 0 else float("inf"), "hold_h": float(tr.hold_h.mean()),
            "stop_pct": float(tr.stop.mean() * 100), "dd_pct": dd * 100,
            "long_bps": float(longs.mean() * 1e4) if len(longs) else None, "n_long": len(longs),
            "short_bps": float(shorts.mean() * 1e4) if len(shorts) else None, "n_short": len(shorts),
            "first_bps": float(net[tr.entry_t <= split].mean() * 1e4),
            "second_bps": float(net[tr.entry_t > split].mean() * 1e4) if (tr.entry_t > split).any() else None}


def walk_forward(trades_by_config: dict, months: list[tuple[int, int]], min_n: int = 30) -> tuple[Trades, list]:
    """Each month: pick the config with the best t (mean / std error) on trades that finished before the month,
    then keep that config's trades that start in the month. → (the honest trades, [(month start, config or None)])."""
    out, picks = [], []
    for m0, m1 in months:
        best, best_t = None, -np.inf
        for key, tr in trades_by_config.items():
            past = tr.net[tr.exit_t <= m0]
            if len(past) < min_n:
                continue
            se = past.std(ddof=1) / np.sqrt(len(past))
            tt = past.mean() / se if se > 0 else -np.inf
            if tt > best_t:
                best, best_t = key, tt
        picks.append((m0, best))
        if best is not None:
            tr = trades_by_config[best]
            out.append(tr.where((tr.entry_t >= m0) & (tr.entry_t < m1)))
    return concat(out), picks


def months_of(t0_ms: int, t1_ms: int, warmup_days: float) -> list[tuple[int, int]]:
    """Calendar months (UTC, 30-day blocks for simplicity) after a warm-up."""
    out, m = [], t0_ms + int(warmup_days * 86_400_000)
    while m < t1_ms:
        out.append((m, min(m + 30 * 86_400_000, t1_ms)))
        m += 30 * 86_400_000
    return out


def sizing(tr: Trades, risk: float, coins: np.ndarray, weeks: int = 1, runs: int = 5000, seed: int = 7) -> dict:
    """Risk `risk` of the balance per trade (loss at the stop); leverage = risk / stop distance, capped at the venue
    maximum. Resample whole weeks of the honest trades: what `weeks` weeks in a row can do to the balance."""
    if not len(tr):
        return {}
    lev = risk / tr.stop
    cap = np.array([HL_MAX_LEV.get(c, 10) for c in coins], dtype=float)
    capped = lev > cap
    lev = np.minimum(lev, cap)
    pnl = np.maximum(lev * tr.net, -1.0)                       # share of the balance, per trade
    wk = tr.exit_t // WEEK_MS
    groups = [pnl[wk == w] for w in np.unique(wk)]
    week_mult = np.array([np.prod(1 + g) for g in groups])
    rng = np.random.default_rng(seed)
    draws = week_mult[rng.integers(0, len(week_mult), (runs, weeks))]
    paths = np.cumprod(draws, axis=1)
    end = paths[:, -1]
    low = paths.min(axis=1)
    return {"median": float(np.median(end) - 1), "p10": float(np.percentile(end, 10) - 1),
            "p90": float(np.percentile(end, 90) - 1), "hit25": float((end >= 1.25).mean()),
            "half": float((low <= 0.5).mean()), "lev": float(np.median(lev)), "capped": float(capped.mean()),
            "weeks_seen": len(groups)}

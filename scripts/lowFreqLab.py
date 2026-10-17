"""
Low-frequency lab: do any classic hours-to-days signals make money on Hyperliquid after fees and funding, and how
would you size them for a big week? The maths is in engine/lowfreq.py, the data in ingestion/history.py.

    python -m scripts.lowFreqLab --fetch                 # download / top up the history, then run
    python -m scripts.lowFreqLab                         # run on what's cached

On the server, from /opt/analytix (the first fetch takes a few minutes):
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.lowFreqLab --fetch > /tmp/lowfreq.txt 2>&1 &'

The signals, each tried with a few settings (stop in ATR multiples of the last 24 hours, max hold in hours):
    breakout     close above the high (below the low) of the last N hours: go with it
    momentum     once a day at 00:00 UTC: the side of the last L hours' move
    pullback     in an uptrend (close above its N-hour EMA), buy when RSI(14) dips under a level; the mirror for shorts
    reversion    close far from its N-hour average (z-score): bet on the way back
    funding      funding crowded on one side (its 24-hour average far from the last 30 days'): take the other side
    squeeze      after quiet hours (24-hour ATR well under the 7-day ATR), go with the break of the 24-hour range

Reading it: section 1 is the honest result. Each month every signal's settings are picked using only earlier
trades, then that month is traded. A signal passes only if it's positive after costs with t 2+ and in both halves
of the honest months. Section 2 shows the best settings in hindsight, which always look better. Section 3 sizes
what passes: how much of the balance to risk per trade, and what a week or a month can do.
"""
import argparse
import sys
import time
from collections import Counter

import numpy as np

from engine.lowfreq import (HOUR_MS, atr, concat, ema, months_of, prior_max, prior_min, rolling_mean_std, rsi,
                            simulate, sizing, summary, walk_forward)
from ingestion.history import HISTORY_DIR, load, update

COST_BPS = 11.0          # Hyperliquid taker 4.5 bps each way, plus about 1 bp of slippage each way
RISKS = (0.02, 0.05, 0.10)


def f(x, d=1):
    return "-" if x is None or not np.isfinite(x) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.0f}%"


# ── signals (+1 / -1 / 0 at each bar's close) ──────────────────────────────
def breakout(b, n):
    up, dn = b.c > prior_max(b.h, n), b.c < prior_min(b.l, n)
    return np.where(up, 1, np.where(dn, -1, 0))


def momentum(b, lookback):
    past = np.concatenate([np.full(lookback, np.nan), b.c[:-lookback]])
    r = b.c / past - 1
    at = ((b.t // HOUR_MS) % 24 == 23)                 # the bar ending at 00:00 UTC
    return np.where(at & np.isfinite(r), np.sign(r), 0).astype(int)


def pullback(b, n, lo, hi):
    up = b.c > ema(b.c, n)
    r = rsi(b.c, 14)
    s = np.where(up & (r < lo), 1, np.where(~up & (r > hi), -1, 0))
    s[:n] = 0
    return s


def reversion(b, n, z):
    m, sd = rolling_mean_std(b.c, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        zz = (b.c - m) / sd
    return np.where(zz < -z, 1, np.where(zz > z, -1, 0))


def funding(b, z):
    if not np.any(b.funding):
        return np.zeros(len(b.c), dtype=int)
    f24, _ = rolling_mean_std(b.funding, 24)
    m, sd = rolling_mean_std(np.nan_to_num(f24), 720)
    with np.errstate(divide="ignore", invalid="ignore"):
        zz = (f24 - m) / sd
    return np.where(zz > z, -1, np.where(zz < -z, 1, 0))


def squeeze(b, q):
    a24, a168 = atr(b.h, b.l, b.c, 24), atr(b.h, b.l, b.c, 168)
    quiet = np.concatenate([[False], (a24 / a168 < q)[:-1]])
    s = breakout(b, 24) * quiet
    s[:168] = 0
    return s


FAMILIES = {
    "breakout": [((n,), st, hold) for n in (24, 72, 168) for st in (2, 4) for hold in (24, 72, 168)],
    "momentum": [((lb,), st, hold) for lb in (24, 72, 168, 336) for st in (2, 4) for hold in (24, 72)],
    "pullback": [((n, lo, 100 - lo), st, hold) for n in (100, 200) for lo in (25, 30) for st in (2, 4) for hold in (12, 48)],
    "reversion": [((n, z), st, hold) for n in (24, 72) for z in (2, 3) for st in (2, 4) for hold in (6, 24)],
    "funding": [((z,), st, hold) for z in (1.5, 2.5) for st in (2, 4) for hold in (24, 72)],
    "squeeze": [((q,), st, hold) for q in (0.6, 0.8) for st in (2, 4) for hold in (24, 72)],
}
SIGNAL = {"breakout": breakout, "momentum": momentum, "pullback": pullback, "reversion": reversion,
          "funding": funding, "squeeze": squeeze}


def label(fam, params, st, hold):
    p = {"breakout": "{}h range", "momentum": "{}h move", "pullback": "EMA {} · RSI {}/{}", "reversion": "{}h · z {}",
         "funding": "z {}", "squeeze": "ATR ratio < {}"}[fam].format(*params)
    return f"{p} · stop {st} ATR · max {hold}h"


def run_source(source: str, coins: list[str], cost_bps: float, warmup_days: float, L: list[str],
               folder=HISTORY_DIR) -> None:
    bars = {c: load(c, source, folder) for c in coins}
    bars = {c: b for c, b in bars.items() if b is not None and len(b) > 24 * 30}
    name = {"hl": "HYPERLIQUID", "bn": "BINANCE FUTURES (HL costs)"}[source]
    if not bars:
        L += [f"{name}: no history cached for {', '.join(coins)}. Run with --fetch.", ""]
        return
    t0 = min(int(b.t[0]) for b in bars.values())
    t1 = max(int(b.t[-1]) for b in bars.values()) + HOUR_MS
    months = months_of(t0, t1, warmup_days)
    L.append(f"{name}  " + " · ".join(
        f"{c} {len(b) / 24:.0f}d ({time.strftime('%d %b %y', time.gmtime(b.t[0] / 1000))} to "
        f"{time.strftime('%d %b %y', time.gmtime(b.t[-1] / 1000))}, hold {pct(b.c[-1] / b.c[0] - 1)}, "
        f"funding {np.mean(b.funding != 0) * 100:.0f}% of hours)" for c, b in bars.items()))
    if not months:
        L += ["  Not enough history for a walk-forward test yet.", ""]
        return
    oos_ms = months[-1][1] - months[0][0]
    L.append(f"  Honest months: {len(months)} (after {warmup_days:g} days to learn from). Costs {cost_bps:g} bps a "
             f"round trip plus funding.")
    L.append("")

    results, best_in = {}, {}
    for fam, grid in FAMILIES.items():
        by_cfg = {}
        for params, st, hold in grid:
            trs = []
            for c, b in bars.items():
                sig = SIGNAL[fam](b, *params)
                trs.append(simulate(b, sig, st, hold, cost_bps))
            by_cfg[(params, st, hold)] = concat(trs)
        honest, picks = walk_forward(by_cfg, months, min_n=20)
        results[fam] = (honest, picks)
        span = t1 - t0
        ins = sorted(((summary(tr, span), k) for k, tr in by_cfg.items() if len(tr) >= 30),
                     key=lambda x: -(x[0]["t"] or -9))
        best_in[fam] = ins[:2]

    L.append("  1. HONEST RESULT (walk-forward, all coins together). bps per trade after costs; t from weekly blocks")
    L.append(f"     {'signal':<11}{'trades':>7}{'/week':>7}{'win':>6}{'net bps (t)':>16}{'gross':>8}{'longs':>8}{'shorts':>8}"
             f"{'hold h':>8}{'stop %':>8}{'1st half':>10}{'2nd half':>10}{'months +':>10}")
    passing = []
    for fam, (honest, picks) in results.items():
        s = summary(honest, oos_ms)
        if not s:
            L.append(f"     {fam:<11}{'no trades (nothing qualified in time)':>40}")
            continue
        mon = [honest.net[(honest.entry_t >= m0) & (honest.entry_t < m1)] for m0, m1 in months]
        up = sum(1 for m in mon if len(m) and m.sum() > 0)
        had = sum(1 for m in mon if len(m))
        tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
        L.append(f"     {fam:<11}{s['n']:>7}{s['per_week']:>7.1f}{s['win'] * 100:>5.0f}%{f(s['net_bps']) + ' (t ' + tt + ')':>16}"
                 f"{f(s['gross_bps']):>8}{f(s['long_bps']):>8}{f(s['short_bps']):>8}{s['hold_h']:>8.0f}{s['stop_pct']:>8.1f}"
                 f"{f(s['first_bps']):>10}{f(s['second_bps']):>10}{str(up) + '/' + str(had):>10}")
        if s["net_bps"] > 0 and (s["t"] or 0) >= 2 and s["first_bps"] > 0 and (s["second_bps"] or 0) > 0:
            passing.append(fam)
        used = Counter(k for _, k in picks if k is not None).most_common(1)
        if used:
            L.append(f"     {'':<11}mostly picked: {label(fam, *used[0][0])} ({used[0][1]} of {len(picks)} months)")
    L.append("")

    L.append("  2. BEST SETTINGS IN HINDSIGHT (all the data; flattering, for reference only)")
    for fam, rows in best_in.items():
        for s, k in rows:
            tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
            L.append(f"     {fam:<11}{label(fam, *k):<46}{s['n']:>6} trades{f(s['net_bps']):>8} bps (t {tt}), "
                     f"win {s['win'] * 100:.0f}%")
    L.append("")

    L.append("  3. SIZING. Risk a share of the balance per trade (what a stop-out costs). Leverage = risk / stop "
             "distance, capped at Hyperliquid's maximum. Weeks resampled from the honest trades")
    if not passing:
        L.append("     Nothing passed, so no sizing: no amount of leverage turns a signal without an edge into one.")
    for fam in passing:
        honest = results[fam][0]
        L.append(f"     {fam}:")
        for risk in RISKS:
            w1 = sizing(honest, risk, honest.coins, weeks=1)
            w4 = sizing(honest, risk, honest.coins, weeks=4)
            capped = f", capped on {w1['capped'] * 100:.0f}% of trades" if w1["capped"] > 0.01 else ""
            L.append(f"       risk {risk * 100:.0f}% a trade (leverage about {w1['lev']:.0f}x{capped}): "
                     f"a week: middle {pct(w1['median'])}, bad 1 in 10 {pct(w1['p10'])}, +25% or more "
                     f"{w1['hit25'] * 100:.0f}% of weeks · 4 weeks: middle {pct(w4['median'])}, halved at some point "
                     f"{w4['half'] * 100:.0f}%")
    L.append("")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download / top up the history first")
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--hl-days", type=float, default=210, help="Hyperliquid keeps about 5,000 hourly candles")
    ap.add_argument("--bn-days", type=float, default=1100, help="Binance history for the longer check (0 = skip)")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap.add_argument("--folder", default=str(HISTORY_DIR), help=argparse.SUPPRESS)
    a = ap.parse_args()
    from pathlib import Path
    folder = Path(a.folder)
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    t0 = time.time()
    if a.fetch:
        print(f"Fetching into {HISTORY_DIR}", file=sys.stderr)
        update(coins, a.hl_days, a.bn_days, folder, say=lambda m: print(m, file=sys.stderr))
    L = [f"LOW-FREQUENCY LAB  {', '.join(coins)} · hourly bars · in at the next bar's open, out at the stop or the max "
         f"hold · {a.cost_bps:g} bps a round trip plus funding", ""]
    run_source("hl", coins, a.cost_bps, 60, L, folder)
    if a.bn_days:
        run_source("bn", [c for c in coins if c != "HYPE"], a.cost_bps, 180, L, folder)
    L.append("A signal is worth trading only if it passes on Hyperliquid and holds up on Binance's longer history.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

"""
Wallet study: are some Hyperliquid wallets consistently early, and does following them pay?
The maths is in engine/walletstudy.py.

    python -m scripts.walletStudy                        # last 7 days, every coin, wallets scored at 60s
    python -m scripts.walletStudy --score-h 300 --min-n 30 --t-min 3 --csv /tmp/wallets.csv

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.walletStudy --csv /tmp/wallets.csv > /tmp/wallets.txt 2>&1

Reading it: section 1 says whether skill carries over from the earlier 60% of the data to the later 40% at all.
Section 2 copies the chosen wallets in the later part only, so nothing in it was used to pick them.
"""
import argparse
import csv
import json
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.walletstudy import HORIZONS, LAGS, LEVERAGES, Episodes, Marks, follow_table, spearman, taker_of, wallet_stats
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

T_BUCKETS = ((3, np.inf, "t 3 or more"), (2, 3, "t 2 to 3"), (-2, 2, "no clear skill"), (-3, -2, "t -3 to -2"),
             (-np.inf, -3, "t -3 or less"))


def short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else str(w)


def f(x, d=1):
    return "-" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None else f"{x * 100:+.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--min-usd", type=float, default=1000, help="ignore decisions smaller than this")
    ap.add_argument("--score-h", type=int, default=60, help="markout horizon (s) wallets are scored on")
    ap.add_argument("--min-n", type=int, default=20, help="decisions a wallet needs in the earlier part to be judged")
    ap.add_argument("--t-min", type=float, default=2.5, help="t-stat to count as skilled (and minus this as wrong)")
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--csv", help="write every wallet's scores here")
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    eb = Episodes()

    def on_line(line: str) -> None:
        if '"channel":"trades"' not in line:
            return
        try:
            data = json.loads(line)["msg"]["data"]
        except (ValueError, KeyError, TypeError):
            return
        for tr in data or []:
            coin = tr.get("coin")
            if coin not in coins:
                continue
            who = taker_of(tr)
            if who is None:
                continue
            try:
                eb.add(coin, who[0].lower(), int(tr["time"]), who[1], float(tr["px"]) * float(tr["sz"]))
            except (KeyError, TypeError, ValueError):
                pass

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    eps = [e for e in eb.finish() if e.usd >= a.min_usd and e.wallet not in LIQUIDATOR_ADDRESSES]
    marks = {c: Marks(gr) for c, gr in g.items()}
    print(f"  read {eb.trades:,} trades into {len(eps):,} decisions of ${a.min_usd:,.0f}+, {time.time() - t0:.0f}s",
          file=sys.stderr)
    if not eps:
        print("No trades with wallets found in these recordings.")
        return
    t_lo, t_hi = eps[0].t_ms, eps[-1].t_ms
    split = t_lo + a.train * (t_hi - t_lo)
    train = [e for e in eps if e.t_ms <= split]
    test = [e for e in eps if e.t_ms > split]
    hours = (t_hi - t_lo) / 3.6e6

    L = [f"WALLET STUDY  {hours:.0f} h · {', '.join(sorted(g))} · {eb.trades:,} trades → {len(eps):,} decisions of "
         f"${a.min_usd:,.0f}+ by {len({e.wallet for e in eps}):,} wallets",
         f"A decision = one wallet's taker trades on one coin and side, under 30s apart. Earlier {a.train:.0%} "
         f"({len(train):,} decisions) picks wallets; the later part ({len(test):,}) judges them.", ""]

    # 1. does skill carry over?
    L.append("1. DOES SKILL CARRY OVER? Each wallet's average move after its decisions (bps, from the second after "
             "its trade), earlier part vs later part")
    L.append(f"   {'horizon':<9}{'wallets':>8}{'rank match':>12}   (1 = same order both times, 0 = no relation)")
    stats_tr = {h: wallet_stats(train, marks, h) for h in HORIZONS}
    stats_te = {h: wallet_stats(test, marks, h) for h in HORIZONS}
    for h in HORIZONS:
        both = [w for w, s in stats_tr[h].items() if s["n"] >= a.min_n and stats_te[h].get(w, {}).get("n", 0) >= a.min_n // 2]
        rho = spearman(np.array([stats_tr[h][w]["mean"] for w in both]), np.array([stats_te[h][w]["mean"] for w in both]))
        L.append(f"   {str(h) + 's':<9}{len(both):>8}{f(rho, 2):>12}")
    h0 = a.score_h
    tr, te = stats_tr.get(h0) or wallet_stats(train, marks, h0), stats_te.get(h0) or wallet_stats(test, marks, h0)
    L.append("")
    L.append(f"   Wallets grouped by their earlier score at {h0}s ({a.min_n}+ decisions) → how their later decisions did at {h0}s")
    L.append(f"   {'earlier score':<18}{'wallets':>8}{'later decisions':>17}{'later avg bps':>15}")
    for lo, hi, label in T_BUCKETS:
        ws = [w for w, s in tr.items() if s["n"] >= a.min_n and lo <= s["t"] < hi]
        later = [(te[w]["mean"], te[w]["n"]) for w in ws if w in te]
        n = sum(k for _, k in later)
        avg = sum(m * k for m, k in later) / n if n else None
        L.append(f"   {label:<18}{len(ws):>8}{n:>17,}{f(avg, 2):>15}")
    L.append("")

    # 2. follow the skilled, fade the wrong
    smart = {w for w, s in tr.items() if s["n"] >= a.min_n and s["t"] >= a.t_min and s["mean"] > 0}
    wrong = {w for w, s in tr.items() if s["n"] >= a.min_n and s["t"] <= -a.t_min and s["mean"] < 0}
    groups = [("copy skilled", [e for e in test if e.wallet in smart], False),
              ("fade wrong", [e for e in test if e.wallet in wrong], True),
              ("copy everyone", test, False),
              ("copy $50k+", [e for e in test if e.usd >= 50_000], False)]
    L.append(f"2. FOLLOWING, later part only. Skilled = {len(smart)} wallets with t ≥ {a.t_min:g} at {h0}s earlier; "
             f"wrong = {len(wrong)} with t ≤ -{a.t_min:g}.")
    L.append("   mid = bps at the mid (t from hourly blocks); spread = after paying the spread both ways; "
             + ", ".join(f"{lev}x" for lev in LEVERAGES) + " = Papertrade, % of margin per trade")
    for name, sub, flip in groups:
        if not sub:
            L.append(f"   {name}: no decisions")
            continue
        tab = follow_table(sub, marks, flip=flip)
        L.append(f"   {name} ({len(sub):,} decisions)")
        L.append(f"     {'in after':<9}{'hold':>5}{'n':>8}{'mid (t)':>16}{'spread':>8}{'win':>6}"
                 + "".join(f"{str(lev) + 'x':>9}" for lev in LEVERAGES))
        for lag in LAGS:
            for h in HORIZONS:
                r = tab.get((lag, h))
                if not r:
                    continue
                tt = f"{r['mid_t']:.1f}" if r["mid_t"] is not None else "-"
                L.append(f"     {str(lag) + 's':<9}{str(h) + 's':>5}{r['n']:>8,}{f(r['mid']) + ' (t ' + tt + ')':>16}"
                         f"{f(r['spread']):>8}{r['win'] * 100:>5.0f}%" + "".join(f"{pct(r['pt'][lev]):>9}" for lev in LEVERAGES))
    L.append("")

    # 3. the wallets themselves
    L.append(f"3. TOP SKILLED WALLETS (earlier part, scored at {h0}s)")
    L.append(f"   {'wallet':<13}{'coins':<14}{'earlier n':>10}{'avg bps':>9}{'t':>6}{'later n':>9}{'later avg':>11}{'typical size':>14}")
    for w in sorted(smart, key=lambda w: -tr[w]["t"])[:15]:
        s, l = tr[w], te.get(w, {})
        L.append(f"   {short(w):<13}{','.join(s['coins'])[:13]:<14}{s['n']:>10}{f(s['mean']):>9}{s['t']:>6.1f}"
                 f"{l.get('n', 0):>9}{f(l.get('mean')):>11}{'$' + format(s['usd'], ',.0f'):>14}")
    L.append("")
    L.append("Trust it only if: section 1 shows a clear positive rank match, the t 3+ group stays positive later, and "
             "'copy skilled' beats 'copy everyone' and 'copy $50k+' in the later part.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))

    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["wallet", "coins", "n_train", "mean_train_bps", "t_train", "n_test", "mean_test_bps", "median_usd"])
            for w, s in sorted(tr.items(), key=lambda kv: -kv[1]["t"]):
                l = te.get(w, {})
                wr.writerow([w, "|".join(s["coins"]), s["n"], round(s["mean"], 3), round(s["t"], 2),
                             l.get("n", 0), round(l["mean"], 3) if l else "", round(s["usd"], 0)])


if __name__ == "__main__":
    main()

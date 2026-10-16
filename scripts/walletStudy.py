"""
Wallet study: are some Hyperliquid wallets consistently early, and does following them pay?
The maths is in engine/walletstudy.py.

    python -m scripts.walletStudy                        # last 7 days, wallets scored on every coin, followed on BTC/ETH
    python -m scripts.walletStudy --score-h 30 --min-n 30 --t-min 3 --hold 60 --csv /tmp/wallets.csv

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.walletStudy --csv /tmp/wallets.csv > /tmp/wallets.txt 2>&1

Reading it: section 1 says whether skill carries over from the earlier 60% of the data to the later 40% at all.
Everything after it copies wallets in the later part only, so nothing there was used to pick them. Following is
measured on the coins Papertrade lists (BTC and ETH by default): wallets are scored on every coin.
"""
import argparse
import csv
import json
import re
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.walletstudy import (HORIZONS, LAGS, LEAD_GAP_BPS, LEAD_WINDOW_S, LEVERAGES, Episodes, Marks, follow_table,
                                last_per_second, lead_gap, spearman, taker_of, wallet_stats)
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

T_BUCKETS = ((3, np.inf, "t 3 or more"), (2, 3, "t 2 to 3"), (-2, 2, "no clear skill"), (-3, -2, "t -3 to -2"),
             (-np.inf, -3, "t -3 or less"))
TIERS = ((0.0, 3.0, "fast scalpers"), (3.0, 6.0, "movers"), (6.0, np.inf, "big movers"))
EXT = re.compile(r'"venue":"binance","coin":"([A-Za-z0-9]+)","ts":(\d+),"bid":([0-9.eE+-]+),"ask":([0-9.eE+-]+)')


def short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else str(w)


def f(x, d=1):
    return "-" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None else f"{x * 100:+.0f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE", help="coins wallets are scored on")
    ap.add_argument("--trade", default="BTC,ETH", help="coins following is measured on (Papertrade's)")
    ap.add_argument("--min-usd", type=float, default=1000, help="ignore decisions smaller than this")
    ap.add_argument("--score-h", type=int, default=60, help="markout horizon (s) wallets are scored on")
    ap.add_argument("--min-n", type=int, default=20, help="decisions a wallet needs in the earlier part to be judged")
    ap.add_argument("--t-min", type=float, default=2.5, help="t-stat to count as skilled (and minus this as wrong)")
    ap.add_argument("--hold", type=int, default=30, help="hold (s) for the main comparison table")
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--csv", help="write every wallet's scores here")
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    trade = {c.strip().upper() for c in a.trade.split(",") if c.strip()} & coins
    eb = Episodes()
    binance: dict[str, dict[int, float]] = {c: {} for c in coins}

    def on_line(line: str) -> None:
        if '"channel":"ext"' in line:
            m = EXT.search(line)
            if m and m.group(1) in binance:
                binance[m.group(1)][int(m.group(2)) // 1000] = (float(m.group(3)) + float(m.group(4))) / 2
            return
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
    bgrid = {c: last_per_second(rows) for c, rows in binance.items()}
    print(f"  read {eb.trades:,} trades into {len(eps):,} decisions of ${a.min_usd:,.0f}+, {time.time() - t0:.0f}s",
          file=sys.stderr)
    if not eps:
        print("No trades with wallets found in these recordings.")
        return
    t_lo, t_hi = eps[0].t_ms, eps[-1].t_ms
    split = t_lo + a.train * (t_hi - t_lo)
    train = [e for e in eps if e.t_ms <= split]
    test = [e for e in eps if e.t_ms > split]
    test_h = max((t_hi - split) / 3.6e6, 1e-6)
    hours = (t_hi - t_lo) / 3.6e6

    # Binance gap for every later decision on the traded coins
    gap: dict[int, float] = {}
    for coin in trade:
        sub = [e for e in test if e.coin == coin]
        if not sub or coin not in g:
            continue
        b0, bm = bgrid.get(coin, (0, np.zeros(0)))
        vals = lead_gap(np.array([e.t_ms for e in sub]), np.array([e.side for e in sub]), g[coin], b0, bm)
        for e, v in zip(sub, vals):
            gap[id(e)] = v

    L = [f"WALLET STUDY v2  {hours:.0f} h · scored on {', '.join(sorted(coins & set(g)))} · followed on "
         f"{', '.join(sorted(trade))} · {eb.trades:,} trades → {len(eps):,} decisions of ${a.min_usd:,.0f}+ by "
         f"{len({e.wallet for e in eps}):,} wallets",
         f"A decision = one wallet's taker trades on one coin and side, under 30s apart. Earlier {a.train:.0%} "
         f"({len(train):,} decisions) picks wallets; the later {test_h:.0f} h ({len(test):,}) judges them.", ""]

    # 1. does skill carry over?
    L.append("1. DOES SKILL CARRY OVER? Each wallet's average move after its decisions, earlier part vs later part")
    L.append(f"   {'horizon':<9}{'wallets':>8}{'rank match':>12}   (1 = same order both times, 0 = no relation)")
    stats_tr = {h: wallet_stats(train, marks, h) for h in HORIZONS}
    stats_te = {h: wallet_stats(test, marks, h) for h in HORIZONS}
    for h in HORIZONS:
        both = [w for w, s in stats_tr[h].items() if s["n"] >= a.min_n and stats_te[h].get(w, {}).get("n", 0) >= a.min_n // 2]
        rho = spearman(np.array([stats_tr[h][w]["mean"] for w in both]), np.array([stats_te[h][w]["mean"] for w in both]))
        L.append(f"   {str(h) + 's':<9}{len(both):>8}{f(rho, 2):>12}")
    h0 = a.score_h
    tr = stats_tr.get(h0) or wallet_stats(train, marks, h0)
    te = stats_te.get(h0) or wallet_stats(test, marks, h0)
    L.append(f"   Grouped by earlier score at {h0}s ({a.min_n}+ decisions) → later average at {h0}s:  " + "  ·  ".join(
        f"{label} {f(sum(te[w]['mean'] * te[w]['n'] for w in ws if w in te) / max(1, sum(te[w]['n'] for w in ws if w in te)), 2)}"
        for lo, hi, label in T_BUCKETS
        for ws in [[w for w, s in tr.items() if s["n"] >= a.min_n and lo <= s["t"] < hi]] if ws))
    L.append("")

    # the groups
    smart = {w for w, s in tr.items() if s["n"] >= a.min_n and s["t"] >= a.t_min and s["mean"] > 0}
    wrong = {w for w, s in tr.items() if s["n"] >= a.min_n and s["t"] <= -a.t_min and s["mean"] < 0}
    tier_of = {w: label for w in smart for lo, hi, label in TIERS if lo <= tr[w]["mean"] < hi}
    later = [e for e in test if e.coin in trade]
    skilled = [e for e in later if e.wallet in smart]
    lead_t = lambda e: gap.get(id(e), np.nan)
    groups = [("all skilled", skilled, False)]
    groups += [(label, [e for e in skilled if tier_of.get(e.wallet) == label], False) for _, _, label in TIERS]
    groups += [(f"skilled, Binance led", [e for e in skilled if lead_t(e) >= LEAD_GAP_BPS], False),
               (f"skilled, no Binance lead", [e for e in skilled if abs(lead_t(e)) < LEAD_GAP_BPS], False),
               (f"skilled, against Binance", [e for e in skilled if lead_t(e) <= -LEAD_GAP_BPS], False),
               ("big movers, no Binance lead", [e for e in skilled if tier_of.get(e.wallet) == "big movers"
                                                and abs(lead_t(e)) < LEAD_GAP_BPS], False),
               ("fade the wrong", [e for e in later if e.wallet in wrong], True),
               ("copy everyone", later, False),
               ("copy $50k+", [e for e in later if e.usd >= 50_000], False)]
    tiers_n = {label: sum(1 for w in smart if tier_of.get(w) == label) for _, _, label in TIERS}
    L.append(f"2. HOW LATE CAN YOU BE? Later part, {', '.join(sorted(trade))}, held {a.hold}s. Each cell: bps at the "
             f"mid / Papertrade at {LEVERAGES[-1]}x (% of margin per trade).")
    L.append(f"   Skilled = {len(smart)} wallets with t ≥ {a.t_min:g} at {h0}s earlier ("
             + ", ".join(f"{k} {v}" for k, v in tiers_n.items()) + f"; tiers by their earlier average: under 3, 3-6, "
             f"6+ bps). Binance led = Binance moved their way {LEAD_GAP_BPS:g}+ bps more than Hyperliquid in the "
             f"{LEAD_WINDOW_S}s before.")
    L.append(f"   {'group':<29}{'per hour':>9}" + "".join(f"{str(lag) + 's late':>15}" for lag in LAGS))
    tables = {}
    for name, sub, flip in groups:
        if not sub:
            L.append(f"   {name:<29}{'0':>9}")
            continue
        tab = follow_table(sub, marks, flip=flip, horizons=(a.hold,))
        tables[name] = tab
        cells = []
        for lag in LAGS:
            r = tab.get((lag, a.hold))
            cells.append("-" if not r else f"{f(r['mid'])} / {pct(r['pt'][LEVERAGES[-1]])}")
        L.append(f"   {name:<29}{len(sub) / test_h:>9.0f}" + "".join(f"{c:>15}" for c in cells))
    L.append("")

    # 3. hold and leverage, for the main groups at 2s late
    L.append("3. HOLD AND LEVERAGE, 2s late. mid bps (t from hourly blocks), win rate, Papertrade % of margin per trade")
    for name in ("all skilled", "big movers", "skilled, no Binance lead"):
        sub = next((s for n, s, _ in groups if n == name), None)
        if not sub:
            continue
        tab = follow_table(sub, marks, lags=(2,), horizons=HORIZONS)
        L.append(f"   {name} ({len(sub):,} decisions)")
        L.append(f"     {'hold':>5}{'n':>9}{'mid (t)':>16}{'win':>6}" + "".join(f"{str(lev) + 'x':>8}" for lev in LEVERAGES))
        for h in HORIZONS:
            r = tab.get((2, h))
            if not r:
                continue
            tt = f"{r['mid_t']:.1f}" if r["mid_t"] is not None else "-"
            L.append(f"     {str(h) + 's':>5}{r['n']:>9,}{f(r['mid']) + ' (t ' + tt + ')':>16}{r['win'] * 100:>5.0f}%"
                     + "".join(f"{pct(r['pt'][lev]):>8}" for lev in LEVERAGES))
    L.append("")

    # 4. the wallets themselves
    L.append(f"4. TOP SKILLED WALLETS by earlier t at {h0}s (later = how they did in the later part, all coins)")
    L.append(f"   {'wallet':<13}{'tier':<15}{'coins':<14}{'earlier n':>10}{'avg bps':>9}{'t':>6}{'later n':>9}"
             f"{'later avg':>11}{'typical size':>14}")
    for w in sorted(smart, key=lambda w: -tr[w]["t"])[:15]:
        s, l = tr[w], te.get(w, {})
        L.append(f"   {short(w):<13}{tier_of.get(w, ''):<15}{','.join(s['coins'])[:13]:<14}{s['n']:>10}{f(s['mean']):>9}"
                 f"{s['t']:>6.1f}{l.get('n', 0):>9}{f(l.get('mean')):>11}{'$' + format(s['usd'], ',.0f'):>14}")
    L.append("")
    L.append("Pick the group and lateness you can live with: it needs a positive Papertrade number at the speed you "
             "can actually fill, and enough decisions per hour to be worth watching.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))

    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["wallet", "tier", "coins", "n_train", "mean_train_bps", "t_train", "n_test", "mean_test_bps",
                         "median_usd"])
            for w, s in sorted(tr.items(), key=lambda kv: -kv[1]["t"]):
                l = te.get(w, {})
                wr.writerow([w, tier_of.get(w, ""), "|".join(s["coins"]), s["n"], round(s["mean"], 3), round(s["t"], 2),
                             l.get("n", 0), round(l["mean"], 3) if l else "", round(s["usd"], 0)])


if __name__ == "__main__":
    main()

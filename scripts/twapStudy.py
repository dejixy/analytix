"""
TWAP hunting (edge idea 3): once a wallet's steady beat of market orders is spotted, does price keep drifting its
way while the rest of the order comes in? Detection is engine/twap.py.

    python -m scripts.twapStudy                                  # 7 days, BTC and ETH, spotted at the 4th slice
    python -m scripts.twapStudy --k 6 --min-usd 5000

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.twapStudy > /tmp/twap.txt 2>&1

Reading it: section 1 shows how many TWAPs there are and how big. Section 2 rides each one from when it's spotted
(+ = price went the TWAP's way after you got in). Section 3 checks lateness, leverage and both halves for the best
group. Worth building only if it's positive in both halves with a fill of 6s or more.
"""
import argparse
import json
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.twap import Slices, ride, runs
from engine.walletstudy import taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

HOLDS = (60, 300, 900, 1800, None)          # None = until the beat stops
LEVS = (50, 100, 200, 500)
SIZES = ((1_000, 5_000, "$1-5k"), (5_000, 20_000, "$5-20k"), (20_000, np.inf, "$20k+"))


def f(x, d=1):
    return "-" if x is None or not np.isfinite(x) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.0f}%"


def hold_label(h):
    return "to the end" if h is None else (f"{h // 60}m" if h >= 60 else f"{h}s")


def t_hourly(t_ms, v):
    if len(v) < 3:
        return None
    _, inv = np.unique(np.asarray(t_ms) // 3_600_000, return_inverse=True)
    m = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(m) < 3 or m.std(ddof=1) == 0:
        return None
    return float(v.mean() / (m.std(ddof=1) / np.sqrt(len(m))))


def stats(rs, g, k, lag, hold, split_ms):
    rows = []
    for r in rs:
        res = ride(g[r.coin], r, k, lag, hold, LEVS, r.coin) if r.coin in g else None
        if res:
            rows.append((r.times[k - 1], res))
    if not rows:
        return None
    t = np.array([x[0] for x in rows])
    b = np.array([x[1]["bps"] for x in rows])
    first, second = b[t <= split_ms], b[t > split_ms]
    return {"n": len(rows), "bps": float(b.mean()), "t": t_hourly(t, b), "win": float((b > 0).mean()),
            "pt": {lev: float(np.mean([x[1]["pt"][lev] for x in rows])) for lev in LEVS},
            "bust": {lev: float(np.mean([x[1]["pt"][lev] == -1.0 for x in rows])) for lev in LEVS},
            "before": float(np.mean([x[1]["before"] for x in rows])),
            "hold": float(np.median([x[1]["hold_s"] for x in rows])),
            "first": float(first.mean()) if len(first) else None, "second": float(second.mean()) if len(second) else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH")
    ap.add_argument("--k", type=int, default=4, help="slices needed before a run counts as spotted")
    ap.add_argument("--min-usd", type=float, default=1000, help="smallest slice that counts")
    ap.add_argument("--lag", type=int, default=6, help="seconds from spotting to your fill")
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    sl = Slices()
    n_trades = [0]

    def on_line(line: str) -> None:
        if '"channel":"trades"' not in line:
            return
        try:
            data = json.loads(line)["msg"]["data"]
        except (ValueError, KeyError, TypeError):
            return
        for tr in data or []:
            coin = tr.get("coin")
            who = taker_of(tr) if coin in coins else None
            if who is None or who[0].lower() in LIQUIDATOR_ADDRESSES:
                continue
            try:
                sl.add(who[0].lower(), coin, who[1], int(tr["time"]), float(tr["px"]) * float(tr["sz"]))
                n_trades[0] += 1
            except (KeyError, TypeError, ValueError):
                pass

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    found = runs(sl, a.k, a.min_usd)
    print(f"  {n_trades[0]:,} aggressive fills, {len(found):,} runs, {time.time() - t0:.0f}s", file=sys.stderr)
    if not found:
        print("No TWAP-like runs found.")
        return
    hours = max(len(gr) for gr in g.values()) / 3600
    split = float(np.median([r.times[a.k - 1] for r in found]))
    groups = []
    for kind in ("native", "regular"):
        for lo, hi, label in SIZES:
            rs = [r for r in found if r.kind == kind and lo <= np.median(r.usd) < hi]
            groups.append((f"{kind} {label}", rs))

    L = [f"TWAP HUNTING  {hours:.0f} h · {', '.join(sorted(g))} · a run = one wallet's market orders at a steady beat "
         f"(native = Hyperliquid's 30s TWAP; regular = any other steady beat of 8-120s), spotted at slice {a.k}", ""]
    L.append("1. HOW MANY, HOW BIG")
    L.append(f"   {'group':<20}{'runs':>6}{'per day':>9}{'slices (median)':>17}{'lasts (median)':>16}{'total $ (median)':>18}{'beat':>7}")
    for name, rs in groups:
        if not rs:
            continue
        L.append(f"   {name:<20}{len(rs):>6}{len(rs) / hours * 24:>9.1f}{np.median([len(r.times) for r in rs]):>17.0f}"
                 f"{np.median([(r.times[-1] - r.times[0]) / 60000 for r in rs]):>14.1f}m"
                 f"{'$' + format(np.median([sum(r.usd) for r in rs]), ',.0f'):>18}{np.median([r.beat_s for r in rs]):>6.0f}s")
    L.append("")

    L.append(f"2. RIDING THEM. In {a.lag}s after slice {a.k}, the TWAP's way; bps at the mid (t from hourly blocks). "
             f"'Before' = how far price had already gone its way during the first {a.k} slices.")
    L.append(f"   {'group':<20}{'n':>6}{'before':>8}" + "".join(f"{hold_label(h):>15}" for h in HOLDS))
    for name, rs in groups:
        cells, n, before = [], 0, None
        for h in HOLDS:
            s = stats(rs, g, a.k, a.lag, h, split)
            if s:
                n, before = max(n, s["n"]), s["before"] if before is None else before
                tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
                cells.append(f"{f(s['bps'])} (t {tt})")
            else:
                cells.append("-")
        if n:
            L.append(f"   {name:<20}{n:>6}{f(before):>8}" + "".join(f"{c:>15}" for c in cells))
    L.append("")

    L.append("3. THE DETAIL: lateness, leverage (Papertrade % of margin per trade, busts in brackets) and both halves")
    L.append(f"   {'group':<20}{'hold':>11}{'filled':>7}{'n':>5}{'bps':>7}{'win':>6}" + "".join(f"{str(lev) + 'x':>13}" for lev in LEVS)
             + f"{'1st half':>10}{'2nd half':>10}")
    for name, rs in groups:
        if len(rs) < 20:
            continue
        for h in (300, None):
            for lag in (2, 6, 10, 30):
                s = stats(rs, g, a.k, lag, h, split)
                if not s:
                    continue
                L.append(f"   {name:<20}{hold_label(h):>11}{str(lag) + 's':>7}{s['n']:>5}{f(s['bps']):>7}{s['win'] * 100:>5.0f}%"
                         + "".join(f"{pct(s['pt'][lev]) + ' (' + format(s['bust'][lev] * 100, '.0f') + '%)':>13}" for lev in LEVS)
                         + f"{f(s['first']):>10}{f(s['second']):>10}")
    L.append("")
    L.append("Worth building only if a group is positive in both halves at a fill of 6s or more, with a Papertrade "
             "column positive and busts you can live with.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

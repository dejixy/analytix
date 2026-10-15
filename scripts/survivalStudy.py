"""
Survival study: does the side the Hold clock favours (it lasts longer before busting) actually win?
The maths is in engine/survivalstudy.py.

    python -m scripts.survivalStudy                                  # last 7 days, ETH and BTC, 841x, +25% and +50%
    python -m scripts.survivalStudy --days 3 --leverage 500 --targets 25,50,100

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.survivalStudy > /tmp/survival.txt 2>&1

Reading it: "avg" is what one trade paid on average, in % of margin, after Papertrade's cut on wins (a bust is
-100%). The read only works if the favoured side's avg is above zero in both halves of the data.
"""
import argparse
import gzip
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from config import RECORD_DIR
from engine.holdclock import Grid, papertrade_kept
from engine.survivalstudy import GAPS, LAG_S, RACE_S, STEP_S, moments, summarize
from ingestion.replay import _first_recv, recording_files

STALE_S = 5
BOOK = re.compile(r'"coin":"([A-Za-z0-9]+)","time":(\d+),"levels":\[\[\{"px":"([0-9.]+)".*?\}\],\[\{"px":"([0-9.]+)"')


def book_of(line: str):
    """(coin, ms, best bid, best ask) from a recorded l2Book line, or None."""
    if '"l2Book"' not in line:
        return None
    m = BOOK.search(line)
    if m:
        return m.group(1), int(m.group(2)), float(m.group(3)), float(m.group(4))
    try:
        d = json.loads(line)["msg"]["data"]
        bids, asks = d["levels"]
        return d["coin"], int(d["time"]), float(bids[0]["px"]), float(asks[0]["px"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def grids(files: list[Path], coins: set[str], progress: bool = True) -> dict[str, Grid]:
    """One row per second per coin, the way api/holdclock.py builds it live: lowest best bid, highest best ask, last
    mid, and ok = a real book update within the last STALE_S seconds."""
    per: dict[str, dict[int, list]] = {c: {} for c in coins}
    t0 = time.time()
    for k, path in enumerate(files):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    b = book_of(line)
                    if not b or b[0] not in per or b[2] <= 0 or b[3] <= 0:
                        continue
                    coin, ms, bid, ask = b
                    s = ms // 1000
                    row = per[coin].get(s)
                    if row is None:
                        per[coin][s] = [bid, ask, (bid + ask) / 2]
                    else:
                        row[0], row[1], row[2] = min(row[0], bid), max(row[1], ask), (bid + ask) / 2
        except (EOFError, OSError):
            pass                                                         # a file cut off mid-write: use what's there
        if progress and (k + 1) % 24 == 0:
            print(f"  read {k + 1}/{len(files)} files, {time.time() - t0:.0f}s", file=sys.stderr)
    out = {}
    for coin, rows in per.items():
        if not rows:
            continue
        ts = np.array(sorted(rows))
        t_lo, t_hi = int(ts[0]), int(ts[-1])
        n = t_hi - t_lo + 1
        bid, ask, mid = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        for s in ts:
            i = s - t_lo
            bid[i], ask[i], mid[i] = rows[int(s)]
        have = ~np.isnan(mid)
        last = np.where(have, np.arange(n), 0)
        np.maximum.accumulate(last, out=last)
        ok = (np.arange(n) - last) <= STALE_S
        for arr in (bid, ask, mid):
            arr[:] = arr[last]
        out[coin] = Grid(np.arange(t_lo, t_hi + 1), bid, ask, mid, ok)
    return out


def pct(x, signed=True):
    return "-" if x is None else (f"{x * 100:+.1f}%" if signed else f"{x * 100:.0f}%")


def report(ms, coins, hours, leverage, targets_pct) -> str:
    split_t = float(np.median([m.t for m in ms])) if ms else 0.0
    dist_bps = 1e4 / leverage
    L = [f"SURVIVAL STUDY  {hours:.0f} h of recordings · {', '.join(coins)} · {leverage:g}x (bust {dist_bps:.1f} bps away) "
         f"· {len(ms)} moments, one every {STEP_S}s",
         f"Each moment: what the Hold clock showed (how long a long and a short last 9 times in 10), then a trade "
         f"entered {LAG_S}s later at the mid, held up to {RACE_S // 60} min. 'avg' = paid per trade in % of margin after "
         "Papertrade's cut; a bust is -100%. t uses hourly blocks.", ""]
    rows = [("no read", "both sides", lambda m: [1, -1])]
    for lo, hi, label in GAPS:
        rows.append((label, "favoured", lambda m, lo=lo, hi=hi: [m.side] if lo <= m.gap < hi else []))
    rows.append(("3x or more", "other side", lambda m: [-m.side] if m.gap >= 3 else []))
    rows.append(("2x or more", "favoured + 10m trend agrees", lambda m: [m.side] if m.gap >= 2 and m.trend == m.side else []))
    rows.append(("2x or more", "favoured, trend doesn't", lambda m: [m.side] if m.gap >= 2 and m.trend != m.side else []))
    rows.append(("3x or more", "favoured short", lambda m: [m.side] if m.gap >= 3 and m.side < 0 else []))
    rows.append(("3x or more", "favoured long", lambda m: [m.side] if m.gap >= 3 and m.side > 0 else []))
    for k, tp in enumerate(targets_pct):
        tg = tp / 100 / leverage
        pays = {c: papertrade_kept(tg, c) * leverage for c in ("ETH", "BTC")}
        be = {c: 1 / (1 + p) if p > 0 else None for c, p in pays.items()}
        L.append(f"TARGET +{tp:g}% of margin ({tg * 1e4:.1f} bps): a win pays ETH {pct(pays['ETH'])} / BTC {pct(pays['BTC'])} "
                 f"after the cut; break-even win rate about {pct(be['ETH'], False)}")
        L.append(f"   {'gap':<12}{'side':<29}{'trades':>7}{'win':>6}{'bust':>6}   {'avg (t)':<17}{'1st half':>9}{'2nd half':>10}")
        for label, side, pick in rows:
            s = summarize(ms, pick, k, tg, leverage, split_t)
            if not s:
                continue
            tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
            L.append(f"   {label:<12}{side:<29}{s['n']:>7}{pct(s['win'], False):>6}{pct(s['bust'], False):>6}   "
                     f"{pct(s['avg']) + ' (t ' + tt + ')':<17}{pct(s['first']):>9}{pct(s['second']):>10}")
        L.append("")
    by_gap = np.array([m.gap for m in ms]) if ms else np.array([])
    if len(by_gap):
        L.append("How often each gap shows up: " + ", ".join(
            f"{label} {np.mean((by_gap >= lo) & (by_gap < hi)):.0%}" for lo, hi, label in GAPS))
    L.append("Trust a row only if it has a few hundred trades, t of 3 or more, and both halves above zero.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="ETH,BTC")
    ap.add_argument("--leverage", type=float, default=841)
    ap.add_argument("--targets", default="25,50", help="% of margin before Papertrade's cut")
    a = ap.parse_args()
    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    targets = [float(x) for x in a.targets.split(",") if x.strip()]
    t0 = time.time()
    g = grids(files, set(coins))
    ms, hours = [], 0.0
    for coin in coins:
        if coin not in g:
            continue
        hours = max(hours, len(g[coin]) / 3600)
        ms += moments(coin, g[coin], a.leverage, [t / 100 / a.leverage for t in targets])
        print(f"  {coin}: {len(g[coin]) / 3600:.0f} h, {time.time() - t0:.0f}s", file=sys.stderr)
    print(f"({time.time() - t0:.0f}s)\n")
    print(report(ms, [c for c in coins if c in g], hours, a.leverage, targets))


if __name__ == "__main__":
    main()

"""
TWAP v2: does knowing a TWAP's full size help? Hyperliquid's API shows any wallet's TWAP order (total size, minutes,
how much is done), so when the bot spots one it can know how much buying or selling is still to come and for how
long. This checks, on the recordings, whether that matters: TWAPs with more still to come, or a bigger share of the
market's volume, should push price harder. It also checks spotting at slice 2 instead of 4 (the API can confirm a
TWAP that early).

    python -m scripts.twapSize                         # 7 days, BTC and ETH
    python -m scripts.twapSize --min-slice 5000

On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.twapSize > /tmp/twapsize.txt 2>&1 &'

"Still to come" is read from the recordings after the fact (the rest of the run). Live, the API gives the planned size
at the moment of spotting, which is the same thing unless the TWAP is cancelled early. Participation = what's still to
come as a share of the coin's usual trading over the time left (its average over the previous 24 hours).

Reading it: a size or participation split is worth building on if the bigger group is clearly better than the
smaller one in both halves.
"""
import argparse
import json
import sys
import time
from collections import defaultdict

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.twap import Slices, ride, runs
from engine.walletstudy import taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids
from scripts.twapStudy import f, pct, t_hourly

LEVS = (200,)
REMAIN = ((0, 250_000, "under $250k"), (250_000, 1_000_000, "$250k-1M"), (1_000_000, np.inf, "$1M+"))
MINUTES = ((0, 5, "under 5 min"), (5, 15, "5-15 min"), (15, np.inf, "15+ min"))
SHARE = ((0, 0.02, "under 2%"), (0.02, 0.05, "2-5%"), (0.05, np.inf, "5%+"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH")
    ap.add_argument("--min-slice", type=float, default=20_000, help="median slice size for a big TWAP")
    ap.add_argument("--lag", type=int, default=6)
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    sl = Slices()
    vol = defaultdict(float)                         # (coin, hour) → $ traded

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
            try:
                usd, ms = float(tr["px"]) * float(tr["sz"]), int(tr["time"])
            except (KeyError, TypeError, ValueError):
                continue
            vol[(coin, ms // 3_600_000)] += usd
            who = taker_of(tr)
            if who is not None and who[0].lower() not in LIQUIDATOR_ADDRESSES:
                sl.add(who[0].lower(), coin, who[1], ms, usd)

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    found = [r for r in runs(sl, 4, 1000) if r.kind == "native" and np.median(r.usd) >= a.min_slice and r.coin in g]
    print(f"  {len(found)} big native TWAPs, {time.time() - t0:.0f}s", file=sys.stderr)
    if not found:
        print("No big TWAPs found.")
        return

    def per_second(coin, t_ms):
        h = t_ms // 3_600_000
        past = [vol.get((coin, h - k), 0.0) for k in range(1, 25)]
        past = [x for x in past if x > 0]
        return np.mean(past) / 3600 if past else np.nan

    rows = []
    for r in found:
        for k in (2, 4):
            spot = r.times[k - 1]
            left_usd = float(sum(r.usd[k:]))
            left_s = max((r.times[-1] - spot) / 1000 + 30, 30)
            share = left_usd / (per_second(r.coin, spot) * left_s)
            row = {"run": r, "k": k, "t": spot, "left_usd": left_usd, "left_min": left_s / 60, "share": share,
                   "total": float(sum(r.usd))}
            for hold in (300, 900, None):
                res = ride(g[r.coin], r, k, a.lag, hold, LEVS, r.coin)
                row[hold] = res
            rows.append(row)
    hours = max(len(gr) for gr in g.values()) / 3600
    split = float(np.median([x["t"] for x in rows]))

    def stat(sub, hold):
        got = [(x["t"], x[hold]) for x in sub if x[hold]]
        if len(got) < 3:
            return None
        t = np.array([x[0] for x in got])
        b = np.array([x[1]["bps"] for x in got])
        p = np.array([x[1]["pt"][200] for x in got])
        return {"n": len(b), "bps": b.mean(), "t": t_hourly(t, b), "win": (b > 0).mean(), "pt": p.mean(),
                "first": b[t <= split].mean() if (t <= split).any() else np.nan,
                "second": b[t > split].mean() if (t > split).any() else np.nan}

    def line(name, s):
        if not s:
            return f"   {name:<26}{'too few':>8}"
        tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
        return (f"   {name:<26}{s['n']:>6}{f(s['bps']) + ' (t ' + tt + ')':>16}{s['win'] * 100:>5.0f}%{pct(s['pt']):>8}"
                f"{f(s['first']):>10}{f(s['second']):>10}")

    head = f"   {'':<26}{'n':>6}{'bps (t)':>16}{'win':>6}{'200x':>8}{'1st half':>10}{'2nd half':>10}"
    four = [x for x in rows if x["k"] == 4]
    L = [f"TWAP v2: SIZE AND TIMING  {hours:.0f} h · {', '.join(sorted(g))} · {len(found)} big native TWAPs "
         f"(median slice ${a.min_slice:,.0f}+, {len(found) / hours * 24:.1f} a day) · in {a.lag}s after the slice, "
         f"its way, at the mid", ""]
    L.append("1. ALL OF THEM, spotted at slice 4 (what the bot does now)")
    L.append(head)
    for hold, name in ((300, "out after 5 min"), (900, "out after 15 min"), (None, "out when it stops")):
        L.append(line(name, stat(four, hold)))
    L.append("")
    for title, key, buckets in (("2. BY HOW MUCH IS STILL TO COME (at slice 4)", "left_usd", REMAIN),
                                ("3. BY HOW LONG IT STILL RUNS (at slice 4)", "left_min", MINUTES),
                                ("4. BY ITS SHARE OF THE MARKET'S VOLUME while it runs (at slice 4)", "share", SHARE)):
        L.append(title + ", out when it stops")
        L.append(head)
        for lo, hi, name in buckets:
            L.append(line(name, stat([x for x in four if lo <= x[key] < hi], None)))
        L.append("")
    L.append("5. SPOTTED EARLIER: slice 2 vs slice 4, the same TWAPs (live, the API would confirm it's a TWAP at slice 2)")
    L.append(head)
    for k in (2, 4):
        sub = [x for x in rows if x["k"] == k]
        for hold, name in ((900, "15 min"), (None, "when it stops")):
            L.append(line(f"slice {k}, out {name}", stat(sub, hold)))
    L.append("")
    big = [x for x in four if x["share"] >= 0.02 and x["left_usd"] >= 250_000]
    L.append("6. THE COMBINATION: 2%+ of volume and $250k+ still to come")
    L.append(head)
    for hold, name in ((900, "out after 15 min"), (None, "out when it stops")):
        L.append(line(name, stat(big, hold)))
    L.append("")
    L.append("Worth building into the bot if the bigger groups in 2 to 4 beat the smaller ones in both halves. With "
             "this few TWAPs, treat any one row as a hint, and the live record (which logs the API's numbers) as the "
             "test.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

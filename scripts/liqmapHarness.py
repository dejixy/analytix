"""
Liquidation map test: does price get pulled toward clusters of liquidation prices (and cascade through them), or
pushed away? The one idea that gives a direction from structure rather than flow. Uses the positions the live
service collects (api/liqmap.py: the top 300 traders on the followed coins, every 10 minutes) and the fast store.

    python -m scripts.liqmapHarness --db data/positions.sqlite
    python -m scripts.liqmapHarness --band 1.0 --test-days 7

On the server, from the research copy (reads the positions file without touching it; the fast store must be up to
date, scripts.buildStore):
    cd ~/analytix && nice -n 19 /opt/analytix/.venv/bin/python -m scripts.liqmapHarness \\
        --db /opt/analytix/data/positions.sqlite > /tmp/liqmap.txt 2> /tmp/liqmap.log

The map, at each collection round
    when         the round's requests are spread over its first 5 minutes, so the map counts as known 5.5 minutes
                 after it starts: everything is measured from then, against the price then (no look-ahead)
    near         longs whose liquidation price is within the band below the price (they're forced to sell if it
                 falls) and shorts within the band above (forced to buy if it rises), by position value
    heavier      the side with more of it; a round counts as "big" when its heavier side is in the top third of that
                 coin's rounds on the earlier days

0b. The magnet test: in big rounds, is the edge of the band on the heavier side touched first (within 4 hours) more
    often than the other edge? Above 50% means price is pulled toward the liquidations.

The trades (through the profit harness, as scripts.profitHarness)
    toward 30m / 2h   in big rounds, trade toward the heavier side; held 30 minutes or 2 hours
    away 30m / 2h     trade away from it
    filters           all; not quiet; loud only (the move-size forecast)
"""
import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from config import POSITIONS_DB, STORE_DIR
from engine import volforecast
from engine.harness import DAY_MS, Market, Signal, day_t, one_at_a_time
from scripts.profitHarness import f, forecast_table, judge, pct, store_span
from storage import tickStore

KNOWN_AFTER_MS = 330_000
TOUCH_WITHIN_MIN = 240
HOLDS = (("30m", 1_800_000), ("2h", 7_200_000))
FILTERS = ("all", "not quiet", "loud only")


def load_rounds(db: Path, lo: int, hi: int) -> dict[str, list[tuple[int, np.ndarray, np.ndarray, np.ndarray]]]:
    """coin -> [(round start ms, side +1/-1, liquidation price, value $)], positions with a liquidation price."""
    rows = None
    for uri in (f"file:{db}?mode=ro", f"file:{db}?immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True)
            rows = con.execute("SELECT t_ms, coin, szi, liq_px, value_usd FROM positions WHERE t_ms >= ? AND t_ms < ? "
                               "AND liq_px IS NOT NULL AND liq_px > 0 AND value_usd > 0 ORDER BY t_ms", (lo, hi)).fetchall()
            con.close()
            break
        except sqlite3.Error as exc:
            err = exc
    if rows is None:
        sys.exit(f"couldn't read positions from {db}: {err}")
    groups: dict[tuple[str, int], list] = defaultdict(list)
    for t, coin, szi, liq, val in rows:
        groups[(coin, t)].append((1 if szi > 0 else -1, liq, val))
    out: dict[str, list] = defaultdict(list)
    for (coin, t), g in sorted(groups.items(), key=lambda kv: kv[0][1]):
        a = np.array(g, dtype=float)
        out[coin].append((t, a[:, 0], a[:, 1], a[:, 2]))
    return out


def near(price: float, side: np.ndarray, liq: np.ndarray, value: np.ndarray, band: float) -> tuple[float, float]:
    """($ of longs liquidated within band below, $ of shorts within band above), positions not yet past the price."""
    longs = (side > 0) & (liq < price) & (liq >= price * (1 - band))
    shorts = (side < 0) & (liq > price) & (liq <= price * (1 + band))
    return float(value[longs].sum()), float(value[shorts].sum())


def first_touch(fc, t_ms: int, price: float, band: float, within_min: int = TOUCH_WITHIN_MIN) -> int:
    """+1 if the band's upper edge is reached first (minute prices), -1 if the lower, 0 if neither within the time."""
    i0 = int(t_ms // volforecast.MINUTE) - fc.m0
    seg = fc.price[max(i0, 0):max(i0, 0) + within_min]
    up = np.flatnonzero(seg >= price * (1 + band))
    dn = np.flatnonzero(seg <= price * (1 - band))
    if not len(up) and not len(dn):
        return 0
    if not len(dn) or (len(up) and up[0] < dn[0]):
        return 1
    return -1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(POSITIONS_DB), help="the live service's positions.sqlite")
    ap.add_argument("--band", type=float, default=1.0, help="how near counts, % of the price")
    ap.add_argument("--test-days", type=float, default=7)
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--wait", type=float, default=30, help="seconds a limit order waits")
    args = ap.parse_args()
    band = args.band / 100
    notional = args.capital * args.leverage
    first_day, last_day, start, end, split = store_span(args.test_days)

    rounds = load_rounds(Path(args.db), start + DAY_MS, end - 7_200_000)
    coins = sorted(set(rounds) & set(tickStore.coins(STORE_DIR)))
    if not coins:
        sys.exit("no positions inside the store's days")
    print(f"loading {', '.join(coins)}...", file=sys.stderr)
    t0 = time.time()
    fc = {c: volforecast.fit(c, start, split, end) for c in coins}
    markets = {c: Market(c, start, end) for c in coins}
    print(f"  loaded in {time.time() - t0:.0f}s", file=sys.stderr)

    # the map at each round, measured when it's known
    maps = []                                                    # (coin, t known, price, longs $, shorts $, n positions)
    for c in coins:
        for t, side, liq, val in rounds[c]:
            tk = t + KNOWN_AFTER_MS
            q = markets[c].quote(tk)
            if q is None:
                continue
            p = (q[0] + q[1]) / 2
            lo_, hi_ = near(p, side, liq, val, band)
            maps.append((c, tk, p, lo_, hi_, len(side)))
    big_cut = {}
    for c in coins:
        heavier = [max(lo_, hi_) for cc, tk, p, lo_, hi_, n in maps if cc == c and tk <= split and max(lo_, hi_) > 0]
        big_cut[c] = float(np.percentile(heavier, 200 / 3)) if len(heavier) >= 30 else float("inf")

    first_round = min(tk for _, tk, *_ in maps)
    train_days = max((split - first_round) / DAY_MS, 1e-9)
    test_days = (end - split) / DAY_MS
    print(f"Liquidation map test · positions from {time.strftime('%d %b %H:%M', time.gmtime(first_round / 1000))} UTC · "
          f"store to {last_day} · locked test from {time.strftime('%d %b %H:%M', time.gmtime(split / 1000))} UTC "
          f"({test_days:.1f} days) · band ±{args.band:g}% · ${args.capital:g} at {args.leverage:g}x · 0.5 s delay")

    print(f"\n0. The map (rounds, positions per round, $ within ±{args.band:g}% of price: median / 'big' cut)")
    print(f"{'coin':<6}{'rounds':>8}{'positions':>11}{'longs near':>13}{'shorts near':>13}{'big cut':>12}")
    for c in coins:
        mm = [m for m in maps if m[0] == c]
        print(f"{c:<6}{len(mm):>8}{np.median([m[5] for m in mm]):>11.0f}{np.median([m[3] for m in mm]) / 1e3:>12.0f}k"
              f"{np.median([m[4] for m in mm]) / 1e3:>12.0f}k{big_cut[c] / 1e3:>11.0f}k")

    print(f"\n0b. The magnet test: in big rounds, which edge of ±{args.band:g}% is touched first within 4 hours")
    print(f"{'coin':<6}{'days':<8}{'big rounds':>11}{'heavier side first':>20}{'other side first':>18}{'neither':>9}"
          f"{'t (days)':>10}")
    for c in coins:
        for label, test in (("earlier", False), ("locked", True)):
            res, when = [], []
            for cc, tk, p, lo_, hi_, n in maps:
                if cc != c or (tk > split) != test or max(lo_, hi_) < big_cut[c] or lo_ == hi_:
                    continue
                toward = 1 if hi_ > lo_ else -1
                res.append(first_touch(fc[c], tk, p, band) * toward)
                when.append(tk)
            res, when = np.array(res), np.array(when, dtype=np.int64)
            if not len(res):
                continue
            hit = res != 0
            share = (res[hit] > 0).mean() if hit.any() else None
            t = day_t(when[hit], (res[hit] > 0) - 0.5) if hit.sum() > 2 else None
            print(f"{c:<6}{label:<8}{len(res):>11}{pct(share):>20}{pct(1 - share if share is not None else None):>18}"
                  f"{pct((res == 0).mean()):>9}{f(t):>10}")
    print("  Above 50% in 'heavier side first' means price is pulled toward the liquidations. Rounds 10 minutes apart\n"
          "  overlap, so t treats days as the independent blocks; both day sets must agree.")

    forecast_table(fc)

    sigs: dict[str, list[Signal]] = {}
    for hname, hold in HOLDS:
        tw, aw = [], []
        for c, tk, p, lo_, hi_, n in maps:
            if max(lo_, hi_) < big_cut[c] or lo_ == hi_:
                continue
            toward = 1 if hi_ > lo_ else -1
            tw.append(Signal(c, tk, toward, hold_ms=hold))
            aw.append(Signal(c, tk, -toward, hold_ms=hold))
        sigs[f"toward {hname}"] = one_at_a_time(tw)
        sigs[f"away {hname}"] = one_at_a_time(aw)
    sigs = {k: v for k, v in sigs.items() if v}
    if not sigs:
        sys.exit("no big rounds to trade")
    judge(sigs, markets, fc, split, train_days, test_days, notional, args.wait, filters=FILTERS, by_coin=True,
          capital=args.capital, leverage=args.leverage)


if __name__ == "__main__":
    main()

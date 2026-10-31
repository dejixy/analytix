"""
Profit harness: every trade idea we have, traded the way a bot on this server really could (engine/harness.py), with
and without the loud-market filter (engine/volforecast.py), ranked by net dollars a day on days it never saw.

    python -m scripts.profitHarness
    python -m scripts.profitHarness --test-days 7 --capital 100 --leverage 10

On the server, from the research copy (reads the live bot's records without touching them; the fast store must be
built up to date first with scripts.buildStore):
    cd ~/analytix && nice -n 19 /opt/analytix/.venv/bin/python -m scripts.profitHarness \\
        --db /opt/analytix/data/bars.sqlite > /tmp/harness.txt 2> /tmp/harness.log

The ideas
    TWAP 5m/15m/30m/end   follow every big TWAP the live bot spotted ($20k+ slices), from the moment it was spotted;
                          held 5, 15 or 30 minutes, or until the bot saw it stop
    fades (Degen)         the informed basket's fades that Degen takes: the other side of a hidden order from a
                          wallet whose pushes reliably reverse, held 15 minutes
    random                the same coins, sides and holds as TWAP 15m at random times: what costs alone do

How it's judged
    split        the newest --test-days are locked away; everything is chosen on the days before them
    styles       market both ways; market in, limit out; limit in (skip or chase), market out; limit both
    filters      all; not quiet; loud only (the move-size forecast's thirds, fitted on the earlier days)
    the pick     for each idea, the style and filter with the best net on the earlier days, then its result on the
                 locked days. Passes if net > 0 there with t >= 2 over days and both halves positive
    dollars      net bps x (capital x leverage) per trade, summed per day
"""
import argparse
import json
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from config import BARS_DB, STORE_DIR
from engine import volforecast
from engine.harness import DAY_MS, STYLES, Market, Signal, day_t, one_at_a_time, summary, trade
from storage import tickStore

TWAP_END_AFTER_MS = 35_000          # the bot calls a TWAP over this long after its last slice (api/twaptrack.py)
FADE_HOLD_MS = 900_000
FILTERS = ("all", "not quiet", "loud only")
MIN_TRAIN = 20


def records(db: Path, table: str) -> list[dict]:
    """Rows of one of the bot's tables, read-only (falls back to ignoring the write-ahead log if it must)."""
    for uri in (f"file:{db}?mode=ro", f"file:{db}?immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True)
            rows = [json.loads(d) for (d,) in con.execute(f"SELECT data FROM {table} ORDER BY t_ms")]
            con.close()
            return rows
        except sqlite3.Error as exc:
            last = exc
    print(f"  couldn't read {table} from {db}: {last}", file=sys.stderr)
    return []


def ideas(db: Path, lo: int, hi: int, seed: int = 7) -> dict[str, list[Signal]]:
    out: dict[str, list[Signal]] = {}
    tw = [r for r in records(db, "bot_twaps") if lo <= r["t_ms"] < hi]
    for label, hold in (("TWAP 5m", 300_000), ("TWAP 15m", 900_000), ("TWAP 30m", 1_800_000)):
        out[label] = one_at_a_time([Signal(r["coin"], r["t_ms"], r["side"], hold_ms=hold) for r in tw])
    out["TWAP until it stops"] = one_at_a_time([
        Signal(r["coin"], r["t_ms"], r["side"], hold_ms=60_000, exit_ms=r["end_ms"] + TWAP_END_AFTER_MS)
        for r in tw if r.get("end_ms")])
    fd = [r for r in records(db, "bot_fades") if lo <= r["t_ms"] < hi and r.get("fade")]
    out["fades (Degen)"] = one_at_a_time([Signal(r["coin"], r["t_ms"], r["side"], hold_ms=FADE_HOLD_MS) for r in fd])
    rng = random.Random(seed)
    base = out["TWAP 15m"]
    rand = [Signal(s.coin, rng.randrange(lo, hi - FADE_HOLD_MS), s.side, hold_ms=900_000) for s in base for _ in range(3)]
    out["random (control)"] = one_at_a_time(rand)
    return {k: v for k, v in out.items() if v}


def f(x, d=1, sign=True):
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(BARS_DB), help="the live bot's SQLite file (bars.sqlite)")
    ap.add_argument("--test-days", type=float, default=7)
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--wait", type=float, default=30, help="seconds a limit order waits")
    args = ap.parse_args()
    notional = args.capital * args.leverage

    coins_in_store = tickStore.coins(STORE_DIR)
    if not coins_in_store:
        sys.exit(f"no fast store in {STORE_DIR}: run scripts.buildStore first")
    first_day = min(tickStore.days(c, STORE_DIR)[0] for c in coins_in_store)
    last_day = max(tickStore.days(c, STORE_DIR)[-1] for c in coins_in_store)
    start = tickStore.day_end_ms(first_day) - DAY_MS
    end = tickStore.day_end_ms(last_day)
    split = end - int(args.test_days * DAY_MS)

    sigs = ideas(Path(args.db), start + DAY_MS, end - 3_600_000)     # a day of warm-up for the forecast
    if not sigs:
        sys.exit("no bot records in that range: check --db")
    coins = sorted({s.coin for v in sigs.values() for s in v} & set(coins_in_store))
    print(f"loading {', '.join(coins)}...", file=sys.stderr)
    t0 = time.time()
    markets = {c: Market(c, start, end) for c in coins}
    fc = {c: volforecast.fit(c, start, split, end) for c in coins}
    print(f"  loaded in {time.time() - t0:.0f}s", file=sys.stderr)

    first_sig = min(s.t_ms for v in sigs.values() for s in v)
    train_days = max((split - first_sig) / DAY_MS, 1e-9)
    test_days = (end - split) / DAY_MS
    print(f"Profit harness · store {first_day} to {last_day} · locked test from "
          f"{time.strftime('%d %b %H:%M', time.gmtime(split / 1000))} UTC ({test_days:.1f} days) · "
          f"${args.capital:g} at {args.leverage:g}x = ${notional:,.0f} a trade · 0.5 s delay · limit waits {args.wait:g}s")

    print("\n1. Move-size forecast on the locked days (next 30 min, bps): does it sort quiet from loud?")
    print(f"{'coin':<6}{'corr':>7}{'naive':>8}{'quiet':>14}{'middle':>14}{'loud':>14}")
    for c in coins:
        q = fc[c].quality if fc[c] else {}
        br = q.get("by_regime", {})
        cell = lambda r: f"{f(br[r][0], 1, False)} ({pct(br[r][1])})" if r in br and br[r][0] is not None else "-"
        print(f"{c:<6}{f(q.get('corr'), 2, False):>7}{f(q.get('naive_corr'), 2, False):>8}"
              f"{cell('quiet'):>14}{cell('middle'):>14}{cell('loud'):>14}")
    print("  corr: forecast vs what happened (logs); naive: 'same as the last 30 minutes'. Cells: average |move| "
          "(share of minutes)")

    # price every idea every way once
    priced: dict[tuple[str, str], list] = {}
    regime: dict[str, list] = {}
    for name, ss in sigs.items():
        regime[name] = [fc[s.coin].at(s.t_ms)[1] if fc.get(s.coin) else None for s in ss]
        for style in STYLES:
            priced[(name, style)] = [trade(markets[s.coin], s, style, args.wait * 1000) if s.coin in markets else None
                                     for s in ss]

    def pick(name, style, filt, test: bool):
        ss = sigs[name]
        keep = []
        for s, r, reg in zip(ss, priced[(name, style)], regime[name]):
            if (s.t_ms > split) != test:
                continue
            if filt == "not quiet" and reg not in ("middle", "loud"):
                continue
            if filt == "loud only" and reg != "loud":
                continue
            keep.append(r)
        return summary(keep, test_days if test else train_days, notional)

    print("\n2. Every idea, every way in and out, no filter (net bps a trade after fees; earlier days -> locked days)")
    print(f"{'idea':<22}{'style':<32}{'n':>6}{'filled':>8}{'gross':>8}{'net':>8}{'->':>4}{'n':>6}{'net':>8}{'t':>7}")
    for name in sigs:
        for style in STYLES:
            a, b = pick(name, style, "all", False), pick(name, style, "all", True)
            print(f"{name:<22}{style:<32}{a['trades']:>6}{pct(a['fill']):>8}{f(a['gross']):>8}{f(a['net']):>8}"
                  f"{'':>4}{b['trades']:>6}{f(b['net']):>8}{f(b['t']):>7}")

    print("\n3. The pick: best style and filter on the earlier days, judged on the locked days")
    print(f"{'idea':<22}{'chosen on earlier days':<44}{'n':>5}{'gross':>8}{'net':>8}{'t':>6}{'win':>6}"
          f"{'halves':>13}{'/day':>6}{'$/day':>8}")
    tried = 0
    verdicts = []
    for name in sigs:
        best = None
        for style in STYLES:
            for filt in FILTERS:
                tried += 1
                a = pick(name, style, filt, False)
                if a["trades"] >= MIN_TRAIN and a["net"] is not None and (best is None or a["net"] > best[2]["net"]):
                    best = (style, filt, a)
        if best is None:
            print(f"{name:<22}{'too few trades on the earlier days':<44}")
            continue
        style, filt, a = best
        b = pick(name, style, filt, True)
        ok = (b["net"] or -1) > 0 and (b["t"] or 0) >= 2 and (b["first"] or -1) > 0 and (b["second"] or -1) > 0
        verdicts.append((name, ok, b))
        halves = f"{f(b['first'])}/{f(b['second'])}"
        chosen = f"{style}, {filt} ({f(a['net'])})"
        print(f"{name:<22}{chosen:<44}{b['trades']:>5}"
              f"{f(b['gross']):>8}{f(b['net']):>8}{f(b['t']):>6}{pct(b['win']):>6}{halves:>13}"
              f"{f(b['per_day'], 1, False):>6}{f(b['usd_day'], 2):>8}")
    print(f"  ({tried} combinations tried on the earlier days; the locked days were used once, for the pick)")

    print("\n4. Verdict")
    passed = [v for v in verdicts if v[1]]
    if passed:
        for name, _, b in passed:
            print(f"  PASSES: {name}: {f(b['net'])} bps a trade after fees on the locked days, about "
                  f"{f(b['usd_day'], 2)} a day at ${args.capital:g} x {args.leverage:g}. Next: shadow it in the bot.")
    else:
        print("  Nothing passes on the locked days (net > 0, t >= 2 over days, both halves positive).")


if __name__ == "__main__":
    main()

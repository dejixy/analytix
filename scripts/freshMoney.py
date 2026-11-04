"""
Fresh money: does a burst of taker volume from wallets new to the recordings come before big moves?

    python -m scripts.freshMoney                         # measured on BTC, ETH, SOL, HYPE; wallets known from every
                                                         # coin in the fast store
    python -m scripts.freshMoney --coins BTC,ETH --out /tmp/fresh.json

On the server, from the research copy (a few minutes; the fast store must be built first, scripts/buildStore.py):
    cd ~/analytix && git pull && ANALYTIX_STORE_DIR=~/analytix-store nice -n 19 \\
        /opt/analytix/.venv/bin/python -m scripts.freshMoney > /tmp/fresh.txt && cat /tmp/fresh.txt

Decided before running (engine/freshmoney.py has the numbers):
    fresh       taker dollars from wallets first seen in the recordings under 24 h before the trade (on any coin, as
                taker or maker), after a 7-day burn-in; engine fills (liquidations) left out
    spike       a 60-minute window whose fresh share of taker dollars is in the top 1% of the 3 to 7 days before its
                day (thresholds walk forward a day at a time), with at least those days' median fresh dollars; one
                per coin per 4 hours; its direction is the fresh money's net (bought minus sold)
    outcomes    the move over the next 1 and 4 hours from the window's end: its size against every 5-minute start in
                the test days (the baseline), and the move in the spike's direction
    control     volume spikes: windows in the top 1% for taker dollars whose fresh share is at or under the median;
                if these do as well, fresh money adds nothing beyond "it got busy"
    passes if   a warning: 4h moves after spikes at least 1.25x the baseline, t >= 2, above 1x in both halves of the
                test days, and above the control's ratio. A trade: the 4h move in the spike's direction above
                9 bps (a taker round trip) with t >= 2 and above 0 in both halves. "Moved before" shows whether the
                fresh money came after a move (following it) rather than before one
"""
import argparse
import calendar
import json
import sys
import time
from pathlib import Path

import numpy as np

from config import STORE_DIR
from engine import freshmoney as F
from storage import tickStore

MAIN = ("BTC", "ETH", "SOL", "HYPE")
COLS = ("trade_t", "trade_px", "trade_sz", "trade_side", "trade_engine", "trade_buyer", "trade_seller")


def read_day(store: Path, coin: str, day: str) -> dict | None:
    """One stored day's trades, straight from its file (rows from either side of midnight included: they're summed
    into the right buckets later)."""
    path = Path(store) / coin / f"{day}.npz"
    if not path.exists():
        return None
    with np.load(path) as z:
        if "trade_t" not in z.files or not len(z["trade_t"]):
            return None
        out = {k: z[k] for k in COLS if k in z.files}
        out["wallets"] = z["wallets"] if "wallets" in z.files else np.zeros(0, dtype="<U42")
    out.setdefault("trade_engine", np.zeros(len(out["trade_t"]), dtype=bool))
    return out


def collect(store: Path, measure: list[str], seen_coins: list[str], log=sys.stderr):
    """Walk the store a day at a time: every coin's wallets go into Seen first, then the measured coins' taker
    trades are bucketed with their wallets' ages."""
    seen = F.Seen()
    per = {c: F.Coin() for c in measure}
    by_coin = {c: set(tickStore.days(c, store)) for c in seen_coins}
    all_days = sorted(set().union(*by_coin.values())) if by_coin else []
    t0 = time.time()
    for n, day in enumerate(all_days):
        loaded = {}
        for c in seen_coins:
            if day not in by_coin[c]:
                continue
            z = read_day(store, c, day)
            if z is None:
                continue
            gid = seen.gids(z["wallets"])
            to_g = lambda idx: np.where(idx >= 0, gid[np.clip(idx, 0, max(len(gid) - 1, 0))], -1) if len(gid) else np.full(len(idx), -1)
            buyer, seller = to_g(z["trade_buyer"].astype(np.int64)), to_g(z["trade_seller"].astype(np.int64))
            t = z["trade_t"].astype(np.int64)
            seen.update(buyer, t)
            seen.update(seller, t)
            if c in per:
                loaded[c] = (t, z, buyer, seller)
        for c, (t, z, buyer, seller) in loaded.items():
            side = np.sign(z["trade_side"]).astype(np.int64)
            taker = np.where(side > 0, buyer, seller)
            keep = (~z["trade_engine"].astype(bool)) & (taker >= 0) & (side != 0)
            order = np.argsort(t[keep], kind="stable")
            tt, tk = t[keep][order], taker[keep][order]
            px = z["trade_px"][keep][order].astype(float)
            usd = px * z["trade_sz"][keep][order].astype(float)
            per[c].add(tt, px, usd, side[keep][order], tk, seen.age(tk, tt))
        if log and (n % 5 == 4 or n == len(all_days) - 1):
            print(f"  {day}: {n + 1}/{len(all_days)} days, {len(seen.ids):,} wallets, {time.time() - t0:.0f}s", file=log)
    first_ms = calendar.timegm(time.strptime(all_days[0], "%Y%m%d")) * 1000 if all_days else None
    return per, seen, first_ms, all_days


def fmt_row(name: str, sc: dict, extra: str = "") -> str:
    def f(x, spec):
        return "   –" if x is None or (isinstance(x, float) and np.isnan(x)) else format(x, spec)
    h1, h4 = sc["1h"], sc["4h"]
    return (f"  {name:<8}{sc['n']:>4}   {f(h4['ratio'], '5.2f')}x ({f(h4['t_abs'], '+5.1f')})   "
            f"{f(h4['signed'], '+6.1f')} ({f(h4['t_signed'], '+5.1f')}) {f(h4['win'] * 100 if h4['win'] == h4['win'] else None, '3.0f')}%   "
            f"{f(h1['ratio'], '5.2f')}x  {f(h1['signed'], '+6.1f')}   {f(sc['before_ratio'], '5.2f')}x{extra}")


HEAD = ("          n   4h size vs normal (t)   4h with the spike bps (t) win   1h size  1h bps   moved before")


def pooled(parts: list[tuple[dict, np.ndarray, np.ndarray, np.ndarray]]) -> dict:
    """All coins together: each spike's move relative to its own coin's baseline, then averaged."""
    out = {"n": int(sum(len(i) for _, i, _, _ in parts))}
    for name, h in F.HORIZONS.items():
        rel, signed = [], []
        for g, idx, d, test in parts:
            base = np.nanmean(np.abs(F.moves(g["px"], np.flatnonzero(test), h))) if test.any() else np.nan
            ev = F.moves(g["px"], idx, h)
            rel.append(np.abs(ev) / base)
            signed.append(d * ev)
        rel, signed = (np.concatenate(x) if x else np.zeros(0) for x in (rel, signed))
        out[name] = {"ratio": float(np.nanmean(rel)) if len(rel) else float("nan"), "t_abs": F._t(rel - 1),
                     "signed": float(np.nanmean(signed)) if len(signed) else float("nan"), "t_signed": F._t(signed),
                     "win": float(np.nanmean(signed > 0)) if len(signed) else float("nan")}
    before = []
    for g, idx, _, test in parts:
        base = np.nanmean(np.abs(F.moves(g["px"], np.flatnonzero(test), -F.WINDOW))) if test.any() else np.nan
        before.append(np.abs(F.moves(g["px"], idx, -F.WINDOW)) / base)
    b = np.concatenate(before) if before else np.zeros(0)
    out["before_ratio"] = float(np.nanmean(b)) if len(b) else float("nan")
    return out


def halves(g: dict, idx: np.ndarray, d: np.ndarray, test: np.ndarray):
    """The test days split at their middle: (first, second), each with its own spikes and baseline."""
    tk = np.flatnonzero(test)
    if not len(tk):
        return None
    mid = tk[len(tk) // 2]
    first, second = np.arange(len(test)) < mid, np.arange(len(test)) >= mid
    return [(g, idx[m(idx)], d[m(idx)], test & part) for part, m in
            ((first, lambda i: i < mid), (second, lambda i: i >= mid))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default=",".join(MAIN), help="coins measured")
    ap.add_argument("--seen-coins", default="all", help="coins whose wallets count as known: all (every coin in the "
                                                         "store) or a list")
    ap.add_argument("--store", default=str(STORE_DIR))
    ap.add_argument("--out", default="/tmp/fresh_money.json", help="results as JSON (for the terminal later)")
    args = ap.parse_args()
    store = Path(args.store)
    have = tickStore.coins(store)
    measure = [c for c in args.coins.split(",") if c in have]
    if not measure:
        sys.exit(f"none of {args.coins} in the fast store at {store}: run scripts.buildStore first")
    seen_coins = have if args.seen_coins == "all" else sorted(set(args.seen_coins.split(",")) | set(measure))
    print(f"reading {len(seen_coins)} coins' wallets, measuring {', '.join(measure)}...", file=sys.stderr)
    per, seen, first_ms, all_days = collect(store, measure, seen_coins)
    burn_k = (first_ms + F.BURN_DAYS * F.DAY_MS) // F.STEP_MS
    day = lambda k: time.strftime("%d %b", time.gmtime(k * F.STEP_MS / 1000))

    results, parts_f, parts_v, rows_f, rows_v, daily = {}, [], [], [], [], {}
    for c in measure:
        g = per[c].grid()
        if len(g["k"]) < F.WINDOW * 3:
            continue
        idx_f, test = F.spikes(g, burn_k, kind="fresh")
        idx_v, test_v = F.spikes(g, burn_k, kind="volume")
        d_f = np.sign(F.roll(g["fnet"])[idx_f])
        d_v = np.sign(F.roll(g["net"])[idx_v])
        sc_f, sc_v = F.score(g, idx_f, d_f, test), F.score(g, idx_v, d_v, test_v)
        fr = per[c].fresh_trades()
        conc = [F.concentration(fr, (g["k"][i] - F.WINDOW + 1) * F.STEP_MS, (g["k"][i] + 1) * F.STEP_MS) for i in idx_f]
        conc = [x for x in conc if x is not None]
        top = float(np.median(conc)) if conc else float("nan")
        rows_f.append(fmt_row(c, sc_f, f"   {top * 100:3.0f}%" if conc else "     –"))
        rows_v.append(fmt_row(c, sc_v))
        parts_f.append((g, idx_f, d_f, test))
        parts_v.append((g, idx_v, d_v, test_v))
        # the fresh share by day, after the burn-in: a sanity line (it should settle, not keep falling)
        per_day = F.DAY_MS // F.STEP_MS
        dd = g["k"] // per_day
        daily[c] = {time.strftime("%Y-%m-%d", time.gmtime(int(x) * F.DAY_MS / 1000)):
                    round(float(g["fresh"][dd == x].sum() / max(g["total"][dd == x].sum(), 1)), 4)
                    for x in np.unique(dd) if x * per_day >= burn_k}
        results[c] = {"fresh": sc_f, "volume": sc_v, "top_wallet_share": top,
                      "spikes": [{"t_ms": int((g["k"][i] + 1) * F.STEP_MS), "dir": int(dd_), "share":
                                  round(float(F.roll(g["fresh"])[i] / max(F.roll(g["total"])[i], 1)), 4),
                                  "fresh_usd": round(float(F.roll(g["fresh"])[i]))}
                                 for i, dd_ in zip(idx_f, d_f)],
                      "test_from": day(int(np.flatnonzero(test)[0]) + g["k"][0]) if test.any() else None}
    if not parts_f:
        sys.exit("not enough data after the burn-in")

    test_days = [r["test_from"] for r in results.values() if r["test_from"]]
    print(f"Fresh money · {len(all_days)} days in the store ({all_days[0]} to {all_days[-1]}) · {len(seen.ids):,} wallets "
          f"from {len(seen_coins)} coins · burn-in {F.BURN_DAYS} days · tests from {min(test_days) if test_days else '–'}")
    print("\nFresh share of taker dollars by day (after the burn-in):")
    days_all = sorted(set().union(*[d.keys() for d in daily.values()]))
    print("  " + "   ".join(f"{c:>10}" for c in daily) + "    day")
    for dstr in days_all[-21:]:
        print("  " + "   ".join(f"{daily[c].get(dstr, float('nan')) * 100:9.1f}%" for c in daily) + f"    {dstr}")

    print("\n1. Fresh-money spikes (the test). Sizes are against every 5-minute start in the test days; bps are the move")
    print("   in the fresh money's direction; 'moved before' is the hour before the spike against normal; the last")
    print("   column is the median share of a spike's fresh dollars from its single biggest wallet")
    print(HEAD + "   top wallet")
    for r in rows_f:
        print(r)
    pf = pooled(parts_f)
    print(fmt_row("all", pf))
    hv = [halves(*p) for p in parts_f]
    h1 = pooled([h[0] for h in hv if h]); h2 = pooled([h[1] for h in hv if h])
    print(fmt_row("1st half", h1))
    print(fmt_row("2nd half", h2))

    print("\n2. The control: volume spikes without fresh money")
    print(HEAD)
    for r in rows_v:
        print(r)
    pv = pooled(parts_v)
    print(fmt_row("all", pv))

    warn = (pf["4h"]["ratio"] >= 1.25 and pf["4h"]["t_abs"] >= 2 and h1["4h"]["ratio"] > 1 and h2["4h"]["ratio"] > 1
            and pf["4h"]["ratio"] > pv["4h"]["ratio"])
    trade = (pf["4h"]["signed"] > 9 and pf["4h"]["t_signed"] >= 2 and h1["4h"]["signed"] > 0 and h2["4h"]["signed"] > 0)
    print("\nVerdict (decided before running):")
    print(f"  as a warning of big moves: {'PASSES' if warn else 'fails'} "
          f"(4h {pf['4h']['ratio']:.2f}x, t {pf['4h']['t_abs']:+.1f}; halves {h1['4h']['ratio']:.2f}x / "
          f"{h2['4h']['ratio']:.2f}x; control {pv['4h']['ratio']:.2f}x)")
    print(f"  as a trade in its direction: {'PASSES' if trade else 'fails'} "
          f"(4h {pf['4h']['signed']:+.1f} bps, t {pf['4h']['t_signed']:+.1f}; halves {h1['4h']['signed']:+.1f} / "
          f"{h2['4h']['signed']:+.1f}; needs > 9 bps)")
    if pf["before_ratio"] > 1.5:
        print(f"  note: the hour before a spike moved {pf['before_ratio']:.1f}x normal, so fresh money mostly arrives "
              f"after a move")
    Path(args.out).write_text(json.dumps({"coins": results, "pooled": {"fresh": pf, "volume": pv, "halves": [h1, h2]},
                                          "daily_share": daily, "verdict": {"warning": warn, "trade": trade}},
                                         default=float, indent=1))
    print(f"\n(results in {args.out})", file=sys.stderr)


if __name__ == "__main__":
    main()

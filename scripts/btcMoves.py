"""
BTC estimate v2: how big BTC's next day will be, and what follows the most extreme ETF-flow days and Strategy's
biggest buys (engine/btcsize.py has the rules, fixed before the first run).

    python -m scripts.btcMoves                 # the size forecast on all but the held-back last 20%, and the events
    python -m scripts.btcMoves --final         # once, at the end: the size forecast's held-back days too

On the server:  bash scripts/run.sh moves     (prints it and keeps it in /tmp/btc_moves.txt)

It reads the same data as the BTC estimate (scripts/btcEstimate.py, same cache folder) plus Deribit's DVOL.
"""
import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

from engine import btcmodel as M
from engine import btcsize as S
from ingestion import flows as F
from scripts.btcEstimate import gather

WHEN = lambda ms: time.strftime("%a %d %b %H:%M UTC", time.gmtime(ms / 1000))
DAY = lambda ms: time.strftime("%d %b %Y", time.gmtime(ms / 1000))


def size_line(name: str, s: dict) -> str:
    if s.get("n", 0) < 20:
        return f"  {name:<30}{s.get('n', 0):>5}   too few"
    gain = f"{s['gain']:>+9.3f} ({s['t_gain']:+4.1f})" if "gain" in s else f"{'':>16}"
    return f"  {name:<30}{s['n']:>5}{s['corr']:>8.2f}{s['r2']:>+10.2f}{gain}"


def pct(x, nd=2) -> str:
    return "–" if x is None else f"{x:+.{nd}f}%"


def event_table(res_by_side: dict, base: dict) -> list[str]:
    heads = [f"{h // 24} day{'s' if h > 24 else ''}{'*' if h == S.PRIMARY_H else ''}" for h in S.HORIZONS]
    out = [f"  {'':<10}{'events':>7}" + "".join(f"{x:>16}" for x in heads)]
    for side, (n_events, res) in res_by_side.items():
        cells = []
        for h in S.HORIZONS:
            s = res.get(h, {})
            cells.append(f"{'':>15}" if s.get("n", 0) < 4 else f"{s['excess']:>+7.2f}% ({s['t']:+4.1f})")
        out.append(f"  {side:<10}{n_events:>7}" + "".join(f"{c:>16}" for c in cells))
    out.append(f"  {'all days':<10}{base[S.HORIZONS[0]]['n']:>7}" + "".join(
        f"{('avg ' + pct(base[h]['mean'])) if h in base else '':>16}" for h in S.HORIZONS))
    return out


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--final", action="store_true", help="also score the size forecast's held-back days (do this once)")
    ap.add_argument("--etf-file", default="")
    ap.add_argument("--dat-csv", default="")
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--no-book", action="store_true")
    ap.add_argument("--cache", default=str(Path.home() / "btc-flows"))
    ap.add_argument("--out", default="/tmp/btc_moves.json")
    args = ap.parse_args()
    d = asyncio.run(gather(args))
    if not d["etf"]:
        sys.exit("no ETF flows: TFTC and Farside both failed (see the log)")
    rows = M.build_rows(d["etf"], d["dat"], d["btc"], d["mstr"], d["book"])
    start = rows[0]["t"] - 2 * M.DAY if rows else M.ms_of("2024-01-01")
    dvol = asyncio.run(F.fetch_dvol(start, Path(args.cache), sys.stderr))
    srows = S.size_rows(rows, d["btc"], dvol)
    e, b, m, dat = d["etf"], d["btc"], d["mstr"], d["dat"]
    print("Data")
    print(f"  ETF flows      {len(e):>6} US days" + (f", {e[0]['date']} to {e[-1]['date']} ({d['etf_src']})" if e else ""))
    print(f"  BTC prices     {len(b):>6} hours" + (f", {DAY(b[0][0])} to {DAY(b[-1][0])} ({d['btc_src']})" if b else ""))
    print(f"  Strategy price {len(m):>6} days" + (f" ({d['mstr_src']})" if m else " (premium term left out)"))
    print(f"  Strategy buys  {len(dat):>6} filings" + (f", {dat[0]['filed']} to {dat[-1]['filed']}" if dat else ""))
    print(f"  Options DVOL   {len(dvol):>6} hours" + (f", {DAY(dvol[0][0])} to {DAY(dvol[-1][0])} (Deribit)" if dvol else
                                                     " (Deribit not reachable: the options model is left out)"))
    print(f"  Order book     {len(d['book']):>6} hours from our recordings\n")

    # ── 1. size ──
    flows = tuple(k for k in S.FLOWS if m or k != "prem_abs")
    opts = S.OPTIONS if dvol else ()
    every = S.OWN + opts + flows
    done = S.complete(srows, every)
    tune, held = M.split(done, args.final)
    min_train = max(60, min(M.MIN_TRAIN, len(tune) // 2))
    models = [("Own past (HAR)", S.OWN)] + ([("Own past + options", S.OWN + opts)] if opts else []) + \
             [("Own past + flows", S.OWN + flows)] + ([("Own past + options + flows", every)] if opts else [])
    best_without = S.OWN + opts
    with_flows = best_without + flows
    print(f"1. How big will BTC's next {S.SIZE_H} h be?   from {M.ENTRY_H}:00 UTC the day after each US flow day · "
          f"{len(done)} days ({done[0]['day'] if done else '–'} to {done[-1]['day'] if done else '–'}), "
          f"{len(held) if args.final else int(len(done) * M.HOLD_FRAC)} held back")
    print(f"   walk-forward, out of sample (first fit on {min_train} days, refit every {S.REFIT_EVERY}); scored on log "
          "realised vol; R² against the plain average; gain = squared error saved against own past alone")
    print(f"  {'model':<30}{'days':>5}{'corr':>8}{'R² vs avg':>10}{'gain (t)':>16}")
    naive = S.walk(tune, (), min_train)
    preds, results, prev = {}, {}, None
    for name, terms in models:
        p = S.walk(tune, terms, min_train)
        preds[terms] = p
        s = S.size_scores(p, naive, preds[S.OWN] if terms != S.OWN else None)
        results[name] = s
        print(size_line(name, s))
    pf, pb = preds.get(with_flows, []), preds.get(best_without, [])
    h1, h2 = S.size_halves(pf, naive, pb)
    whole = S.size_scores(pf, naive, pb)
    print(f"  the test: {'own past + options' if opts else 'own past'} with the flows against without them")
    print(size_line("  all days", whole))
    print(size_line("  first half", h1))
    print(size_line("  second half", h2))
    flows_pass = whole.get("t_gain", 0) >= 2 and h1.get("gain", 0) > 0 and h2.get("gain", 0) > 0
    own = results.get("Own past (HAR)", {})
    print(f"\n  The flows {'add to' if flows_pass else 'do not add to'} the forecast"
          + (" (t >= 2, both halves)." if flows_pass else f" (needs t >= 2 and both halves; got t {whole.get('t_gain', 0):+.1f})."))
    if own.get("n", 0) >= 20:
        print(f"  BTC's own past alone: correlation {own['corr']:.2f} with the next day's realised vol "
              f"({'a usable size forecast' if own['corr'] >= 0.3 else 'weak'}).")
    chosen = with_flows if flows_pass else best_without
    pc = preds.get(chosen, [])
    q = S.quintiles(pc)
    if q:
        print(f"\n  Days sorted into fifths by the forecast ({'with' if flows_pass else 'without'} flows), out of sample:")
        print(f"  {'fifth':<12}{'days':>5}{'forecast':>10}{'realised':>10}{'avg |move|':>12}")
        for k, x in enumerate(q):
            print(f"  {['quietest', 'quiet', 'middle', 'busy', 'wildest'][k]:<12}{x['n']:>5}{x['forecast']:>9.2f}%"
                  f"{x['rv']:>9.2f}%{x['move']:>11.2f}%")
        if q[0]["move"]:
            print(f"  the wildest fifth moved {q[-1]['move'] / q[0]['move']:.1f}× the quietest fifth")
        cov = S.coverage(pc)
        if cov is not None:
            print(f"  BTC stayed within ± the forecast on {cov:.0%} of days (a fair one-sd forecast: about 68%)")
    if args.final and held:
        allp = S.walk(done, chosen, min_train)
        alln = S.walk(done, (), min_train)
        pick = [i for i, p in enumerate(allp) if p["row"] in held]
        print("\n   held back (seen once):")
        print(size_line("  held-back days", S.size_scores([allp[i] for i in pick], [alln[i] for i in pick])))
    now = S.size_now(srows, chosen)
    if now:
        normal = f"; its 30-day normal is {now['normal']:.2f}%" if now["normal"] else ""
        print(f"\n  Now: from {WHEN(now['t'])}, BTC's next {S.SIZE_H} h: about ±{now['est']:.2f}% (one sd){normal}; "
              f"bigger than {now['rank']:.0%} of the last year's forecasts"
              + (f" (that window is over: it was {now['actual']:.2f}%)" if now["finished"] else ""))
        top = sorted(now["parts"].items(), key=lambda kv: -abs(kv[1]))[:3]
        print("  biggest pushes: " + ", ".join(f"{S.NAMES[k]} {v:+.2f}" for k, v in top) + "   (log points)")

    # ── 2. extreme ETF-flow days ──
    px = M.Series(d["btc"])
    base = S.baseline(rows, px)
    ev = S.flow_events(rows)
    sides = {"inflow": [x for x in ev if x["side"] == 1], "outflow": [x for x in ev if x["side"] == -1]}
    res = {k: S.event_test([x["row"]["t"] for x in v], px, base) for k, v in sides.items()}
    print(f"\n2. Extreme ETF-flow days: bigger (inflow) or smaller (outflow) than {1 - S.EVENT_Q:.0%} of the "
          f"{S.EVENT_WINDOW} US days before; a run within {S.EVENT_GAP} days is one event")
    print(f"   BTC's move from {M.ENTRY_H}:00 UTC the next day, above the average day; (t) by event; "
          f"* = the pre-registered test")
    for line in event_table({k: (len(v), res[k]) for k, v in sides.items()}, base):
        print(line)
    verdicts = {}
    for k, r in res.items():
        s = r.get(S.PRIMARY_H, {})
        verdicts[k] = S.passes(r)
        if s.get("n", 0) >= 4:
            sz = r.get(24, {}).get("size")
            print(f"  {k}: 3 days first half {pct(s['first'])}, second half {pct(s['second'])}, above average on "
                  f"{s['above']:.0%}" + (f"; the next day's move was {sz:.2f}× an ordinary day's size" if sz else ""))
    for k, v in sides.items():
        if v:
            last = v[-1]
            print(f"  latest {k} event: {last['row']['day']} (${last['flow']:,.0f}m)")
    print("  " + "; ".join(f"{k}: {'passes' if ok else 'fails'}" for k, ok in verdicts.items())
          + f" (3 days: |t| >= 2, same sign both halves, beyond {S.COST_PCT * 100:.0f} bps)")

    # ── 3. Strategy's biggest buys ──
    dev = S.dat_events(dat, d["btc"])
    dres = S.event_test([x["t"] for x in dev], px, base)
    print(f"\n3. Strategy's biggest buys: a filing whose dollar purchase beats {S.DAT_Q:.0%} of the {S.DAT_WINDOW} buys "
          f"before it (from {M.ENTRY_H}:00 UTC on the filing day)")
    for line in event_table({"big buy": (len(dev), dres)}, base):
        print(line)
    ds = dres.get(S.PRIMARY_H, {})
    if ds.get("n", 0) >= 4:
        print(f"  3 days first half {pct(ds['first'])}, second half {pct(ds['second'])}; "
              + ("passes" if S.passes(dres) else "fails") + " the pre-registered test")
    if dev:
        print(f"  latest: {dev[-1]['day']} (${dev[-1]['usd'] / 1e6:,.0f}m)")
    elif len(dat) < S.DAT_MIN + 5:
        print(f"  only {len(dat)} filings read: too few for this test")
    print("\n  The event rules were fixed before this run: changing them now would need new days to test on.")
    Path(args.out).write_text(json.dumps({
        "size": {"models": results, "halves": [h1, h2], "flows_pass": flows_pass, "quintiles": q, "now": now},
        "flow_events": {k: res[k] for k in res}, "flow_pass": verdicts, "dat_events": dres,
        "latest": {k: (v[-1]["row"]["day"] if v else None) for k, v in sides.items()}}, default=float, indent=1))
    print(f"(saved to {args.out})", file=sys.stderr)


if __name__ == "__main__":
    main()

"""
The BTC estimate: ETF flows, treasury-company buying, Strategy's premium and the order book in one equation for
BTC's next 24 hours, fitted walk-forward (engine/btcmodel.py has the terms, the timing and the fit).

    python -m scripts.btcEstimate                      # tune: every row but the held-back last 20%
    python -m scripts.btcEstimate --lam 0.3 --terms etf,etf5,mom      # try a variation
    python -m scripts.btcEstimate --final              # once, when the tuning is done: the held-back rows too

On the server (a few minutes; reads the fast store for the order-book terms):
    cd ~/analytix && git pull && ANALYTIX_STORE_DIR=~/analytix-store nice -n 19 \\
        /opt/analytix/.venv/bin/python -m scripts.btcEstimate > /tmp/btc_est.txt 2> /tmp/btc_est.log; cat /tmp/btc_est.txt

If Farside turns the script away, save https://farside.co.uk/btc/ from a browser (Ctrl+S) and pass --etf-file.
If the SEC refuses, set ANALYTIX_SEC_UA="your name your@email" (the SEC asks scripts to say who they are).
Other treasury companies: --dat-csv with columns date,company,btc (the day each purchase was announced).
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

from config import STORE_DIR
from engine import btcmodel as M
from ingestion import flows as F
from ingestion.backfill import fetch_candles

MODELS = [("BTC's own move only", ("mom",)), ("ETF flows only", ("etf", "etf5")),
          ("Slow money", ("etf", "etf5", "dat", "prem", "mom")), ("Slow money + order book", M.TERMS)]


def book_hours(coin: str = "BTC", store: Path = STORE_DIR, near: float = 0.0025, log=sys.stderr) -> dict[int, dict]:
    """Hourly taker net dollars and the bid/ask depth lean within `near` of the mid, from the fast store (each day's
    file read once, only the columns needed)."""
    from storage import tickStore
    out: dict[int, dict] = {}
    for day in tickStore.days(coin, store):
        with np.load(Path(store) / coin / f"{day}.npz") as z:
            cols = {k: z[k] for k in ("trade_t", "trade_px", "trade_sz", "trade_side", "book_t", "bid_px", "bid_sz",
                                      "ask_px", "ask_sz") if k in z.files}
        if len(cols.get("trade_t", ())):
            h = (cols["trade_t"] // M.HOUR * M.HOUR).astype(np.int64)
            uh, inv = np.unique(h, return_inverse=True)
            net = np.bincount(inv, weights=cols["trade_side"] * cols["trade_px"] * cols["trade_sz"])
            for k, v in zip(uh, net):
                out.setdefault(int(k), {"taker": 0.0, "imb": 0.0})["taker"] += float(v)
        if len(cols.get("book_t", ())):
            bp, bs = np.nan_to_num(cols["bid_px"]), np.nan_to_num(cols["bid_sz"])
            ap, as_ = np.nan_to_num(cols["ask_px"]), np.nan_to_num(cols["ask_sz"])
            mid = (bp[:, 0] + ap[:, 0]) / 2
            ok = (bp[:, 0] > 0) & (ap[:, 0] > 0)
            B = (bp * bs * (bp >= (mid * (1 - near))[:, None])).sum(1)
            A = (ap * as_ * ((ap <= (mid * (1 + near))[:, None]) & (ap > 0))).sum(1)
            imb = np.where(ok & (A + B > 0), (B - A) / np.maximum(A + B, 1e-9), np.nan)
            h = (cols["book_t"] // M.HOUR * M.HOUR).astype(np.int64)
            good = ~np.isnan(imb)
            uh, inv = np.unique(h[good], return_inverse=True)
            sums = np.bincount(inv, weights=imb[good])
            cnt = np.bincount(inv)
            for k, sm, c in zip(uh, sums, cnt):
                out.setdefault(int(k), {"taker": 0.0, "imb": 0.0})["imb"] = float(sm / c)
        if log:
            print(f"  book {day}: {len(cols.get('trade_t', ())):,} trades, {len(cols.get('book_t', ())):,} books", file=log)
    return out


def row_of(name: str, s: dict, extra: str = "") -> str:
    if s.get("n", 0) < 10:
        return f"  {name:<26}{s.get('n', 0):>5}   too few"
    return (f"  {name:<26}{s['n']:>5}{s['ic']:>+8.3f} ({s['t_ic']:+4.1f}){s['hit']:>8.0%}{s['r2']:>+9.3f}"
            f"{s['follow_bps']:>+9.1f}{extra}")


HEAD = f"  {'model':<26}{'days':>5}{'corr (t)':>15}{'sign':>8}{'R² vs 0':>9}{'bps':>9}"


async def gather(args, log=sys.stderr) -> dict:
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    if args.etf_file:
        etf = F.read_etf_file(args.etf_file)
    else:
        html = await F.fetch_farside()
        (cache / "farside.html").write_text(html)
        etf = F.parse_farside(html)
    print(f"ETF flows: {len(etf)} days, {etf[0]['date'] if etf else '–'} to {etf[-1]['date'] if etf else '–'}", file=log)
    dat = []
    try:
        dat = await F.fetch_strategy(args.since, cache=cache / "strategy_8k.json", log=log)
    except Exception as exc:                                      # noqa: BLE001 - the rest still runs
        print(f"Strategy 8-Ks: not read ({type(exc).__name__}: {exc})", file=log)
    if args.dat_csv:
        dat += F.read_dat_csv(args.dat_csv)
    try:
        btc = [(t + M.HOUR, c) for t, c in await F.fetch_yahoo("BTC-USD", "730d", "1h")]   # stamped at the bar's open
    except Exception as exc:                                      # noqa: BLE001
        print(f"Yahoo BTC hourly: {exc}; using Hyperliquid's hourly candles", file=log)
        btc = []
    hl = await fetch_candles("BTC", "1h", 5000)
    seen = {t for t, _ in btc}
    btc += [(int(r["t"]) + M.HOUR, float(r["c"])) for r in hl if r.get("c") and int(r["t"]) + M.HOUR not in seen]
    mstr = await F.fetch_yahoo("MSTR", "5y", "1d")
    mstr = [(t + 7 * M.HOUR + 30 * 60_000, c) for t, c in mstr]      # stamped at the open; known by the close, 6.5 h on (7.5 to be safe)
    book = {} if args.no_book else book_hours(log=log)
    return {"etf": etf, "dat": dat, "btc": sorted(btc), "mstr": mstr, "book": book}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lam", type=float, default=0.1, help="ridge shrinkage (more = coefficients nearer zero)")
    ap.add_argument("--terms", default=",".join(M.TERMS))
    ap.add_argument("--horizon", type=int, default=M.HORIZON_H)
    ap.add_argument("--final", action="store_true", help="also score the held-back rows (do this once)")
    ap.add_argument("--etf-file", default="")
    ap.add_argument("--dat-csv", default="")
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--no-book", action="store_true")
    ap.add_argument("--cache", default=str(Path.home() / "btc-flows"))
    ap.add_argument("--out", default="/tmp/btc_estimate.json")
    args = ap.parse_args()
    terms = tuple(t for t in args.terms.split(",") if t in M.TERMS)
    d = asyncio.run(gather(args))
    if not d["etf"]:
        sys.exit("no ETF flows: save https://farside.co.uk/btc/ and pass --etf-file")
    rows = M.build_rows(d["etf"], d["dat"], d["btc"], d["mstr"], d["book"], horizon_h=args.horizon)
    done = M.usable(rows, [t for t in terms if t not in ("taker", "book")])
    tune, held = M.split(done, args.final)
    book_days = sum(1 for r in done if r["z"].get("taker") is not None)
    print(f"BTC estimate · rows measured at {M.ENTRY_H}:00 UTC the day after each US flow day, move over the next "
          f"{args.horizon} h · {len(done)} usable rows ({done[0]['day'] if done else '–'} to {done[-1]['day'] if done else '–'}), "
          f"{len(held) if args.final else int(len(done) * M.HOLD_FRAC)} held back · order book on {book_days} of them")
    print(f"  treasury buying: {len(d['dat'])} purchases read; Strategy's latest: "
          + (f"{d['dat'][-1]['filed']} bought {d['dat'][-1]['bought']:,.0f} BTC" if d["dat"] else "–"))
    rc = M.reactive(d["etf"], d["btc"])
    print("\n1. Do ETF flows follow BTC? Correlation of a day's flow with BTC's move over US hours:")
    print("  " + "   ".join(f"{k}: {v:+.2f}" for k, v in rc.items()) + "   (big same-day or day-before numbers: flows chase price)")
    print("\n2. Each term alone against the move that followed (all usable rows; a first look, not a test)")
    for k, v in M.univariate(done).items():
        print(f"  {M.NAMES[k]:<26}{v['n']:>5} days   r {v['r']:+.3f}")
    print(f"\n3. Walk-forward, out of sample (refit every {M.REFIT_EVERY} rows; ridge {args.lam:g}; "
          "bps = the average move in the estimate's direction)")
    print(HEAD)
    results = {}
    for name, ts in MODELS + ([("Your terms", terms)] if terms not in [m[1] for m in MODELS] else []):
        p = M.walk_forward(tune, ts, args.lam, horizon_h=args.horizon)
        s = M.scores(p)
        results[name] = s
        print(row_of(name, s))
    p_all = M.walk_forward(tune, terms, args.lam, horizon_h=args.horizon)
    h1, h2 = M.halves(p_all)
    print(row_of("  first half", h1))
    print(row_of("  second half", h2))
    if args.final and held:
        p_final = [x for x in M.walk_forward(done, terms, args.lam, horizon_h=args.horizon) if x["row"] in held]
        print("\n   held back (seen once):")
        print(row_of("  held-back rows", M.scores(p_final)))
    sd = M.scores(p_all).get("resid_sd")
    now = M.estimate_now([r for r in rows if r["z"].get("etf") is not None], terms, args.lam, sd)
    if now:
        when = time.strftime("%a %d %b %H:%M UTC", time.gmtime(now["t"] / 1000))
        print(f"\n4. The equation (fitted on all {now['n']} finished rows; each term is a z-score, see engine/btcmodel.py)")
        print("  " + now["equation"])
        print(f"\n5. Now: from {when}, BTC's next {args.horizon} h")
        print(f"  estimate {now['est']:+.2f}%   (68% band ±{now['band']:.2f}%, chance up {now['p_up']:.0%})")
        for k in terms:
            z = now["z"].get(k)
            print(f"  {M.NAMES[k]:<26}{'–' if z is None else f'{z:+.2f}σ':>8}  →{now['parts'][k]:+.3f}%   "
                  f"(raw {now['raw'][k] if now['raw'][k] is None else round(now['raw'][k], 3)})")
        verdict = M.scores(p_all)
        good = verdict.get("n", 0) >= 60 and verdict.get("t_ic", 0) >= 2 and h1.get("ic", 0) > 0 and h2.get("ic", 0) > 0
        print(f"\n  {'The equation has some out-of-sample skill (t ≥ 2, both halves).' if good else 'Not trustworthy yet: the out-of-sample check is too weak (needs t ≥ 2 and both halves positive).'}")
    Path(args.out).write_text(json.dumps({"models": results, "halves": [h1, h2], "now": now, "reactive": rc,
                                          "terms": terms, "lam": args.lam}, default=float, indent=1))
    print(f"(saved to {args.out})", file=sys.stderr)


if __name__ == "__main__":
    main()

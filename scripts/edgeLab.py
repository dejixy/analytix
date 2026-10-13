"""
Edge lab: replay the recordings and measure every signal the way the bot would trade it (engine/edgelab.py).

    python -m scripts.edgeLab                          # every recording, every coin, every setup
    python -m scripts.edgeLab --days 7                 # the last week only
    python -m scripts.edgeLab --lead-only              # Binance leads alone: much faster (no analyzer)
    python -m scripts.edgeLab --coins BTC,ETH --csv events.csv

On the server, from /opt/analytix:  sudo -u analytix .venv/bin/python -m scripts.edgeLab --days 7 --csv /tmp/events.csv

Reading it: numbers are bps per event after fees unless they say gross. "train" is the earlier 60% of the time
span, "test" the rest; only test numbers say whether something works. An edge needs t ≥ 3 on train and to stay
positive on test. With dozens of things tested, a few will look good by luck: trust the test column.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

from config import RECORD_DIR
from engine.edgelab import HORIZONS_S, LATENCIES_MS, Lab, net_maker, net_market, study
from ingestion.replay import _first_recv, coins_in, read_recordings, recording_files

H = {5: "5s", 10: "10s", 30: "30s", 60: "1m", 120: "2m", 300: "5m", 900: "15m"}


def f(x, d=1, signed=True):
    if x is None:
        return "-"
    return f"{x:+.{d}f}" if signed else f"{x:.{d}f}"


def stat(s, d=1):
    if not s or not s["n"]:
        return "-"
    return f"{f(s['mean'], d)} (t {f(s['t'], 1, False)}, n {s['n']})"


def run(paths, coins=None, days=None, lead_only=False, lead_min_bps=0.0, progress=True) -> tuple[Lab, float]:
    files = recording_files(paths)
    if days:
        newest = max((_first_recv(p) or 0) for p in files) if files else 0
        files = [p for p in files if (_first_recv(p) or 0) >= newest - days * 86_400_000]
    if not coins:
        found: dict[str, None] = {}
        for p in files[:48]:
            for c in coins_in(p, max_lines=5_000):
                found.setdefault(c)
        coins = list(found)
    lab = Lab(coins, lead_only=lead_only, lead_min_bps=lead_min_bps)
    n, t0, span = 0, time.time(), [None, None]
    for fresh, recv, msg in read_recordings(files):
        if fresh and n:
            lab.reset()
        span[0] = recv if span[0] is None else span[0]
        span[1] = recv
        lab.on_message(msg)
        n += 1
        if progress and n % 1_000_000 == 0:
            done_h = (span[1] - span[0]) / 3_600_000
            print(f"  {n / 1e6:.0f}M messages, {done_h:.1f} h of data, {len(lab.events)} events, "
                  f"{time.time() - t0:.0f}s", file=sys.stderr)
    hours = (span[1] - span[0]) / 3_600_000 if span[0] else 0.0
    return lab, hours


def report(lab: Lab, hours: float, taker: float, maker: float) -> str:
    s = study(lab.events, taker, maker)
    L = []
    L.append(f"EDGE LAB  {hours:.0f} h of recordings · {', '.join(lab.coins)} · {s['events']} events from {lab.signals} signals")
    L.append(f"Costs per side: market {taker} bps, limit {maker} bps. Entry {LATENCIES_MS[1] / 1000:g}s after the signal "
             f"at the ask (long) or bid (short); exit at the bid or ask. Train {s['train_events']} events, test {s['test_events']}.")
    L.append("")
    L.append("1. EDGE PER EVENT, after fees (best hold picked on train, judged on test)")
    L.append(f"   {'setup':<15}{'events':>7}  {'hold':>5}  {'how':<16}{'train':<26}{'test':<26}verdict")
    for r in s["setups"]:
        how = "market in, limit out" if r["best_how"] == "maker" else "market"
        L.append(f"   {r['label']:<15}{r['events']:>7}  {H[r['best_h']]:>5}  {how[:15]:<16}{stat(r['train']):<26}{stat(r['test']):<26}{r['verdict']}")
    L.append("")
    L.append("2. HOW THE MOVE BUILDS: gross bps by hold (train), market net in brackets")
    L.append("   " + f"{'setup':<15}" + "".join(f"{H[h]:>14}" for h in HORIZONS_S))
    for r in s["setups"]:
        cells = "".join(f"{f(x['gross']['mean']):>7} ({f(x['market']['mean'], 0)})".rjust(14) for x in r["holds"])
        L.append(f"   {r['label']:<15}{cells}")
    L.append("")
    L.append("3. SPEED: gross bps at a 30s exit by how long after the signal you got in")
    L.append("   " + f"{'setup':<15}" + "".join(f"{lat / 1000:g}s".rjust(16) for lat in LATENCIES_MS))
    for r in s["setups"]:
        L.append(f"   {r['label']:<15}" + "".join(stat(r["latency"][lat]).split(' (')[0].rjust(16) for lat in LATENCIES_MS))
    L.append("")
    L.append("4. FILTERS at each setup's best hold, train → test (✓ = t ≥ 3 on train and holds on test)")
    for r in s["setups"]:
        rows = [x for x in r["filters"] if x["train"]["n"] >= 10]
        if not rows:
            continue
        L.append(f"   {r['label']}")
        for x in rows:
            L.append(f"     {'✓' if x['holds'] else ' '} {x['feature']:<11}{x['value']:<13}{stat(x['train']):<26}{stat(x['test'])}")
    L.append("")
    L.append("5. STOPS: how far winners dipped before paying, at the best hold")
    for r in s["setups"]:
        st = r["stop"]
        if st:
            sig = "" if st["sigma_units"] is None else f" = {st['sigma_units']:.1f}x a normal move over the hold"
            L.append(f"   {r['label']:<15}winners dipped {st['winners_mae_p50']:.1f} bps (median), "
                     f"{st['winners_mae_p90']:.1f} (9 in 10){sig} · a {st['stop_bps']:.1f} bp stop gives {stat(st['with_stop'])}")
    L.append("")
    L.append(f"{s['tests']} comparisons were made: at t ≥ 2 about {s['tests'] * 0.023:.0f} would pass by luck alone. "
             f"Trust what holds on test.")
    return "\n".join(L)


def write_csv(lab: Lab, path: str, taker: float, maker: float) -> None:
    feats = sorted({k for e in lab.events for k in e.features})
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "coin", "side", "time_utc", "tags", *feats,
                    *[f"entry_{lat}ms" for lat in LATENCIES_MS],
                    *[f"gross_{H[h]}" for h in HORIZONS_S], *[f"market_{H[h]}" for h in HORIZONS_S],
                    *[f"maker_{H[h]}" for h in HORIZONS_S], *[f"mae_{H[h]}" for h in HORIZONS_S],
                    *[f"mfe_{H[h]}" for h in HORIZONS_S]])
        for e in lab.events:
            w.writerow([e.id, e.coin, e.side, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e.t_ms / 1000)),
                        "+".join(sorted(e.tags)), *[e.features.get(k) for k in feats],
                        *[e.entries.get(lat) for lat in LATENCIES_MS],
                        *[e.gross(h) for h in HORIZONS_S], *[net_market(e, h, taker) for h in HORIZONS_S],
                        *[net_maker(e, h, taker, maker) for h in HORIZONS_S],
                        *[e.mae.get(h) for h in HORIZONS_S], *[e.mfe.get(h) for h in HORIZONS_S]])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    p.add_argument("--coins", help="comma separated, default every coin in the recordings")
    p.add_argument("--days", type=float, help="only the most recent N days")
    p.add_argument("--lead-only", action="store_true", help="Binance leads alone (fast: no analyzer)")
    p.add_argument("--lead-min", type=float, default=0.0, help="smallest Binance move to count, bps")
    p.add_argument("--taker", type=float, default=4.5, help="taker fee per side, bps")
    p.add_argument("--maker", type=float, default=1.5, help="maker fee per side, bps")
    p.add_argument("--csv", help="also write every event to this CSV")
    a = p.parse_args()
    coins = [c.strip() for c in a.coins.split(",")] if a.coins else None
    t0 = time.time()
    lab, hours = run(a.paths, coins, a.days, a.lead_only, a.lead_min)
    print(report(lab, hours, a.taker, a.maker))
    print(f"\n({time.time() - t0:.0f}s)", file=sys.stderr)
    if a.csv:
        write_csv(lab, a.csv, a.taker, a.maker)
        print(f"events written to {Path(a.csv).resolve()}", file=sys.stderr)


if __name__ == "__main__":
    main()

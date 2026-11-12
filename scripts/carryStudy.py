"""
Funding carry on history: the live carry watcher's rule (engine/watchers.py), unchanged, replayed hour by hour over
Hyperliquid's whole funding history for HYPE, BTC, ETH and SOL, with the spot-perp price gap from 4-hour closes.

    python -m scripts.carryStudy
    bash scripts/run.sh carry          (on the server: a few minutes, kept in /tmp/carry.txt)

Decided before running: the watcher is worth trusting if its trades, pooled, average more than zero after fees with
t >= 2 and are positive in both halves. "Kept" is how much of the 3-day funding rate at entry the hold actually
earned; the live rule assumes half (CARRY_KEEP) until this measures it.
"""
import argparse
import asyncio
import sys
import time

from engine import watchers as W
from ingestion.account import fetch_funding_history, fetch_spot_prices
from ingestion.backfill import fetch_candles

DAY = lambda ms: time.strftime("%d %b %Y", time.gmtime(ms / 1000))


async def gather(coins, since_ms: int, log=sys.stderr) -> dict:
    spot = await fetch_spot_prices()
    out = {}
    for coin in coins:
        tok = W.CARRY_SPOT[coin]
        pair = (spot.get(tok) or {}).get("pair")
        hist = await fetch_funding_history(coin, since_ms, pause_s=1.5)
        perp = [(int(r["t"]) + 4 * W.HOUR, float(r["c"])) for r in await fetch_candles(coin, "4h", 5000) if r.get("c")]
        sp = [(int(r["t"]) + 4 * W.HOUR, float(r["c"])) for r in await fetch_candles(pair, "4h", 5000) if r.get("c")] if pair else []
        print(f"  {coin}: {len(hist):,} funding hours, {len(perp):,} perp and {len(sp):,} spot 4-hour closes "
              f"({tok} spot {pair or 'not found'})", file=log)
        out[coin] = (hist, perp, sp)
        await asyncio.sleep(2)
    return out


def line(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:<8}{0:>7}   no trades"
    kept = f"{s['kept']:.2f}" if s.get("kept") is not None else "-"
    return (f"  {name:<8}{s['n']:>7}{s['days']:>9.1f}{s['funding_bps']:>+10.0f}{s['basis_bps']:>+8.0f}{W.CARRY_COST_BPS:>7.0f}"
            f"{s['avg_bps']:>+9.0f} ({s['t']:+4.1f}){s['won']:>7.0%}{kept:>7}")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default=",".join(W.CARRY_SPOT))
    ap.add_argument("--since", default="2024-01-01")
    args = ap.parse_args()
    coins = [c for c in args.coins.split(",") if c in W.CARRY_SPOT]
    since = int(time.mktime(time.strptime(args.since, "%Y-%m-%d"))) * 1000
    data = asyncio.run(gather(coins, since))
    print(f"Funding carry, the live rule on history: buy spot and short the perp when funding has averaged "
          f"{W.CARRY_ENTER_APR:.0f}%+ a year over 3 days and is {W.CARRY_NOW_APR:.0f}%+ now; close when a day averages "
          f"under {W.CARRY_EXIT_APR:.0f}% or after {W.CARRY_MAX_DAYS} days; {W.CARRY_COST_BPS:.0f} bps taker fees a trade.\n")
    print(f"  {'coin':<8}{'trades':>7}{'days':>9}{'funding':>10}{'gap':>8}{'fees':>7}{'net bps (t)':>16}{'won':>7}{'kept':>7}")
    every = []
    res = {}
    for coin in coins:
        hist, perp, sp = data[coin]
        if not hist:
            print(f"  {coin:<8}   no funding history")
            continue
        r = W.carry_history(coin, hist, perp, sp)
        res[coin] = r
        every += r["trades"]
        print(line(coin, W.summarise(r["trades"])))
    every.sort(key=lambda t: t["t"])
    pooled = W.summarise(every)
    print(line("all", pooled))
    if pooled.get("n"):
        print(f"  halves: {pooled['first']:+.0f} and {pooled['second']:+.0f} bps a trade")
    print("\n  gap = the spot-perp price gap's change over the hold; kept = the median share of the 3-day rate earned\n")
    print("Always on (hold spot and short perp the whole time, fees once):")
    for coin, r in res.items():
        span = (r["to"] - r["from"]) / W.DAY / 365 if r["from"] else 0
        print(f"  {coin:<6} {DAY(r['from'])} to {DAY(r['to'])}: funding {r['always_apr']:+.1f}% a year, negative "
              f"{r['negative_share']:.0%} of hours, worst 30 days {r['worst_30d_bps']:+.0f} bps" if r["worst_30d_bps"] is not None
              else f"  {coin:<6} too short a history")
    ok = pooled.get("n", 0) >= 10 and pooled.get("t", 0) >= 2 and (pooled.get("first") or 0) > 0 and (pooled.get("second") or 0) > 0
    print(f"\n  {'The carry watcher passes' if ok else 'The carry watcher does not pass'} "
          "(needs 10+ trades pooled, t >= 2, positive in both halves)."
          + (f" Measured: {pooled['kept']:.2f} of the 3-day rate lasted (the live rule assumes {W.CARRY_KEEP})."
             if pooled.get("kept") is not None else ""))


if __name__ == "__main__":
    main()

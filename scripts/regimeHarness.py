"""
Trend or reversion, by regime: does the last few minutes' move carry on or snap back over the next 30 minutes, and
does the answer depend on whether the market is quiet or loud? Deji's "best strategy per regime" idea, through the
profit harness (engine/harness.py) and the move-size forecast (engine/volforecast.py).

    python -m scripts.regimeHarness
    python -m scripts.regimeHarness --test-days 7 --capital 100 --leverage 10

On the server, from the research copy (the fast store must be up to date: scripts.buildStore):
    cd ~/analytix && nice -n 19 /opt/analytix/.venv/bin/python -m scripts.regimeHarness > /tmp/regime.txt 2> /tmp/regime.log

The ideas, on every coin at every :00 and :30 (UTC), each held 30 minutes
    trend 5m / 15m     trade the way price moved over the last 5 (or 15) minutes
    revert 5m / 15m    trade against it
    (a minute with no move, or no recent print, is skipped)

How it's judged (as the profit harness)
    filters      all; not quiet; loud only; quiet only (the forecast's thirds, fitted on the earlier days)
    the pick     for each idea, the style and filter with the best net on the earlier days, then its result on the
                 locked newest days, per coin as well. Passes if net > 0 with t >= 2 over days and both halves
                 positive

The bar, from the profit harness: in loud windows a 30-minute move averages about 26 bps on BTC and 60 on HYPE, so
with market orders (9 bps) the direction must be right about 67% of the time on BTC and 58% on HYPE.
"""
import argparse
import sys
import time

import numpy as np

from engine import volforecast
from engine.harness import DAY_MS, Market, Signal
from scripts.profitHarness import forecast_table, judge, store_span
from storage import tickStore
from config import STORE_DIR

HOLD_MS = 30 * 60_000
EVERY_MIN = 30
LOOKS = (5, 15)
FILTERS = ("all", "not quiet", "loud only", "quiet only")


def ideas(fc: dict) -> dict[str, list[Signal]]:
    out = {f"{how} {k}m": [] for k in LOOKS for how in ("trend", "revert")}
    for coin, x in fc.items():
        if x is None:
            continue
        n = len(x.price)
        idx = np.arange(n)
        t_end = (idx + x.m0 + 1) * volforecast.MINUTE
        at = np.flatnonzero((t_end // volforecast.MINUTE) % EVERY_MIN == 0)
        lp = np.log(x.price)
        for k in LOOKS:
            i = at[at >= k]
            move = lp[i] - lp[i - k]
            good = x.ok[i] & x.ok[i - k] & np.isfinite(move) & (move != 0)
            for j, mv in zip(i[good], move[good]):
                side = 1 if mv > 0 else -1
                out[f"trend {k}m"].append(Signal(coin, int(t_end[j]), side, hold_ms=HOLD_MS))
                out[f"revert {k}m"].append(Signal(coin, int(t_end[j]), -side, hold_ms=HOLD_MS))
    return {k: sorted(v, key=lambda s: s.t_ms) for k, v in out.items() if v}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default="", help="default: every coin in the store")
    ap.add_argument("--test-days", type=float, default=7)
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--wait", type=float, default=30, help="seconds a limit order waits")
    args = ap.parse_args()
    notional = args.capital * args.leverage
    first_day, last_day, start, end, split = store_span(args.test_days)
    coins = [c for c in args.coins.split(",") if c] or tickStore.coins(STORE_DIR)

    print(f"loading {', '.join(coins)}...", file=sys.stderr)
    t0 = time.time()
    fc = {c: volforecast.fit(c, start, split, end) for c in coins}
    markets = {c: Market(c, start, end) for c in coins if fc.get(c)}
    print(f"  loaded in {time.time() - t0:.0f}s", file=sys.stderr)
    sigs = ideas(fc)
    sigs = {k: [s for s in v if start + DAY_MS <= s.t_ms < end - HOLD_MS] for k, v in sigs.items()}   # warm-up
    first_sig = min(s.t_ms for v in sigs.values() for s in v)
    train_days = max((split - first_sig) / DAY_MS, 1e-9)
    test_days = (end - split) / DAY_MS
    print(f"Regime test · store {first_day} to {last_day} · locked test from "
          f"{time.strftime('%d %b %H:%M', time.gmtime(split / 1000))} UTC ({test_days:.1f} days) · every :00 and :30, "
          f"held 30 min · ${args.capital:g} at {args.leverage:g}x = ${notional:,.0f} a trade · 0.5 s delay")
    forecast_table(fc)
    judge(sigs, markets, fc, split, train_days, test_days, notional, args.wait, filters=FILTERS, by_coin=True,
          capital=args.capital, leverage=args.leverage)


if __name__ == "__main__":
    main()

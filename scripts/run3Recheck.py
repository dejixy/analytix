"""
Run 3 recheck: the one setup that cleared fees out of sample (a breakout or cascade with Binance moving 7+ bps the
same way: +7.2 and +8.3 bps after fees on Run 3's test half), retested on days Run 3 never saw, priced honestly.

    python -m scripts.run3Recheck                              # events from 17 Oct 2026 on, every coin recorded
    python -m scripts.run3Recheck --start 2026-10-20 --coins BTC,ETH

On the server, from the research copy (the replay takes a while: leave it running in the background):
    cd ~/analytix && ANALYTIX_RECORD_DIR=/opt/analytix/data/recordings nohup nice -n 19 \\
        /opt/analytix/.venv/bin/python -m scripts.run3Recheck > /tmp/run3.txt 2> /tmp/run3.log &

Decided before running (the pass rule):
    candidate   an edge-lab event tagged breakout or cascade, joined by a Binance lead of 7+ bps the same way
    trade       in 0.5 s after the moment it qualifies (both parts seen), out 60 s later, market both ways (9 bps)
    pricing     real trade prints: a buy fills at the first taker-buy print after the entry time, a sell at the
                first taker-sell print (books arrive only every ~5.4 s, so a book price can be seconds old)
    passes if   the average after fees is above zero with t >= 2, and above zero in both halves of the days

Why the pricing matters: the edge lab priced entries from the last book before the entry time. With books 5.4 s apart
that book is often from before the signal, so part of the move that triggered the signal was counted as profit. The
report shows the same events priced three ways, and timed from the first signal (as Run 3 did, which used the Binance
lead before it had happened) and from the moment the event qualified.

Needs the fast store for prices (scripts/buildStore.py); the setups themselves come from replaying the recordings
through the edge lab's own detectors, unchanged.
"""
import argparse
import calendar
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from config import RECORD_DIR, STORE_DIR
from engine.edgelab import BASE_LATENCY_MS, Lab, summary
from ingestion.replay import _first_recv, read_recordings, recording_files
from storage import tickStore

TAKER_RT_BPS = 9.0
HOLDS = (30, 60, 120, 300)
MAIN_HOLD = 60
LEAD_MIN = 7.0
MAX_WAIT_MS = 10_000            # a print or book more than this after the time it's needed for doesn't count
WARMUP_MS = 3 * 3_600_000       # replay starts this long before --start so the 60m windows are warm


class TimedLab(Lab):
    """The edge lab, also noting when each tag first joined an event and every Binance lead's size."""

    def signal(self, coin: str, setup: str, side: int, t: int, extra: dict) -> None:
        super().signal(coin, setup, side, t, extra)
        ev = self.open.get(coin)
        if ev is None or setup not in ev.tags or ev.side != side:
            return
        ev.features.setdefault("tag_t", {}).setdefault(setup, t)
        if setup == "lead" and "lead_bps" in extra:
            ev.features.setdefault("leads", []).append((t, float(extra["lead_bps"])))


def replay(coins: list[str], files: list) -> list:
    """Every edge-lab event for these coins (one process per group of coins, so the replay runs in parallel)."""
    lab = TimedLab(coins)
    n, t0 = 0, time.time()
    for fresh, recv, msg in read_recordings(files):
        if fresh and n:
            lab.reset()
        lab.on_message(msg)
        n += 1
        if n % 2_000_000 == 0:
            print(f"  {'/'.join(coins)}: {n / 1e6:.0f}M messages, {len(lab.events):,} events, "
                  f"{(time.time() - t0) / 60:.0f} min", file=sys.stderr)
    return lab.events


def qualify_time(ev) -> int | None:
    """When an event first had both parts: a breakout or cascade, and a Binance lead of LEAD_MIN+ bps its way."""
    times = ev.features.get("tag_t", {})
    move = [times[k] for k in ("breakout", "cascade") if k in times]
    leads = [t for t, bps in ev.features.get("leads", []) if abs(bps) >= LEAD_MIN and bps * ev.side > 0]
    if not move or not leads:
        return None
    return max(min(move), min(leads))


class Prices:
    """One coin's trade prints by taker side, and its book, from the store."""

    def __init__(self, coin: str, start_ms: int, store):
        z = tickStore.load(coin, ["trades", "book"], start_ms - 600_000, None, store,
                           only=["trade_px", "trade_side", "bid_px", "ask_px"])
        self.prints = {}
        for side in (1, -1):
            m = z["trade_side"] == side
            self.prints[side] = (z["trade_t"][m], z["trade_px"][m])
        self.book_t, self.bid, self.ask = z["book_t"], z["bid_px"][:, 0], z["ask_px"][:, 0]

    def print_px(self, taker_side: int, t: float) -> float:
        tt, px = self.prints[taker_side]
        i = np.searchsorted(tt, t, side="left")
        return float(px[i]) if i < len(tt) and tt[i] - t <= MAX_WAIT_MS else np.nan

    def book_px(self, side: int, t: float, buy: bool) -> float:
        i = np.searchsorted(self.book_t, t, side="left")
        if i >= len(self.book_t) or self.book_t[i] - t > MAX_WAIT_MS:
            return np.nan
        return float(self.ask[i] if buy else self.bid[i])


def priced(ev, p: Prices, t0: int, hold: int, how: str) -> float:
    """Gross bps of a market round trip: in at t0 + 0.5 s, out `hold` s later."""
    t_in = t0 + BASE_LATENCY_MS
    t_out = t_in + hold * 1000
    long = ev.side > 0
    if how == "prints":
        e, x = p.print_px(1 if long else -1, t_in), p.print_px(-1 if long else 1, t_out)
    else:
        e, x = p.book_px(ev.side, t_in, long), p.book_px(ev.side, t_out, not long)
    return ev.side * (x / e - 1) * 1e4 if e > 0 and x > 0 else np.nan


def row(label: str, nets: np.ndarray, t_ms: np.ndarray) -> str:
    ok = np.isfinite(nets)
    v, tt = nets[ok], t_ms[ok]
    s = summary(list(v))
    if not s["n"]:
        return f"  {label:<40}{'-':>6}"
    mid = np.median(tt)
    h1, h2 = v[tt <= mid], v[tt > mid]
    return (f"  {label:<40}{s['n']:>6}{s['mean'] + TAKER_RT_BPS:>+9.1f}{s['mean']:>+8.1f}"
            f"{(s['t'] or 0):>+7.1f}{s['hit'] * 100:>6.0f}%{h1.mean() if len(h1) else np.nan:>+10.1f}"
            f"{h2.mean() if len(h2) else np.nan:>+10.1f}")


HEAD = f"  {'':<40}{'n':>6}{'gross':>9}{'net':>8}{'t':>7}{'win':>7}{'1st half':>10}{'2nd half':>10}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--start", default="2026-10-17", help="first day counted (UTC); Run 3 used data up to 16 Oct")
    ap.add_argument("--coins", default="", help="default: every coin in the store")
    ap.add_argument("--jobs", type=int, default=2, help="coins replayed in parallel")
    args = ap.parse_args()

    start_ms = calendar.timegm(time.strptime(args.start, "%Y-%m-%d")) * 1000
    coins = [c for c in args.coins.split(",") if c] or tickStore.coins(STORE_DIR)
    if not coins:
        sys.exit(f"no fast store in {STORE_DIR}: run scripts.buildStore first")
    files = [f for f in recording_files(args.paths) if (_first_recv(f) or 0) >= start_ms - WARMUP_MS]
    print(f"replaying {len(files)} hours for {', '.join(coins)}...", file=sys.stderr)

    jobs = max(1, min(args.jobs, len(coins)))
    groups = [coins[i::jobs] for i in range(jobs)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        found = [ev for evs in pool.map(replay, groups, [files] * jobs) for ev in evs]
    print(f"replay done in {(time.time() - t0) / 60:.0f} min", file=sys.stderr)
    events = [e for e in found if e.t_ms >= start_ms]
    prices = {c: Prices(c, start_ms, STORE_DIR) for c in coins}
    cands = [(e, qualify_time(e)) for e in events]
    cands = [(e, q) for e, q in cands if q is not None]
    moves = [e for e in events if e.features.get("tag_t", {}).keys() & {"breakout", "cascade"}]
    alone = [e for e in moves if qualify_time(e) is None]

    if not events:
        sys.exit("no edge-lab events after --start")
    first, last = min(e.t_ms for e in events), max(e.t_ms for e in events)
    print(f"Run 3 recheck · {time.strftime('%d %b %H:%M', time.gmtime(first / 1000))} to "
          f"{time.strftime('%d %b %H:%M', time.gmtime(last / 1000))} UTC · {', '.join(coins)} · "
          f"{len(events):,} edge-lab events, {len(moves):,} breakouts or cascades, {len(cands):,} with Binance 7+ bps")
    by_coin = {c: sum(1 for e, _ in cands if e.coin == c) for c in coins}
    print("  candidates by coin: " + ", ".join(f"{c} {k}" for c, k in by_coin.items()))
    print(f"\nbps per trade; net = after a {TAKER_RT_BPS:g} bps taker round trip; gross = before it\n")

    t_q = np.array([q for _, q in cands], float)
    t_first = np.array([e.t_ms for e, _ in cands], float)

    def nets(pairs, t_of, hold, how):
        return np.array([priced(e, prices[e.coin], t, hold, how) for (e, _), t in zip(pairs, t_of)]) - TAKER_RT_BPS

    print("1. The pre-registered test: candidates, in when they qualify, held 60 s")
    print(HEAD)
    main_row = nets(cands, t_q, MAIN_HOLD, "prints")
    print(row("trade prints (the test)", main_row, t_q))
    print(row("next book after entry", nets(cands, t_q, MAIN_HOLD, "book"), t_q))

    print("\n2. How Run 3 measured it: timed from the first signal, same events")
    print(HEAD)
    old = np.array([e.gross(MAIN_HOLD) if e.gross(MAIN_HOLD) is not None else np.nan for e, _ in cands]) - TAKER_RT_BPS
    print(row("last book before entry (Run 3)", old, t_first))
    print(row("trade prints, from first signal", nets(cands, t_first, MAIN_HOLD, "prints"), t_first))
    lead_first = sum(1 for e, q in cands if q > e.t_ms)
    print(f"  {lead_first} of {len(cands)} candidates only qualified after their first signal "
          f"(Run 3's timing entered before the Binance part had happened)")

    print("\n3. Other holds, trade prints, in when they qualify")
    print(HEAD)
    for h in HOLDS:
        if h != MAIN_HOLD:
            print(row(f"{h} s", nets(cands, t_q, h, "prints"), t_q))

    print("\n4. For contrast: breakouts and cascades without the Binance part, trade prints, 60 s")
    print(HEAD)
    a_t = np.array([min(v for k, v in e.features["tag_t"].items() if k in ("breakout", "cascade")) for e in alone],
                   float)
    a = np.array([priced(e, prices[e.coin], t, MAIN_HOLD, "prints") for e, t in zip(alone, a_t)]) - TAKER_RT_BPS
    print(row("alone", a, a_t))

    ok = np.isfinite(main_row)
    s = summary(list(main_row[ok]))
    mid = np.median(t_q[ok]) if ok.any() else 0
    halves = [main_row[ok][t_q[ok] <= mid], main_row[ok][t_q[ok] > mid]]
    passed = (s["n"] and s["mean"] > 0 and (s["t"] or 0) >= 2 and all(len(h) and h.mean() > 0 for h in halves))
    print(f"\nVerdict: {'PASSES' if passed else 'FAILS'} the pre-registered test "
          f"(net {s['mean'] if s['n'] else float('nan'):+.1f} bps, t {s['t'] or 0:+.1f}, n {s['n']}).")


if __name__ == "__main__":
    main()

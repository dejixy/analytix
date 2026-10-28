"""
Feed probe: how often and how fast does Hyperliquid's feed really reach this server?

The recordings show the order book arriving only every 5.4s, and trades arriving about 400 ms after they happen when
the network accounts for only about 125 ms. This opens fresh sockets, separate from the running Analytix, to find out
why, and whether a different feed or more sockets fix it.

    python -m scripts.feedProbe                        # 10 minutes, BTC and ETH, 3 sockets
    python -m scripts.feedProbe --minutes 5 --coins BTC,ETH,SOL,HYPE --sockets 4

On the server (no sudo needed; it records nothing):
    cd /opt/analytix && .venv/bin/python -m scripts.feedProbe > /tmp/probe.txt 2> /tmp/probe.log; cat /tmp/probe.txt

Each Hyperliquid socket subscribes to the same feeds per coin: l2Book (the 20-level book Analytix uses), bbo (best bid
and offer only, sent whenever the top changes, at most once a block) and trades. The first 5 seconds are skipped
(subscribing sends a backlog). Delays are this server's clock minus the exchange's time, so they need a synced clock.

Reading it:
  1. How often each feed arrives on a fresh socket, and how late. If l2Book still arrives every ~5s here, that's
     Hyperliquid's cadence for it; if it's much faster, the main socket is being slowed. bbo's gap is the real pace of
     top-of-book changes.
  2. The same delays for every socket: are some sockets consistently slower?
  3. Taking the first copy of each message across all sockets: how much earlier than socket 1 alone (the tip from the
     thread). Big savings at p90/p99 mean the extra sockets are worth adding to the recorder and the bot.
  4. Bybit: arrival minus its send time (ts) vs minus its matching-engine time (cts). Analytix stamps Bybit quotes with
     cts; if cts is the stale one, the recorder should switch to ts.
  5. This probe's own stalls. Should be near zero; if not, the server itself is too busy to trust the other numbers.
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict

import numpy as np
import websockets

from config import BYBIT_WS_URL, HL_WS_URL, PING_INTERVAL_S
from ingestion.external import BYBIT_PING_S, symbol

WARMUP_MS = 5_000
CHANNELS = ("l2Book", "bbo", "trades")


def now_ms() -> float:
    return time.time() * 1000.0


class Tally:
    """Every message each socket received: when the exchange stamped it and when it arrived here."""

    def __init__(self, start_ms: float):
        self.start = start_ms
        self.rows: dict[tuple, dict[str, list]] = defaultdict(lambda: {"ex": [], "recv": []})  # (socket, ch, coin)
        self.arrivals: dict[tuple, dict[int, float]] = {}                                     # message -> socket -> recv
        self.errors: list[str] = []
        self.drops: list[str] = []

    def hl(self, sock: int, raw: str | bytes, recv: float) -> None:
        msg = json.loads(raw)
        ch, d = msg.get("channel"), msg.get("data")
        if ch == "error":
            self.errors.append(f"socket {sock + 1}: {str(d)[:200]}")
            return
        if recv - self.start < WARMUP_MS or not d:
            return
        if ch in ("l2Book", "bbo"):
            coin, t = d["coin"], int(d["time"])
            key = (ch, coin, t)
        elif ch == "trades":
            coin, t = d[0]["coin"], max(int(x["time"]) for x in d)
            key = (ch, coin, tuple(int(x["tid"]) for x in d))
        else:
            return
        row = self.rows[(sock, ch, coin)]
        row["ex"].append(t)
        row["recv"].append(recv)
        self.arrivals.setdefault(key, {}).setdefault(sock, recv)


async def _ping(ws, msg: dict, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        await ws.send(json.dumps(msg))


async def hl_socket(sock: int, url: str, coins: list[str], tally: Tally) -> None:
    try:
        async with websockets.connect(url, ping_interval=None, open_timeout=10, max_size=2**23) as ws:
            for coin in coins:
                for ch in CHANNELS:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": ch, "coin": coin}}))
            pinger = asyncio.create_task(_ping(ws, {"method": "ping"}, PING_INTERVAL_S))
            try:
                async for raw in ws:
                    tally.hl(sock, raw, now_ms())
            finally:
                pinger.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        tally.drops.append(f"Hyperliquid socket {sock + 1}: {type(exc).__name__}: {exc}")


async def bybit_socket(url: str, coins: list[str], out: list[tuple], tally: Tally) -> None:
    try:
        async with websockets.connect(url, ping_interval=None, open_timeout=10, max_size=2**22) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.1.{symbol(c)}" for c in coins]}))
            pinger = asyncio.create_task(_ping(ws, {"op": "ping"}, BYBIT_PING_S))
            try:
                async for raw in ws:
                    recv = now_ms()
                    msg = json.loads(raw)
                    if not str(msg.get("topic", "")).startswith("orderbook.1.") or recv - tally.start < WARMUP_MS:
                        continue
                    ts, cts = msg.get("ts"), msg.get("cts")
                    if ts is not None and cts is not None:
                        out.append((recv - int(ts), recv - int(cts), msg.get("type") == "snapshot"))
            finally:
                pinger.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        tally.drops.append(f"Bybit: {type(exc).__name__}: {exc}")


async def loop_lag(out: list[float]) -> None:
    """How late a 10 ms sleep wakes up: this process's own stalls."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.01)
        out.append((time.perf_counter() - t0 - 0.01) * 1000)


# ----------------------------------------------------------------------------------------------- report

def q(v, p):
    return float(np.percentile(v, p)) if len(v) else float("nan")


def ms(x):
    return "-" if not np.isfinite(x) else f"{x:,.0f}"


def secs(x):
    return "-" if not np.isfinite(x) else f"{x:.2f}s"


def report(args, coins: list[str], tally: Tally, bybit: list[tuple], lag: list[float], minutes: float) -> None:
    print(f"Feed probe · {time.strftime('%d %b %H:%M', time.gmtime(tally.start / 1000))} UTC · {minutes:g} min · "
          f"{', '.join(coins)} · {args.sockets} Hyperliquid sockets")
    for line in tally.drops:
        print(f"  dropped: {line}")
    for line in sorted(set(tally.errors))[:10]:
        print(f"  Hyperliquid error: {line}")

    print("\n1. How often each feed arrives on socket 1, and how late (delay = arrival here minus exchange time)")
    print(f"{'feed':<8}{'coin':<6}{'msgs':>7}{'per min':>9}{'gap p50':>9}{'gap p90':>9}{'delay p50':>11}{'p90':>7}"
          f"{'p99':>7}")
    for ch in CHANNELS:
        for coin in coins:
            r = tally.rows.get((0, ch, coin))
            if not r or not r["ex"]:
                print(f"{ch:<8}{coin:<6}{0:>7}")
                continue
            ex, recv = np.array(r["ex"], float), np.array(r["recv"])
            gaps = np.diff(np.unique(ex)) / 1000
            delay = recv - ex
            print(f"{ch:<8}{coin:<6}{len(ex):>7,}{len(ex) / minutes:>9,.0f}{secs(q(gaps, 50)):>9}{secs(q(gaps, 90)):>9}"
                  f"{ms(q(delay, 50)):>8} ms{ms(q(delay, 90)):>7}{ms(q(delay, 99)):>7}")
    print("  (the recorder's l2Book arrives every ~5.4s and ~400-500 ms late; compare the l2Book rows)")

    print("\n2. Delay by socket, all coins together (ms: p50 / p90 / p99)")
    print(f"{'socket':<8}" + "".join(f"{ch:>22}" for ch in CHANNELS))
    for s in range(args.sockets):
        cells = []
        for ch in CHANNELS:
            d = [rv - e for coin in coins for (e, rv) in
                 zip(tally.rows.get((s, ch, coin), {"ex": []})["ex"], tally.rows.get((s, ch, coin), {"recv": []})["recv"])]
            cells.append(f"{ms(q(d, 50))} / {ms(q(d, 90))} / {ms(q(d, 99))}" if d else "-")
        print(f"{s + 1:<8}" + "".join(f"{c:>22}" for c in cells))

    print(f"\n3. First copy across {args.sockets} sockets: how much earlier than socket 1 alone (ms)")
    print(f"{'feed':<8}{'messages':>10}{'p50':>7}{'p90':>7}{'p99':>7}{'max':>8}{'socket 1 already first':>24}"
          f"{'missed by some':>16}")
    for ch in CHANNELS:
        keys = [k for k in tally.arrivals if k[0] == ch]
        full = [tally.arrivals[k] for k in keys if len(tally.arrivals[k]) == args.sockets]
        if not full:
            print(f"{ch:<8}{0:>10}")
            continue
        save = np.array([a[0] - min(a.values()) for a in full])
        print(f"{ch:<8}{len(full):>10,}{ms(q(save, 50)):>7}{ms(q(save, 90)):>7}{ms(q(save, 99)):>7}"
              f"{ms(save.max()):>8}{(save == 0).mean() * 100:>23.0f}%{len(keys) - len(full):>16,}")

    print("\n4. Bybit: arrival minus ts (send time) vs minus cts (matching-engine time), ms")
    if bybit:
        a = np.array(bybit, float)
        for name, col in (("ts", 0), ("cts", 1)):
            v = a[:, col]
            print(f"  {name:<4} p50 {ms(q(v, 50)):>6}  p90 {ms(q(v, 90)):>6}  p99 {ms(q(v, 99)):>8}  max {ms(v.max()):>9}  "
                  f"over 5s: {(v > 5000).mean() * 100:.2f}%")
        stale = a[:, 1] - a[:, 0] > 5000
        if stale.any():
            print(f"  cts more than 5s older than ts on {stale.sum():,} of {len(a):,} messages "
                  f"({a[stale, 2].mean() * 100:.0f}% of them snapshots)")
    else:
        print("  no Bybit messages")

    print("\n5. This probe's own stalls (how late a 10 ms timer fires)")
    print(f"  p50 {q(lag, 50):.1f} ms · p99 {q(lag, 99):.1f} ms · max {max(lag) if lag else float('nan'):.1f} ms")


async def main_async(args) -> None:
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    tally = Tally(now_ms())
    bybit: list[tuple] = []
    lag: list[float] = []
    tasks = [asyncio.create_task(hl_socket(i, args.hl_url, coins, tally)) for i in range(args.sockets)]
    if args.bybit_url:
        tasks.append(asyncio.create_task(bybit_socket(args.bybit_url, coins, bybit, tally)))
    tasks.append(asyncio.create_task(loop_lag(lag)))
    end = time.time() + args.minutes * 60
    while time.time() < end:
        await asyncio.sleep(min(60.0, max(0.0, end - time.time())))
        n = sum(len(r["ex"]) for r in tally.rows.values())
        print(f"  {args.minutes * 60 - max(0, end - time.time()):.0f}s: {n:,} Hyperliquid messages, "
              f"{len(bybit):,} Bybit, {len(tally.drops)} dropped sockets", file=sys.stderr)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    minutes = max((now_ms() - tally.start - WARMUP_MS) / 60_000, 1e-9)
    report(args, coins, tally, bybit, lag, minutes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--coins", default="BTC,ETH")
    ap.add_argument("--sockets", type=int, default=3)
    ap.add_argument("--hl-url", default=HL_WS_URL)
    ap.add_argument("--bybit-url", default=BYBIT_WS_URL, help="empty to skip the Bybit check")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()

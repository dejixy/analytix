"""
Fade check: where does the fade's edge sit? Fades come in bunches, and the live basket holds one at a time, so it
always takes the first fade of a bunch and skips the rest. Hyperliquid's max leverage is also highest on BTC and
ETH, where fades were weakest. This splits the study's fades (score -5 or worse, scored walk-forward, in 6s after
spotting, out after 15 min) to see which part carries the edge, and what the live shadow would make with each fix.

    A  by coin
    B  as the live bot takes them (one at a time across all coins): taken vs skipped
    C  one at a time per coin (a slot for each coin): taken vs skipped
    D  by place in a run of fades on the same coin, each less than 15 min after the last: 1st, 2nd, 3rd+
    E  the Hyperliquid shadow with each fix: per trade and per day, % of the margin
    each row: n, bps (t from days), wins, and per trade on Hyperliquid (each coin's max leverage, limit-order fees,
    as the live shadow counts it) and Papertrade 200x (BTC, ETH)

    python -m scripts.fadeCheck                 # last 14 days of recordings
On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.fadeCheck > /tmp/fadecheck.txt 2>&1 &'
"""
import argparse
import math
import sys
import time

import numpy as np

from api.informed import hl_result
from config import RECORD_DIR
from engine.bigmovers import pt_of
from ingestion.replay import _first_recv, recording_files
from scripts.degenSim import DAY_MS, HOLD_S, fade_trades, load

COINS = ("BTC", "ETH", "SOL", "HYPE")
LEV = 200


def taken(trades: list[dict], per_coin: bool) -> list[bool]:
    """Which fades a one-at-a-time bot takes (one slot in all, or one per coin), in time order."""
    free, out = {}, []
    for t in trades:
        k = t["coin"] if per_coin else "all"
        ok = t["t"] >= free.get(k, -1)
        out.append(ok)
        if ok:
            free[k] = t["t"] + HOLD_S * 1000
    return out


def place_in_run(trades: list[dict]) -> list[int]:
    """1 for a coin's first fade after a quiet 15 min, 2 for the next one within 15 min of it, and so on."""
    last, place, out = {}, {}, []
    for t in trades:
        c = t["coin"]
        place[c] = place.get(c, 0) + 1 if c in last and t["t"] - last[c] < HOLD_S * 1000 else 1
        last[c] = t["t"]
        out.append(place[c])
    return out


def day_t(xs: list[tuple[int, float]]) -> float | None:
    days = {}
    for d, v in xs:
        days.setdefault(d, []).append(v)
    means = [np.mean(v) for v in days.values()]
    if len(means) < 3 or np.std(means, ddof=1) == 0:
        return None
    return float(np.mean(means) / (np.std(means, ddof=1) / math.sqrt(len(means))))


def stat(sub: list[dict]) -> dict | None:
    if len(sub) < 5:
        return None
    b = np.array([t["bps"] for t in sub])
    hl = [hl_result(t, LEV, t["coin"]) for t in sub]
    pt = [pt_of(t, LEV, t["coin"]) for t in sub if t["coin"] in ("BTC", "ETH")]
    return {"n": len(sub), "bps": float(b.mean()), "t": day_t([(t["t"] // DAY_MS, t["bps"]) for t in sub]),
            "win": float((b > 0).mean()), "hl": float(np.mean(hl)), "pt": float(np.mean(pt)) if pt else None}


def pct(x):
    return "-" if x is None else f"{x * 100:+.1f}%"


def row(name: str, sub: list[dict]) -> str:
    s = stat(sub)
    if not s:
        return f"   {name:<26}{len(sub):>6}   too few"
    tt = "-" if s["t"] is None else f"{s['t']:+.1f}"
    return (f"   {name:<26}{s['n']:>6}{s['bps']:>+9.1f} ({tt:>5}){s['win']:>6.0%}{pct(s['hl']):>12}{pct(s['pt']):>12}")


HEAD = f"   {'':<26}{'n':>6}{'bps (t)':>17}{'wins':>6}{'HL / trade':>12}{'PT 200x':>12}"


def shadow(trades: list[dict], coins: tuple, per_coin: bool, n_days: int) -> str:
    """The Hyperliquid shadow over the period: trades a day, % of margin per trade and per day."""
    sub = [t for t in trades if t["coin"] in coins]
    keep = [t for t, ok in zip(sub, taken(sub, per_coin)) if ok]
    if not keep:
        return "   no trades"
    r = [hl_result(t, LEV, t["coin"]) for t in keep]
    per_day = {}
    for t, x in zip(keep, r):
        per_day[t["t"] // DAY_MS] = per_day.get(t["t"] // DAY_MS, 0.0) + x
    tt = day_t([(d, v) for d, v in per_day.items()])
    return (f"{len(keep) / n_days:>8.0f}{np.mean(r) * 100:>+11.2f}%{sum(r) / n_days * 100:>+11.1f}%"
            f"{'-' if tt is None else f'{tt:+.1f}':>8}{sum(v > 0 for v in per_day.values()):>5}/{len(per_day)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--min-usd", type=float, default=50_000)
    ap.add_argument("--history-h", type=float, default=72)
    a = ap.parse_args()
    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * DAY_MS]
    t0 = time.time()
    g, orders, _, _ = load(files, set(COINS), set(), a.min_usd)
    trades, _ = fade_trades(g, orders, a.history_h) if len(orders) > 50 else ([], 0)
    trades.sort(key=lambda t: t["t"])
    print(f"  {len(trades)} fades, {time.time() - t0:.0f}s", file=sys.stderr)
    if len(trades) < 20:
        print("Too few fades.")
        return
    n_days = max(1, len({t["t"] // DAY_MS for t in trades}))
    L = [f"FADE CHECK · {len(trades)} fades over {n_days} test days · score -5 or worse, in 6s, out after 15 min", "",
         "A. BY COIN", HEAD, row("all", trades)]
    L += [row(c, [t for t in trades if t["coin"] == c]) for c in COINS]

    tk = taken(trades, per_coin=False)
    L += ["", "B. AS THE LIVE BOT TAKES THEM: one at a time across all coins", HEAD,
          row("taken", [t for t, ok in zip(trades, tk) if ok]),
          row("skipped (one was open)", [t for t, ok in zip(trades, tk) if not ok])]
    for c in COINS:
        L.append(row(f"{c} taken", [t for t, ok in zip(trades, tk) if ok and t["coin"] == c]))
        L.append(row(f"{c} skipped", [t for t, ok in zip(trades, tk) if not ok and t["coin"] == c]))

    tc = taken(trades, per_coin=True)
    L += ["", "C. ONE SLOT PER COIN", HEAD,
          row("taken", [t for t, ok in zip(trades, tc) if ok]),
          row("skipped (that coin's was open)", [t for t, ok in zip(trades, tc) if not ok])]

    pl = place_in_run(trades)
    L += ["", "D. PLACE IN A RUN OF FADES ON THE SAME COIN (each within 15 min of the last)", HEAD]
    for name, sel in (("1st", lambda p: p == 1), ("2nd", lambda p: p == 2), ("3rd or later", lambda p: p >= 3)):
        L.append(row(name, [t for t, p in zip(trades, pl) if sel(p)]))
    for c in COINS:
        L.append(row(f"{c} 1st", [t for t, p in zip(trades, pl) if p == 1 and t["coin"] == c]))
        L.append(row(f"{c} 2nd or later", [t for t, p in zip(trades, pl) if p >= 2 and t["coin"] == c]))

    L += ["", "E. THE HYPERLIQUID SHADOW WITH EACH FIX (each coin's max leverage, limit-order fees, % of the margin)",
          f"   {'':<40}{'/day':>8}{'per trade':>12}{'per day':>12}{'t':>8}{'days +':>8}"]
    for name, coins, per_coin in (("as now: all coins, one at a time", COINS, False),
                                  ("SOL and HYPE only, one at a time", ("SOL", "HYPE"), False),
                                  ("all coins, one slot per coin", COINS, True),
                                  ("SOL and HYPE only, one slot per coin", ("SOL", "HYPE"), True),
                                  ("BTC and ETH only, one at a time", ("BTC", "ETH"), False)):
        L.append(f"   {name:<40}" + shadow(trades, coins, per_coin, n_days))
    L += ["", "Reading it: the edge sits where the bps and the HL column are clearly positive. If skipped or later fades",
          "beat taken or first ones, the bot should hold more than one at a time. If BTC and ETH sit near zero, fading",
          "SOL and HYPE only should do better. Few test days: a clear gap matters more than any one number."]
    print("\n".join(L))


if __name__ == "__main__":
    main()

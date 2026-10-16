"""
Hyperliquid-only moves (edge idea 2): when Hyperliquid jumps and Binance and Bybit don't, does it snap back, and
does fading it pay on Papertrade? The detector is engine/hlonly.py; pricing is engine/walletstudy.Marks.

    python -m scripts.hlOnlyStudy                               # 7 days, BTC and ETH
    python -m scripts.hlOnlyStudy --coins BTC,ETH,SOL,HYPE --days 3

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.hlOnlyStudy > /tmp/hlonly.txt 2>&1

Reading it: section 1 says which way price goes after each kind of move (+ = the fade made money). Section 2 takes
the biggest lone moves and checks lateness, leverage and both halves of the data. A fade only counts if it's positive
in both halves at a fill delay you can manage.
"""
import argparse
import re
import sys
import time

import numpy as np

from config import RECORD_DIR
from engine.hlonly import GAP_S, SIZES, WINDOWS_S, moves, venue_series
from engine.walletstudy import Marks
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

EXT = re.compile(r'"venue":"(binance|bybit)","coin":"([A-Za-z0-9]+)","ts":(\d+),"bid":([0-9.eE+-]+),"ask":([0-9.eE+-]+)')
HOLDS = (10, 30, 60, 300, 900)
LAGS = (2, 4, 6, 10)
LEVS = (200, 500, 841)


def f(x, d=1):
    return "-" if x is None or not np.isfinite(x) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.0f}%"


def t_hourly(t_ms, v):
    if len(v) < 3:
        return None
    _, inv = np.unique(t_ms // 3_600_000, return_inverse=True)
    m = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(m) < 3 or m.std(ddof=1) == 0:
        return None
    return float(v.mean() / (m.std(ddof=1) / np.sqrt(len(m))))


def price(events, marks, fade: bool, lag: int, hold: int, split_ms: float) -> dict | None:
    """Fade (or follow) every event: in `lag` seconds after it at the mid, out `hold` seconds later."""
    mids, ts, pts = [], [], {lev: [] for lev in LEVS}
    for coin in {e.coin for e in events}:
        sub = [e for e in events if e.coin == coin]
        mk = marks.get(coin)
        if mk is None:
            continue
        t = np.array([e.t_ms for e in sub])
        s = np.array([e.side for e in sub]) * (-1 if fade else 1)
        r = mk.follow(t, s, lag, hold, LEVS, coin)
        mids.append(r["mid"])
        ts.append(t[r["idx"]])
        for lev in LEVS:
            pts[lev].append(r["pt"][lev])
    if not mids or not sum(len(m) for m in mids):
        return None
    m, t = np.concatenate(mids), np.concatenate(ts)
    p = {lev: np.concatenate(v) for lev, v in pts.items()}
    first, second = m[t <= split_ms], m[t > split_ms]
    return {"n": len(m), "mid": float(m.mean()), "t": t_hourly(t, m), "win": float((m > 0).mean()),
            "pt": {lev: float(v.mean()) for lev, v in p.items()}, "bust": float((p[841] == -1.0).mean()),
            "first": float(first.mean()) if len(first) else None, "second": float(second.mean()) if len(second) else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH")
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    ext: dict[tuple[str, str], dict[int, float]] = {}

    def on_line(line: str) -> None:
        if '"channel":"ext"' not in line:
            return
        m = EXT.search(line)
        if m and m.group(2) in coins:
            ext.setdefault((m.group(1), m.group(2)), {})[int(m.group(3)) // 1000] = (float(m.group(4)) + float(m.group(5))) / 2

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    marks = {c: Marks(gr) for c, gr in g.items()}
    venues = {c: [venue_series(ext[(v, c)]) for v in ("binance", "bybit") if ext.get((v, c))] for c in g}
    have = sorted(c for c in g if len(venues.get(c, [])) == 2)
    print(f"  grids and quotes ready, {time.time() - t0:.0f}s", file=sys.stderr)
    if not have:
        print("No coin has both Binance and Bybit quotes in these recordings.")
        return

    found = {}
    for w in WINDOWS_S:
        for kind in ("alone", "confirmed"):
            found[(w, kind)] = []
        for c in have:
            got = moves(c, g[c], venues[c], w, SIZES[0][0])
            for kind, evs in got.items():
                found[(w, kind)] += evs
    all_t = [e.t_ms for evs in found.values() for e in evs]
    if not all_t:
        print("No moves found.")
        return
    split = float(np.median(all_t))
    hours = max(len(g[c]) for c in have) / 3600

    L = [f"HYPERLIQUID-ONLY MOVES  {hours:.0f} h · {', '.join(have)} · Binance and Bybit as the rest of the market",
         f"A move = Hyperliquid's mid over the window. Alone = Binance and Bybit each moved under half as far its way. "
         f"Confirmed = both moved 3/4+ as far. One event per coin per {GAP_S}s.", ""]

    L.append("1. WHAT HAPPENS NEXT. Filled 2s late, bps at the mid. For alone moves: the FADE (against the move); for "
             "confirmed: FOLLOWING it. (t from hourly blocks)")
    L.append(f"   {'move':<11}{'size':>6}{'per h':>7}" + "".join(f"{str(h // 60) + 'm' if h >= 60 else str(h) + 's':>15}" for h in HOLDS))
    for kind, fade in (("alone", True), ("confirmed", False)):
        for w in WINDOWS_S:
            for lo, hi, label in SIZES:
                evs = [e for e in found[(w, kind)] if lo <= e.usd < hi]
                if len(evs) < 10:
                    continue
                cells = []
                for h in HOLDS:
                    r = price(evs, marks, fade, 2, h, split)
                    tt = f"{r['t']:.1f}" if r and r["t"] is not None else "-"
                    cells.append("-" if not r else f"{f(r['mid'])} (t {tt})")
                L.append(f"   {kind + ' ' + str(w) + 's':<11}{label:>6}{len(evs) / hours:>7.1f}" + "".join(f"{c:>15}" for c in cells))
        L.append("")

    L.append("2. FADING THE BIGGER LONE MOVES (8+ bps): lateness, leverage, both halves")
    L.append(f"   {'move':<12}{'hold':>5}{'filled':>7}{'n':>6}{'bps (t)':>15}{'win':>6}" + "".join(f"{str(lev) + 'x':>8}" for lev in LEVS)
             + f"{'busts 841x':>11}{'1st half':>10}{'2nd half':>10}")
    for w in WINDOWS_S:
        evs = [e for e in found[(w, "alone")] if e.usd >= 8]
        if len(evs) < 10:
            continue
        for h in (60, 300):
            for lag in LAGS:
                r = price(evs, marks, True, lag, h, split)
                if not r:
                    continue
                tt = f"{r['t']:.1f}" if r["t"] is not None else "-"
                L.append(f"   {'alone ' + str(w) + 's':<12}{str(h // 60) + 'm':>5}{str(lag) + 's':>7}{r['n']:>6}"
                         f"{f(r['mid']) + ' (t ' + tt + ')':>15}{r['win'] * 100:>5.0f}%" + "".join(f"{pct(r['pt'][lev]):>8}" for lev in LEVS)
                         + f"{r['bust'] * 100:>10.1f}%{f(r['first']):>10}{f(r['second']):>10}")
    L.append("")
    L.append("A fade is worth building only if the bps stay positive in both halves at a fill you can manage, and a "
             "Papertrade column is positive.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

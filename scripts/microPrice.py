"""
Micro-price study (Stoikov 2018, "The Micro-Price"): is the book's fair price a better guess of where the mid goes
next than the mid itself or the size-weighted mid? Fitted on older recordings, judged on the last days it never saw.

    python -m scripts.microPrice                                  # BTC, ETH, SOL, HYPE; last 7 days held out
    python -m scripts.microPrice --coins BTC,ETH --test-days 5 --depth 3

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.microPrice > /tmp/microprice.txt 2>&1

How it works: every book snapshot is a state, (imbalance bin, spread in ticks). Counting how states move into each
other, and how the mid moves when they do, gives the expected eventual mid change from each state: the micro-price
adjustment G* (pages 7 to 9 of the paper). The data are mirrored (bids <-> asks) so up and down are treated alike,
which is what makes the sum converge. All moves are in bps of the mid, so coins and price levels compare.

Reading it:
  1. The book: how often each coin's book arrives, its tick, and how often the spread is one tick.
  2. Adjustments: G* by imbalance at a one-tick and a two-tick spread, next to the weighted-mid adjustment, half
     the spread, and what the mid actually did over the next 30s and 60s in the test days (the paper's Figure 4).
  3. The test: how much each guess cuts the squared error of "the mid stays put", per horizon, with a t over hourly
     blocks and both halves of the test days. Positive = better than the mid. Slope = realized move per bp of G*
     (1.0 means G* is the right size); it only means something where the cut is clearly positive.
  4. Tradeable alone? How often the micro-price sits outside the bid and ask at all, and how often it beats a taker
     round trip (the full spread plus 9 bps of fees). The paper's answer: it's a fair price, not a trade.
"""
import argparse
import gzip
import json
import sys
import time
from pathlib import Path

import numpy as np

from config import RECORD_DIR
from ingestion.replay import _first_recv, recording_files

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MAX_GAP_MS = 60_000          # consecutive snapshots further apart than this (recorder off, feed down) don't count
TAKER_RT_BPS = 9.0           # 4.5 bps in and 4.5 out at Hyperliquid's base taker fee


# ----------------------------------------------------------------------------------------------- loading

def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def load_books(paths: list[str], coins: list[str], days: float, depth: int) -> dict[str, np.ndarray]:
    """Top-of-book snapshots per coin as rows of (time ms, bid, ask, bid size, ask size), oldest first.
    Sizes are summed over the best `depth` levels of each side (depth 1 = the paper's Level I)."""
    files = recording_files(paths)
    if not files:
        sys.exit(f"no recordings found in {', '.join(paths)}")
    end = (_first_recv(files[-1]) or 0) + HOUR_MS
    since = end - days * DAY_MS
    rows: dict[str, list[tuple]] = {c: [] for c in coins}
    t0 = time.time()
    for i, path in enumerate(files):
        first = _first_recv(path) or 0
        if first < since - HOUR_MS:
            continue
        with _open(path) as fh:
            for line in fh:
                if '"l2Book"' not in line:
                    continue
                try:
                    d = json.loads(line)["msg"]["data"]
                    out = rows.get(d["coin"])
                    if out is None:
                        continue
                    t = int(d["time"])
                    bids, asks = d["levels"]
                    if t < since or not bids or not asks:
                        continue
                    pb, pa = float(bids[0]["px"]), float(asks[0]["px"])
                    qb = sum(float(lv["sz"]) for lv in bids[:depth])
                    qa = sum(float(lv["sz"]) for lv in asks[:depth])
                except (KeyError, ValueError, TypeError, IndexError, json.JSONDecodeError):
                    continue
                if pa > pb > 0 and qb > 0 and qa > 0:
                    out.append((t, pb, pa, qb, qa))
        if i % 24 == 0:
            print(f"  read {i + 1}/{len(files)} files ({time.time() - t0:.0f}s)", file=sys.stderr)
    books = {}
    for coin, r in rows.items():
        if not r:
            continue
        a = np.array(r, dtype=float)
        a = a[np.argsort(a[:, 0], kind="stable")]
        keep = np.r_[True, np.diff(a[:, 0]) > 0]          # one snapshot per exchange timestamp
        books[coin] = a[keep]
    return books


# ----------------------------------------------------------------------------------------------- states

def tick_of(spread: np.ndarray) -> float:
    """The coin's tick: the smallest spread that shows up at least 0.1% of the time (ignores the odd stray quote)."""
    vals, counts = np.unique(np.round(spread[spread > 0], 10), return_counts=True)
    ok = vals[counts >= max(1, 0.001 * counts.sum())]
    return float(ok.min() if len(ok) else vals.min())


def describe(a: np.ndarray, n: int, m: int) -> dict:
    """Per-snapshot quantities: mid, imbalance, spread in ticks and bps, and the (spread, imbalance) state."""
    t, pb, pa, qb, qa = a.T
    mid = (pa + pb) / 2
    spread = pa - pb
    imb = qb / (qb + qa)
    ticks = np.rint(spread / tick_of(spread)).astype(int)
    ib = np.minimum((imb * n).astype(int), n - 1)
    sb = np.clip(ticks, 1, m) - 1
    return {"t": t, "mid": mid, "imb": imb, "ticks": ticks, "spread_bps": spread / mid * 1e4,
            "ib": ib, "sb": sb, "state": sb * n + ib}


# ----------------------------------------------------------------------------------------------- the model

def fit(d: dict, mask: np.ndarray, n: int, m: int) -> tuple[np.ndarray, np.ndarray, bool]:
    """G*: the expected eventual mid change (bps) from each state, fitted on the snapshots in `mask`.

    Transitions are consecutive snapshots under MAX_GAP_MS apart. Each is mirrored (imbalance bin j <-> n-1-j,
    mid change -> minus it), which guarantees B* G1 = 0 (Theorem 3.1) so the sum converges."""
    N = n * m
    t, mid, ib, sb = d["t"], d["mid"], d["ib"], d["sb"]
    ok = mask[:-1] & mask[1:] & (np.diff(t) <= MAX_GAP_MS)
    i = np.flatnonzero(ok)
    dm = (mid[i + 1] - mid[i]) / mid[i] * 1e4
    x, y = sb[i] * n + ib[i], sb[i + 1] * n + ib[i + 1]
    xm, ym = sb[i] * n + (n - 1 - ib[i]), sb[i + 1] * n + (n - 1 - ib[i + 1])
    x, y, dm = np.r_[x, xm], np.r_[y, ym], np.r_[dm, -dm]

    count = np.bincount(x, minlength=N).astype(float)
    seen = count > 0
    still = dm == 0
    Q = np.zeros((N, N))
    T = np.zeros((N, N))
    np.add.at(Q, (x[still], y[still]), 1.0)                # mid unchanged, state moves x -> y
    np.add.at(T, (x[~still], y[~still]), 1.0)              # mid changed, state moves x -> y
    r = np.bincount(x, weights=dm, minlength=N)            # = R K in the paper: expected mid jump in one step
    Q[seen] /= count[seen, None]
    T[seen] /= count[seen, None]
    r[seen] /= count[seen]

    A = np.eye(N) - Q
    G1 = np.linalg.lstsq(A, r, rcond=None)[0]              # G1 = (1 - Q)^-1 R K
    B = np.linalg.lstsq(A, T, rcond=None)[0]               # B  = (1 - Q)^-1 T
    G, term, converged = G1.copy(), G1.copy(), False
    for _ in range(2000):                                  # G* = G1 + sum_i B^i G1
        term = B @ term
        G += term
        if np.abs(term).max() < 1e-9:
            converged = True
            break
    G[~seen] = 0.0                                         # never seen in training: no adjustment (the mid)
    return G, seen, converged


def future_change(d: dict, idx: np.ndarray, h_s: int) -> np.ndarray:
    """The mid's move (bps) h_s seconds after each snapshot in idx; NaN when the book went quiet over the gap."""
    t, mid = d["t"], d["mid"]
    target = t[idx] + h_s * 1000
    j = np.searchsorted(t, target, side="right") - 1
    good = (j > idx) & (target - t[np.clip(j, 0, None)] <= MAX_GAP_MS) & (target <= t[-1])
    out = np.full(len(idx), np.nan)
    out[good] = (mid[j[good]] - mid[idx[good]]) / mid[idx[good]] * 1e4
    return out


def hourly_t(t_ms: np.ndarray, v: np.ndarray) -> float | None:
    """t of the mean of v, with hours as the independent blocks (overlapping horizons inflate per-snapshot t's)."""
    if len(v) < 3:
        return None
    _, inv = np.unique(t_ms // HOUR_MS, return_inverse=True)
    per_hour = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(per_hour) < 3 or per_hour.std(ddof=1) == 0:
        return None
    return float(per_hour.mean() / (per_hour.std(ddof=1) / np.sqrt(len(per_hour))))


# ----------------------------------------------------------------------------------------------- output

def fmt(x, d=2, sign=True):
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--days", type=float, default=30, help="how much recording to read, newest first")
    ap.add_argument("--test-days", type=float, default=7, help="held out at the end; the model never sees these")
    ap.add_argument("--bins", type=int, default=6, help="imbalance bins")
    ap.add_argument("--spreads", type=int, default=3, help="spread states: 1, 2, ... ticks, the last one and wider")
    ap.add_argument("--depth", type=int, default=1, help="book levels per side in the imbalance (1 = the paper)")
    ap.add_argument("--horizons", default="10,30,60,300", help="seconds ahead to score")
    args = ap.parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    horizons = [int(h) for h in args.horizons.split(",")]
    n, m = args.bins, args.spreads

    print(f"reading book snapshots ({args.days:g} days, depth {args.depth})...", file=sys.stderr)
    books = load_books(args.paths, coins, args.days, args.depth)
    if not books:
        sys.exit("no book snapshots for those coins")

    results = {}
    for coin, a in books.items():
        d = describe(a, n, m)
        split = d["t"][-1] - args.test_days * DAY_MS
        train, test = d["t"] <= split, d["t"] > split
        if train.sum() < 1000 or test.sum() < 1000:
            print(f"{coin}: too little data either side of the split ({train.sum()} / {test.sum()}), skipped")
            continue
        G, seen, converged = fit(d, train, n, m)
        results[coin] = (d, train, test, G, seen, converged)

    first = min(r[0]["t"][0] for r in results.values())
    last = max(r[0]["t"][-1] for r in results.values())
    print(f"Micro-price study · {time.strftime('%d %b %H:%M', time.gmtime(first / 1000))} to "
          f"{time.strftime('%d %b %H:%M', time.gmtime(last / 1000))} UTC · last {args.test_days:g} days held out · "
          f"{n} imbalance bins · spreads 1..{m}+ ticks · depth {args.depth}")

    # 1. the book
    print("\n1. The book (all days)")
    print(f"{'coin':<6}{'snapshots':>11}{'gap p50':>9}{'gap p90':>9}{'tick':>12}{'1 tick':>8}{'2 ticks':>9}"
          f"{'3+':>7}{'spread p50':>12}")
    for coin, (d, *_rest) in results.items():
        gaps = np.diff(d["t"]) / 1000
        tk = d["ticks"]
        tick = tick_of(books[coin][:, 2] - books[coin][:, 1])
        print(f"{coin:<6}{len(d['t']):>11,}{np.median(gaps):>8.1f}s{np.percentile(gaps, 90):>8.1f}s{tick:>12g}"
              f"{(tk == 1).mean() * 100:>7.0f}%{(tk == 2).mean() * 100:>8.0f}%{(tk >= 3).mean() * 100:>6.0f}%"
              f"{np.median(d['spread_bps']):>9.2f}bps")

    # 2. adjustments, like the paper's Figure 4
    print("\n2. Adjustments by imbalance (bps of the mid). G* fitted on the older days; 'actual' = the mid's mean "
          "move in the test days")
    for coin, (d, train, test, G, seen, converged) in results.items():
        idx = np.flatnonzero(test)
        y30, y60 = future_change(d, idx, 30), future_change(d, idx, 60)
        note = "" if converged else "  (sum did not fully converge)"
        for s in range(min(m, 2)):
            in_s = d["sb"][idx] == s
            if in_s.sum() < 200:
                continue
            print(f"\n{coin}, spread {s + 1} tick{'s' if s else ''}  ({in_s.mean() * 100:.0f}% of test snapshots)"
                  f"{note}")
            print(f"  {'imbalance':<12}{'n':>8}{'G*':>8}{'wmid':>8}{'half sprd':>11}{'actual 30s':>12}"
                  f"{'actual 60s':>12}")
            for j in range(n):
                sel = in_s & (d["ib"][idx] == j)
                if sel.sum() < 30:
                    continue
                k = idx[sel]
                wmid = ((d["imb"][k] - 0.5) * d["spread_bps"][k]).mean()
                half = (d["spread_bps"][k] / 2).mean()
                a30, a60 = y30[sel], y60[sel]
                print(f"  {j / n:.2f}-{(j + 1) / n:.2f}   {sel.sum():>8,}{fmt(G[s * n + j]):>8}{fmt(wmid):>8}"
                      f"{fmt(half, sign=False):>11}{fmt(np.nanmean(a30)):>12}{fmt(np.nanmean(a60)):>12}")

    # 3. the test
    print("\n3. Out-of-sample test: cut in squared error vs 'the mid stays put' (positive = better)")
    print(f"{'coin':<6}{'ahead':>6}{'n':>9}{'wmid':>9}{'micro':>9}{'t':>7}{'1st half':>10}{'2nd half':>10}"
          f"{'slope':>8}")
    for coin, (d, train, test, G, seen, converged) in results.items():
        idx = np.flatnonzero(test)
        g = G[d["state"][idx]]
        w = (d["imb"][idx] - 0.5) * d["spread_bps"][idx]
        mid_t = d["t"][-1] - args.test_days * DAY_MS / 2
        for h in horizons:
            y = future_change(d, idx, h)
            ok = np.isfinite(y)
            if ok.sum() < 100:
                continue
            yy, gg, ww, tt = y[ok], g[ok], w[ok], d["t"][idx][ok]
            e0, ew, eg = yy ** 2, (yy - ww) ** 2, (yy - gg) ** 2
            cut = lambda e, sel=slice(None): 1 - e[sel].mean() / e0[sel].mean()
            h1, h2 = tt <= mid_t, tt > mid_t
            slope = (gg @ yy) / (gg @ gg) if (gg @ gg) > 0 else np.nan
            print(f"{coin:<6}{h:>5}s{ok.sum():>9,}{pct(cut(ew)):>9}{pct(cut(eg)):>9}"
                  f"{fmt(hourly_t(tt, e0 - eg), 1):>7}{pct(cut(eg, h1)):>10}{pct(cut(eg, h2)):>10}"
                  f"{fmt(slope, 2, sign=False):>8}")

    # 4. tradeable alone?
    print("\n4. Tradeable on its own? (test days)")
    print(f"{'coin':<6}{'|G*| p50':>10}{'|G*| p99':>10}{'half sprd p50':>15}{'outside bid/ask':>17}"
          f"{'beats taker round trip':>24}")
    for coin, (d, train, test, G, seen, converged) in results.items():
        idx = np.flatnonzero(test)
        g = np.abs(G[d["state"][idx]])
        sp = d["spread_bps"][idx]
        outside = (g > sp / 2).mean()
        beats = (g > sp + TAKER_RT_BPS).mean()
        print(f"{coin:<6}{np.median(g):>10.2f}{np.percentile(g, 99):>10.2f}{np.median(sp / 2):>15.2f}"
              f"{outside * 100:>16.2f}%{beats * 100:>23.3f}%")
    print("\nA micro-price that beats the mid is a better yardstick and a better input to a combined forecast. "
          "It is only a trade on its own where the last column is meaningfully above zero.")


if __name__ == "__main__":
    main()

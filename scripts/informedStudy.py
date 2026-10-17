"""
Informed flow, first study (models 1 and 2 in engine/informed.py): do slow informed traders exist on Hyperliquid?
Finds every hidden order (a wallet taking one side in pieces over a minute or more), measures what price did over
the next 5 to 60 minutes, and checks whether some wallets are reliably right at those horizons.

    python -m scripts.informedStudy                      # last 14 days of recordings, BTC ETH SOL HYPE
    python -m scripts.informedStudy --days 0             # every recording on the server

On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.informedStudy > /tmp/informed.txt 2>&1 &'

Reading it:
    1-2  what hidden orders look like and what price does after them, on average
    3    the key question: is a wallet that was right in the first half of the data still right in the second half,
         at 15-60 minutes? If not, there are no slow informed wallets to follow
    4    the honest test: each day, wallets are scored only on earlier days, then that day's hidden orders from the
         best (and worst) are followed. Worth building only if positive on most days and in both halves
    5    the shape of the move after informed orders vs everyone (holds = information, fades = pressure)
"""
import argparse
import json
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.informed import HiddenOrders, follow, rank_corr, scores, shrink_k
from engine.walletstudy import taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.bigMovers import f, pct, short, t_hourly
from scripts.survivalStudy import grids

DAY_MS = 86_400_000
HS = (60, 300, 900, 1800, 3600)
LAG = 6
LEV = 200


def load(files, coins: set[str], min_usd: float):
    """One pass over the recordings: each coin's one-second grid and every hidden order (liquidators left out)."""
    ho = HiddenOrders(min_usd=min_usd)

    def on_line(line: str) -> None:
        if '"channel":"trades"' not in line:
            return
        try:
            data = json.loads(line)["msg"]["data"]
        except (ValueError, KeyError, TypeError):
            return
        for tr in data or []:
            coin = tr.get("coin")
            who = taker_of(tr) if coin in coins else None
            if who is None:
                continue
            w = who[0].lower()
            if w in LIQUIDATOR_ADDRESSES:
                continue
            try:
                ho.add(w, coin, who[1], int(tr["time"]), float(tr["px"]) * float(tr["sz"]))
            except (KeyError, TypeError, ValueError):
                pass

    g = grids(files, coins, on_other=on_line)
    return g, [h for h in ho.finish() if h.coin in g], ho


def lab(h):
    return f"{h // 60}m"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14, help="0 = every recording")
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--min-usd", type=float, default=50_000)
    ap.add_argument("--history-h", type=float, default=72, help="hours before the first test day")
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * DAY_MS]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    t0 = time.time()
    g, orders, ho = load(files, coins, a.min_usd)
    print(f"  {len(orders):,} hidden orders, {time.time() - t0:.0f}s", file=sys.stderr)
    if len(orders) < 50:
        print("Too few hidden orders found.")
        return
    wl, cl = {}, {}
    W = np.array([wl.setdefault(h.wallet, len(wl)) for h in orders])
    C = np.array([cl.setdefault(h.coin, len(cl)) for h in orders])
    coin_names = list(cl)
    S = np.array([h.side for h in orders])
    TD = np.array([h.t_detect for h in orders], dtype=np.int64)
    TF = np.array([h.t_first for h in orders], dtype=np.int64)
    TE = np.array([h.t_last for h in orders], dtype=np.int64)
    USD = np.array([h.usd_detect for h in orders])
    TOTAL = np.array([h.usd for h in orders])
    REG = np.array([h.regular_detect for h in orders])
    PIECES = np.array([h.pieces for h in orders])
    n_w = len(wl)
    trade_coins = np.isin(C, [cl[c] for c in ("BTC", "ETH") if c in cl])

    mo, pt = {}, {}
    for h in HS:
        mo[h], pt[h] = np.full(len(orders), np.nan), np.full(len(orders), np.nan)
        for ci, c in enumerate(coin_names):
            k = np.nonzero(C == ci)[0]
            mo[h][k], pt[h][k] = follow(g[c], TD[k], S[k], LAG, h, LEV, c)
    pre = np.full(len(orders), np.nan)
    for ci, c in enumerate(coin_names):
        gr = g[c]
        t0s = int(gr.t[0])
        k = np.nonzero(C == ci)[0]
        i, j = TF[k] // 1000 - t0s, TD[k] // 1000 - t0s
        ok = (i >= 0) & (j < len(gr)) & (i < len(gr))
        kk = k[ok]
        pre[kk] = S[kk] * (gr.mid[j[ok]] / gr.mid[i[ok]] - 1) * 1e4
    print(f"  marked, {time.time() - t0:.0f}s", file=sys.stderr)

    hours = max(len(gr) for gr in g.values()) / 3600
    span_days = hours / 24
    split_all = float(np.median(TD))

    def line(name, sel, h, show_pt=True):
        v = mo[h][sel]
        ok = np.isfinite(v)
        v, tt = v[ok], TD[sel][ok]
        if len(v) < 10:
            return f"   {name:<34}{'too few':>8}"
        p = pt[h][sel][ok]
        ts = t_hourly(tt, v)
        s = f"{ts:.1f}" if ts is not None else "-"
        first, second = v[tt <= split_all], v[tt > split_all]
        return (f"   {name:<34}{len(v):>6}{f(v.mean()) + ' (t ' + s + ')':>16}{(v > 0).mean() * 100:>5.0f}%"
                + (f"{pct(p.mean()):>8}{(p <= -1).mean() * 100:>6.0f}%" if show_pt else "")
                + f"{f(first.mean()) if len(first) else '-':>9}{f(second.mean()) if len(second) else '-':>9}")

    head = f"   {'':<34}{'n':>6}{'bps (t)':>16}{'win':>6}{str(LEV) + 'x':>8}{'busts':>6}{'1st':>9}{'2nd':>9}"
    L = [f"INFORMED FLOW, STUDY 1  {hours:.0f} h · {', '.join(sorted(g))} · hidden order = one wallet taking one side "
         f"in {ho.min_pieces}+ pieces over {ho.min_span_ms // 1000}s+, no gap over {ho.gap_ms // 1000}s, spotted at "
         f"${a.min_usd:,.0f} · in {LAG}s after it's spotted, its way, at the mid", ""]

    # 1
    L.append("1. THE HIDDEN ORDERS")
    L.append(f"   {len(orders):,} ({len(orders) / span_days:.0f} a day) from {n_w:,} wallets · TWAP-like (30s beat) "
             f"{REG.mean() * 100:.0f}% · BTC/ETH {trade_coins.mean() * 100:.0f}%")
    L.append(f"   at spotting: median ${np.median(USD):,.0f} in {np.median([h.pieces_detect for h in orders]):.0f} pieces · "
             f"in the end: median ${np.median(TOTAL):,.0f}, {np.median(PIECES):.0f} pieces, lasting "
             f"{np.median((TE - TF) / 60000):.0f} min · price had already moved {f(np.nanmean(pre))} bps its way "
             f"before it was spotted")
    L.append("")

    # 2
    L.append("2. WHAT PRICE DOES AFTER THEM (everyone), bps its way by hold")
    L.append(f"   {'':<34}" + "".join(f"{lab(h):>12}" for h in HS))
    groups = [("all", np.ones(len(orders), bool)), ("irregular (not a TWAP)", ~REG), ("TWAP-like", REG),
              (f"${a.min_usd / 1000:.0f}-250k at spotting", USD < 250_000), ("$250k+ at spotting", USD >= 250_000),
              ("BTC and ETH only", trade_coins)]
    for name, sel in groups:
        cells = []
        for h in HS:
            v = mo[h][sel]
            v = v[np.isfinite(v)]
            ts = t_hourly(TD[sel][np.isfinite(mo[h][sel])], v)
            cells.append(f"{f(v.mean()) if len(v) else '-'} ({ts:.1f})" if ts is not None and len(v) else "-")
        L.append(f"   {name:<34}" + "".join(f"{c:>12}" for c in cells))
    L.append("   (t in brackets)")
    L.append("")

    # 3
    L.append("3. DOES BEING RIGHT REPEAT? Wallets with 5+ hidden orders in each half of the data: first-half average vs "
             "second-half average")
    first_half = TD <= split_all
    L.append(f"   {'hold':<8}{'wallets':>8}{'rank match':>12}{'top fifth':>12}{'middle':>10}{'bottom fifth':>14}"
             f"   (second-half bps, wallets grouped by first-half score)")
    for h in HS[1:]:
        v = mo[h]
        ok = np.isfinite(v)
        k = shrink_k(W[ok & first_half], v[ok & first_half])
        a1 = scores(W, np.where(first_half, v, np.nan), np.zeros(len(v)), 0, n_w, k)
        a2 = scores(W, np.where(~first_half, v, np.nan), np.zeros(len(v)), 0, n_w, k)
        both = (a1["n"] >= 5) & (a2["n"] >= 5)
        idx = np.nonzero(both)[0]
        if len(idx) < 10:
            L.append(f"   {lab(h):<8}{len(idx):>8}   too few")
            continue
        rc = rank_corr(a1["score"][idx], a2["mean"][idx])
        q = np.quantile(a1["score"][idx], [0.2, 0.8])
        top, bot = idx[a1["score"][idx] >= q[1]], idx[a1["score"][idx] <= q[0]]
        mid = idx[(a1["score"][idx] > q[0]) & (a1["score"][idx] < q[1])]
        wavg = lambda ii: float(np.sum(a2["mean"][ii] * a2["n"][ii]) / np.sum(a2["n"][ii])) if len(ii) else np.nan
        L.append(f"   {lab(h):<8}{len(idx):>8}{rc:>+12.2f}{f(wavg(top)):>12}{f(wavg(mid)):>10}{f(wavg(bot)):>14}"
                 f"   (k = {k:.0f})")
    L.append("   Rank match near 0 and top fifth no better than the rest = no slow informed wallets at that hold.")
    L.append("")

    # 4
    first = int(TD.min()) + int(a.history_h * 3.6e6)
    d0 = -(-first // DAY_MS) * DAY_MS
    last = int(TD.max())
    days = [(d, min(d + DAY_MS, last)) for d in range(d0, last, DAY_MS) if min(d + DAY_MS, last) - d >= 6 * 3_600_000]
    L.append(f"4. FOLLOWING THE BEST, FADING THE WORST, walk-forward over {len(days)} test days (scores only from "
             f"earlier days)")
    L.append(head.replace("   " + " " * 34, "   " + f"{'':<34}"))
    best_sets = {}
    for h in (900, 1800, 3600):
        v = mo[h]
        ready = TD + (LAG + h + 1) * 1000
        fol = {x: [] for x in (2.0, 5.0, 10.0)}
        fade = {x: [] for x in (2.0, 5.0)}
        for dd0, dd1 in days:
            past = ready <= dd0
            k = shrink_k(W[past], v[past])
            sc = scores(W, v, ready, dd0, n_w, k)["score"]
            today = np.nonzero((TD >= dd0) & (TD < dd1))[0]
            for x in fol:
                fol[x] += list(today[sc[W[today]] >= x])
            for x in fade:
                fade[x] += list(today[sc[W[today]] <= -x])
        for x, ii in fol.items():
            ii = np.array(ii, dtype=int)
            best_sets[(h, x)] = ii
            L.append(line(f"follow score {x:g}+, hold {lab(h)}", ii, h))
            if len(ii):
                L.append(line(f"   on BTC/ETH only", ii[trade_coins[ii]], h))
        for x, ii in fade.items():
            ii = np.array(ii, dtype=int)
            if len(ii) < 10:
                L.append(f"   {'fade score -' + format(x, 'g') + ' or worse, ' + lab(h):<34}{'too few':>8}")
                continue
            v2 = -mo[h][ii]
            ok = np.isfinite(v2)
            ts = t_hourly(TD[ii][ok], v2[ok])
            L.append(f"   {'fade score -' + format(x, 'g') + ' or worse, ' + lab(h):<34}{ok.sum():>6}"
                     f"{f(v2[ok].mean()) + ' (t ' + (f'{ts:.1f}' if ts is not None else '-') + ')':>16}"
                     f"{(v2[ok] > 0).mean() * 100:>5.0f}%")
        L.append("")
    L.append("   Per day, follow score 5+, hold 30m (bps, orders): " + " · ".join(
        f"{time.strftime('%a', time.gmtime(dd0 / 1000))} "
        + (lambda ii: f"{f(np.nanmean(mo[1800][ii])) if len(ii) else '-'} ({len(ii)})")(
            np.array([i for i in best_sets.get((1800, 5.0), []) if dd0 <= TD[i] < dd1], dtype=int))
        for dd0, dd1 in days))
    L.append("")

    # 5
    L.append("5. THE SHAPE OF THE MOVE: after hidden orders from wallets scoring 5+ at 30m (walk-forward) vs everyone")
    L.append(f"   {'':<34}{'before':>9}" + "".join(f"{lab(h):>9}" for h in HS))
    for name, sel in (("informed (score 5+ at 30m)", best_sets.get((1800, 5.0), np.array([], int))),
                      ("everyone", np.arange(len(orders)))):
        if len(sel) < 10:
            L.append(f"   {name:<34}too few")
            continue
        L.append(f"   {name:<34}{f(np.nanmean(pre[sel])):>9}" + "".join(f"{f(np.nanmean(mo[h][sel])):>9}" for h in HS))
    L.append("   Holding or growing from 5m to 60m = information. Peaking early and fading = pressure (fade it).")
    L.append("")
    top = sorted(set(W[best_sets.get((1800, 5.0), np.array([], int))]), key=lambda w: -np.sum(W[best_sets[(1800, 5.0)]] == w))[:5]
    if top:
        names = {i: w for w, i in wl.items()}
        L.append("   Most-followed informed wallets (30m, score 5+): " + ", ".join(
            f"{short(names[w])} {int(np.sum(W[best_sets[(1800, 5.0)]] == w))}" for w in top))
    L.append("")
    L.append("Worth building the informed basket on only if section 3 shows being right repeats at 15-60 minutes and a "
             "row in section 4 is positive on most days and in both halves.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

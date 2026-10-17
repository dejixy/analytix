"""
Big movers v4: the v3 test again, with honest timing. v3 timed each decision from its first fill but only kept the
decisions that went on to reach $1,000, so it acted seconds before anyone could know (Run 11 in the findings). Here
every alert happens at a moment the bot could really act, and wallets are picked only on days before the test day.

    python -m scripts.bigMovers4                       # 7 days, first 36 h as history, 30s hold
    python -m scripts.bigMovers4 --lag 6 --hold 30

On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.bigMovers4 > /tmp/bm4.txt 2>&1 &'

Reading it: section 1 checks the code against what we already know. Section 2 is the answer: each way of choosing
WHEN to act, crossed with each way of choosing WHO to follow, filled 6s late. A row only counts if it's positive on
most test days and in both halves. Section 3 has the detail for the best rows. 16 rows are compared, so one of them
looking good by luck is quite possible: the day and half checks are what guard against that.
"""
import argparse
import json
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.bigmovers import DAY_MS, Decisions, merge_at, pick, price, score_by_wallet
from engine.walletstudy import Marks, taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.bigMovers import f, pct, short, t_hourly
from scripts.survivalStudy import grids

RULES = [("reaches $1k (the bot now)", "cross", None),
         ("first order $100+", "first", 100.0),
         ("first order $500+", "first", 500.0),
         ("first order $1k+", "first", 1000.0)]
PICKS = [("60s from its trade, 8+ (the bot now)", "old", 8.0, 2.5),
         ("catchable at 6s, 0+ bps", "follow", 0.0, 2.0),
         ("catchable at 6s, 1+ bps", "follow", 1.0, 2.0),
         ("catchable at 6s, 2+ bps", "follow", 2.0, 2.0)]
LAGS = (2, 4, 6, 10)
LEV = 200


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE", help="coins wallets are scored on")
    ap.add_argument("--trade", default="BTC,ETH", help="coins alerts are taken on (Papertrade's)")
    ap.add_argument("--min-usd", type=float, default=1000)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--history-h", type=float, default=36, help="hours of data before the first test day")
    ap.add_argument("--lag", type=int, default=6, help="seconds from the alert to your fill")
    ap.add_argument("--hold", type=int, default=30)
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * DAY_MS]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    trade = {c.strip().upper() for c in a.trade.split(",") if c.strip()} & coins
    keep_first = min(r[2] for r in RULES if r[2] is not None)
    dec = Decisions(a.min_usd, keep_first)

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
                dec.add(coin, w, int(tr["time"]), who[1], float(tr["px"]) * float(tr["sz"]))
            except (KeyError, TypeError, ValueError):
                pass

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    D = dec.finish()
    if not len(D.t_first):
        print("No trades with wallets found in these recordings.")
        return
    marks = {c: Marks(gr) for c, gr in g.items()}
    nw = len(D.wallets)
    on_trade = np.isin(D.coin, [i for i, c in enumerate(D.coins) if c in trade])
    print(f"  {len(D.t_first):,} decisions, {time.time() - t0:.0f}s", file=sys.stderr)

    # when each decision can be acted on, per rule (-1 = never)
    trig = {}
    for _, kind, x in RULES:
        trig[(kind, x)] = D.t_cross if kind == "cross" else np.where(D.usd_first >= x, D.t_first, -1)
    flawed = np.where(D.usd >= a.min_usd, D.t_first, -1)          # v3: first fill, but only if it reached $1k later

    def follow_bps(T, lag, hold):
        out = np.full(len(T), np.nan)
        for ci, c in enumerate(D.coins):
            mk = marks.get(c)
            k = np.nonzero((D.coin == ci) & (T >= 0))[0]
            if mk is None or not len(k):
                continue
            r = mk.follow(T[k], D.side[k], lag, hold, (LEV,), c)
            out[k[r["idx"]]] = r["mid"]
        return out

    # who: the old score (60s from the trade, decisions that reached $1k), or what was catchable at your lag
    big = D.usd >= a.min_usd
    old_v = np.full(len(D.t_first), np.nan)
    for ci, c in enumerate(D.coins):
        mk = marks.get(c)
        k = np.nonzero((D.coin == ci) & big)[0]
        if mk is not None and len(k):
            old_v[k] = mk.markout(D.t_first[k], D.side[k], 60)
    old_ready = np.where(big, D.t_first + 61_000, -1)                # known once the decision ended and 60s passed
    fol = {key: follow_bps(T, a.lag, a.hold) for key, T in trig.items()}
    fol_ready = {key: np.where(T >= 0, T + (a.lag + a.hold + 1) * 1000, -1) for key, T in trig.items()}
    print(f"  scored, {time.time() - t0:.0f}s", file=sys.stderr)

    first = int(D.t_first.min()) + int(a.history_h * 3.6e6)
    day0 = -(-first // DAY_MS) * DAY_MS
    last = int(D.t_first.max())
    days = [(d, min(d + DAY_MS, last)) for d in range(day0, last, DAY_MS) if min(d + DAY_MS, last) - d >= 6 * 3_600_000]
    if not days:
        print("Not enough data: need history plus at least 6 hours of a test day.")
        return
    test_h = sum(d1 - d0 for d0, d1 in days) / 3.6e6

    def walk(T, key, kind, cutoff, t_min, exclude=()):
        out = []
        for d0, d1 in days:
            if kind == "old":
                sc = score_by_wallet(D.wallet, old_v, old_ready, d0, nw)
            else:
                sc = score_by_wallet(D.wallet, fol[key], fol_ready[key], d0, nw)
            ids = np.setdiff1d(pick(sc, a.min_n, t_min, cutoff), np.array(list(exclude), dtype=np.int64))
            idx = np.nonzero((T >= d0) & (T < d1) & on_trade & np.isin(D.wallet, ids))[0]
            out += merge_at(T, D.side, D.coin, D.wallet, idx, D.coins)
        return out

    def stats(als, lag, hold):
        if not als:
            return None
        p = price(als, marks, lag, hold, (LEV,))
        ok = np.isfinite(p["mid"])
        if not ok.any():
            return None
        m, tt = p["mid"][ok], np.array([x.t_ms for x in als])[ok]
        split = np.median(tt)
        per_day = [m[(tt >= d0) & (tt < d1)] for d0, d1 in days]
        return {"n": int(ok.sum()), "bps": float(m.mean()), "t": t_hourly(tt, m), "win": float((m > 0).mean()),
                "pt": float(np.mean(p["pt"][LEV][ok])), "busts": float((p["pt"][LEV][ok] <= -1).mean()),
                "days_up": sum(1 for x in per_day if len(x) and x.mean() > 0), "days": sum(1 for x in per_day if len(x)),
                "first": float(m[tt <= split].mean()), "second": float(m[tt > split].mean()) if (tt > split).any() else np.nan,
                "per_day": [(float(x.mean()) if len(x) else None, len(x)) for x in per_day], "mids": m, "als": als}

    def tt(s):
        return f"{s['t']:.1f}" if s and s["t"] is not None else "-"

    L = [f"BIG MOVERS v4, HONEST TIMING  {len(days)} test days ({test_h:.0f} h) after {a.history_h:g} h of history · "
         f"wallets scored on {', '.join(sorted(coins & set(g)))}, alerts on {', '.join(sorted(trade))} · "
         f"{a.min_n}+ decisions to be picked · held {a.hold}s", ""]

    # 1. the check
    old_pick = PICKS[0]
    L.append("1. CHECKING THE CODE: the bot's wallet list, two timings. bps at the mid, held "
             f"{a.hold}s (t from hourly blocks)")
    L.append(f"   {'timing':<44}{'alerts':>7}" + "".join(f"{str(g_) + 's late':>15}" for g_ in LAGS))
    for name, T in (("v3's (first fill, if it later reached $1k)", flawed), ("the bot's (when it reaches $1k)", trig[("cross", None)])):
        als = walk(T, None, "old", old_pick[2], old_pick[3])
        cells = []
        for lag in LAGS:
            s = stats(als, lag, a.hold)
            cells.append(f"{f(s['bps'])} (t {tt(s)})" if s else "-")
        L.append(f"   {name:<44}{len(als):>7}" + "".join(f"{c:>15}" for c in cells))
    L.append("   The first row should look like v3 and the second like the bot check. The gap is the flaw.")
    L.append("")

    # 2. the grid
    L.append(f"2. WHEN x WHO, filled {a.lag}s late, held {a.hold}s. Days up = test days with a positive average.")
    L.append(f"   {'act when':<27}{'follow who':<38}{'/hour':>6}{'bps (t)':>15}{'win':>6}{str(LEV) + 'x':>7}"
             f"{'days up':>9}{'1st half':>10}{'2nd half':>10}")
    rows = []
    for rname, kind, x in RULES:
        key = (kind, x)
        for pname, pkind, cutoff, t_min in PICKS:
            als = walk(trig[key], key, pkind, cutoff, t_min)
            s = stats(als, a.lag, a.hold)
            if not s:
                L.append(f"   {rname:<27}{pname:<38}{0:>6.1f}")
                continue
            rows.append(((rname, pname), key, pkind, cutoff, t_min, s))
            L.append(f"   {rname:<27}{pname:<38}{s['n'] / test_h:>6.1f}{f(s['bps']) + ' (t ' + tt(s) + ')':>15}"
                     f"{s['win'] * 100:>5.0f}%{pct(s['pt']):>7}{str(s['days_up']) + '/' + str(s['days']):>9}"
                     f"{f(s['first']):>10}{f(s['second']):>10}")
        L.append("")

    # 3. the best rows
    good = [r for r in rows if r[5]["n"] >= 40]
    good.sort(key=lambda r: -r[5]["bps"])
    L.append(f"3. THE BEST 3 ROWS (40+ alerts), in detail")
    for (rname, pname), key, pkind, cutoff, t_min, s in good[:3]:
        L.append(f"   {rname} · {pname}")
        lat = []
        for lag in LAGS:
            s2 = stats(s["als"], lag, a.hold)
            lat.append(f"{lag}s {f(s2['bps'])} ({pct(s2['pt'])})" if s2 else f"{lag}s -")
        L.append("     lateness (bps, 200x): " + " · ".join(lat))
        L.append("     each day (bps, alerts): " + " · ".join(
            f"{time.strftime('%a', time.gmtime(d0 / 1000))} {f(m) if m is not None else '-'} ({n})"
            for (d0, _), (m, n) in zip(days, s["per_day"])))
        trig_n = {}
        for al in s["als"]:
            trig_n[al.trigger] = trig_n.get(al.trigger, 0) + 1
        top = sorted(trig_n, key=lambda w: -trig_n[w])
        without = stats(walk(trig[key], key, pkind, cutoff, t_min, exclude=top[:1]), a.lag, a.hold)
        L.append(f"     top starters: " + ", ".join(f"{short(D.wallets[w])} {trig_n[w]}" for w in top[:5])
                 + (f" · without the top one: {f(without['bps'])} bps over {without['n']}" if without else ""))
    L.append("")
    L.append("Worth building only if a row is positive on most days and in both halves, and survives without its top "
             "wallet. Otherwise big-mover alerts can't be followed by hand and should be retired.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

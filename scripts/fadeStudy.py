"""
Informed flow, study 2: fading the reliably wrong. Study 1 found that being right doesn't repeat, but fading the new
hidden orders of wallets whose past orders reversed looked good at 15 minutes (+7.4 bps, t 3.8). This checks it
properly: halves, days, Papertrade, without the top wallets, BTC/ETH only, and against controls, so a plain "big
orders snap back" effect can't pass for a wallet skill.

    python -m scripts.fadeStudy                          # last 14 days, BTC ETH SOL HYPE
    python -m scripts.fadeStudy --days 0

On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.fadeStudy > /tmp/fade.txt 2>&1 &'

Every score is walk-forward: each day, wallets are scored only on hidden orders whose 15 minutes had passed before
the day began. Score = n × mean / (n + k) of what price did its way 15 minutes after each order (negative = it
reversed). Fading = the other side, in 6s after the order is spotted (or later, where shown).

Reading it: section 1 is the answer. The wallet fade passes only if it beats the controls (fading every order, and
fading orders of the same sizes), is positive in both halves and on most days, and survives without its top wallets.
"""
import argparse
import sys
import time

import numpy as np

from config import RECORD_DIR
from engine.informed import GAP_MS, follow, scores, shrink_k
from ingestion.replay import _first_recv, recording_files
from scripts.bigMovers import f, pct, short, t_hourly
from scripts.informedStudy import load

DAY_MS = 86_400_000
LEV = 200
SCORE_H = 900


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14, help="0 = every recording")
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--min-usd", type=float, default=50_000)
    ap.add_argument("--history-h", type=float, default=72)
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
    names = {i: w for w, i in wl.items()}
    coin_names = list(cl)
    S = np.array([h.side for h in orders])
    TD = np.array([h.t_detect for h in orders], dtype=np.int64)
    TE = np.array([h.t_last for h in orders], dtype=np.int64)
    USD = np.array([h.usd_detect for h in orders])
    n_w = len(wl)
    btc_eth = np.isin(C, [cl[c] for c in ("BTC", "ETH") if c in cl])

    def price(t_ms, side, lag, hold):
        """Per order: bps and 200x Papertrade result for going `side` lag seconds after t_ms, out hold later."""
        m, p = np.full(len(orders), np.nan), np.full(len(orders), np.nan)
        for ci, c in enumerate(coin_names):
            k = np.nonzero(C == ci)[0]
            m[k], p[k] = follow(g[c], t_ms[k], side[k], lag, hold, LEV, c)
        return m, p

    follow_15, _ = price(TD, S, 6, SCORE_H)                       # what the scores are made of
    fades = {(lag, hold): price(TD, -S, lag, hold) for lag in (6, 30, 60) for hold in (300, 900, 1800)}
    fades[("end", 900)] = price(TE + GAP_MS, -S, 0, 900)          # once the order is over (known GAP_MS later)
    print(f"  priced, {time.time() - t0:.0f}s", file=sys.stderr)

    first = int(TD.min()) + int(a.history_h * 3.6e6)
    d0 = -(-first // DAY_MS) * DAY_MS
    last = int(TD.max())
    days = [(d, min(d + DAY_MS, last)) for d in range(d0, last, DAY_MS) if min(d + DAY_MS, last) - d >= 6 * 3_600_000]
    ready = TD + (6 + SCORE_H + 1) * 1000
    score = np.full(len(orders), np.nan)                         # each order's wallet score as of its day
    known = np.zeros(len(orders), bool)
    for dd0, dd1 in days:
        past = ready <= dd0
        k = shrink_k(W[past], follow_15[past])
        sc = scores(W, follow_15, ready, dd0, n_w, k)
        today = np.nonzero((TD >= dd0) & (TD < dd1))[0]
        score[today] = sc["score"][W[today]]
        known[today] = sc["n"][W[today]] > 0
    in_test = np.isfinite(score)
    split = float(np.median(TD[in_test])) if in_test.any() else 0

    def stat(sel, key=(6, 900)):
        m, p = fades[key]
        sel = np.asarray(sel)
        ok = np.isfinite(m[sel])
        idx = sel[ok]
        if len(idx) < 10:
            return None
        v, tt = m[idx], TD[idx]
        per_day = [v[(tt >= dd0) & (tt < dd1)] for dd0, dd1 in days]
        ts = t_hourly(tt, v)
        return {"n": len(v), "bps": v.mean(), "t": ts, "win": (v > 0).mean(), "pt": p[idx].mean(),
                "busts": (p[idx] <= -1).mean(), "first": v[tt <= split].mean() if (tt <= split).any() else np.nan,
                "second": v[tt > split].mean() if (tt > split).any() else np.nan,
                "up": sum(1 for x in per_day if len(x) and x.mean() > 0), "days": sum(1 for x in per_day if len(x)),
                "per_day": [(x.mean() if len(x) else None, len(x)) for x in per_day], "idx": idx}

    def row(name, s):
        if not s:
            return f"   {name:<38}{'too few':>8}"
        tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
        return (f"   {name:<38}{s['n']:>6}{s['n'] / max(len(days), 1):>6.0f}{f(s['bps']) + ' (t ' + tt + ')':>16}"
                f"{s['win'] * 100:>5.0f}%{pct(s['pt']):>8}{s['busts'] * 100:>6.0f}%{f(s['first']):>8}{f(s['second']):>8}"
                f"{str(s['up']) + '/' + str(s['days']):>8}")

    head = (f"   {'':<38}{'n':>6}{'/day':>6}{'bps (t)':>16}{'win':>6}{str(LEV) + 'x':>8}{'busts':>7}{'1st':>8}{'2nd':>8}"
            f"{'days +':>8}")
    tested = np.nonzero(in_test)[0]
    L = [f"FADE STUDY  {len(days)} test days · {', '.join(sorted(g))} · {len(orders):,} hidden orders · wallets scored "
         f"walk-forward on 15-minute moves after their orders · fading = the other side, at the mid", ""]

    L.append("1. FADE FOR 15 MIN, in 6s after the order is spotted")
    L.append(head)
    main_sel = tested[score[tested] <= -5]
    for x in (10, 5, 2):
        L.append(row(f"wallets scoring -{x} or worse", stat(tested[score[tested] <= -x])))
    L.append(row("control: every order", stat(tested)))
    L.append(row("control: wallets with no history yet", stat(tested[~known[tested]])))
    L.append(row("wallets scoring +5 or better (faded)", stat(tested[score[tested] >= 5])))
    m15 = fades[(6, 900)][0]
    edges = np.quantile(USD[tested], np.linspace(0, 1, 11))
    b_all = np.clip(np.searchsorted(edges, USD, side="right") - 1, 0, 9)
    if len(main_sel):
        w = np.bincount(b_all[main_sel], minlength=10) / len(main_sel)
        means = np.array([np.nanmean(m15[tested][b_all[tested] == b]) if np.any(b_all[tested] == b) else 0
                          for b in range(10)])
        L.append(f"   {'control: every order, same sizes as -5':<38}{'':>12}{f(float(np.sum(w * means))):>16}"
                 f"   (what fading orders of those sizes makes regardless of wallet)")
    L.append("")

    s = stat(main_sel)
    L.append("2. WALLETS SCORING -5 OR WORSE, in detail")
    if not s:
        L.append("   Too few.")
    else:
        L.append(head)
        for hold in (300, 900, 1800):
            for lag in (6, 30, 60):
                L.append(row(f"in {lag}s late, out after {hold // 60} min", stat(main_sel, (lag, hold))))
        L.append(row("in when the order is over, out 15 min", stat(main_sel, ("end", 900))))
        L.append(row("BTC and ETH only", stat(main_sel[btc_eth[main_sel]])))
        counts = np.bincount(W[s["idx"]], minlength=n_w)
        top = np.argsort(-counts)[:3]
        for k in (1, 3):
            L.append(row(f"without the top {k} wallet{'s' if k > 1 else ''}", stat(main_sel[~np.isin(W[main_sel], top[:k])])))
        L.append("   each day (bps, trades): " + " · ".join(
            f"{time.strftime('%a', time.gmtime(dd0 / 1000))} {f(m) if m is not None else '-'} ({n})"
            for (dd0, _), (m, n) in zip(days, s["per_day"])))
        L.append("   most faded: " + ", ".join(
            f"{short(names[w])} {counts[w]} ({f(np.nanmean(m15[s['idx']][W[s['idx']] == w]))})" for w in top if counts[w]))
    L.append("")
    L.append("Worth building into the informed basket only if the -5 row beats both controls, is positive in both "
             "halves and on most days, survives without its top wallets, and the Papertrade column is positive.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

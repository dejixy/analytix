"""
Big movers v3: the last checks before following big-mover wallets on Papertrade. The maths is in
engine/bigmovers.py (walk-forward, alerts, one trade at a time, bankroll) and engine/walletstudy.py (pricing).

    python -m scripts.bigMovers                                   # 7 days, 5s late, 30s hold, 20% per trade
    python -m scripts.bigMovers --lag 6 --hold 60 --size 0.2 --trades-per-day 30

On the server, from /opt/analytix:
    sudo -u analytix nice -n 19 .venv/bin/python -m scripts.bigMovers > /tmp/bigmovers.txt 2>&1

Every number here comes from days the wallet list had not seen: each test day's list is scored only on the days
before it, the way a list refreshed every day would work live.
"""
import argparse
import json
import sys
import time

import numpy as np

from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.bigmovers import (DAY_MS, Arrays, alerts, bankroll, bootstrap, markouts, one_at_a_time, pick, price,
                              scores)
from engine.walletstudy import Episodes, Marks, taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

LAGS = (2, 3, 4, 5, 6, 8, 10)
HOLDS = (30, 60)
LEVS = (200, 841)
SETUPS = ((841, 15.0), (200, 50.0))         # leverage, starting balance


def short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else str(w)


def f(x, d=1):
    return "-" if x is None or not np.isfinite(x) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.0f}%"


def t_hourly(t_ms: np.ndarray, v: np.ndarray):
    if len(v) < 3:
        return None
    _, inv = np.unique(t_ms // 3_600_000, return_inverse=True)
    m = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(m) < 3 or m.std(ddof=1) == 0:
        return None
    return float(v.mean() / (m.std(ddof=1) / np.sqrt(len(m))))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE", help="coins wallets are scored on")
    ap.add_argument("--trade", default="BTC,ETH", help="coins alerts are taken on (Papertrade's)")
    ap.add_argument("--min-usd", type=float, default=1000)
    ap.add_argument("--score-h", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--t-min", type=float, default=2.5)
    ap.add_argument("--cutoff", type=float, default=6.0, help="big mover = earlier average move of at least this (bps)")
    ap.add_argument("--history-h", type=float, default=48, help="hours of data before the first test day")
    ap.add_argument("--lookback-days", type=float, default=0, help="score on only the last N days (0 = all before)")
    ap.add_argument("--lag", type=int, default=5, help="seconds from the alert to your fill")
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--size", type=float, default=0.2, help="share of the balance put up as margin per trade")
    ap.add_argument("--trades-per-day", type=int, default=30)
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * 86_400_000]
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    trade = {c.strip().upper() for c in a.trade.split(",") if c.strip()} & coins
    eb = Episodes()

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
            try:
                eb.add(coin, who[0].lower(), int(tr["time"]), who[1], float(tr["px"]) * float(tr["sz"]))
            except (KeyError, TypeError, ValueError):
                pass

    t0 = time.time()
    g = grids(files, coins, on_other=on_line)
    eps = [e for e in eb.finish() if e.usd >= a.min_usd and e.wallet not in LIQUIDATOR_ADDRESSES]
    if not eps:
        print("No trades with wallets found in these recordings.")
        return
    marks = {c: Marks(gr) for c, gr in g.items()}
    A = Arrays.of(eps)
    mo = markouts(A, marks, a.score_h)
    trade_ids = np.array([i for i, c in enumerate(A.coins) if c in trade])
    on_trade = np.isin(A.coin, trade_ids)
    print(f"  {len(eps):,} decisions, {time.time() - t0:.0f}s", file=sys.stderr)

    first = int(A.t.min()) + int(a.history_h * 3.6e6)
    day0 = -(-first // DAY_MS) * DAY_MS                                   # first full UTC day after the history
    last = int(A.t.max())
    days = [(d, min(d + DAY_MS, last)) for d in range(day0, last, DAY_MS) if min(d + DAY_MS, last) - d >= 6 * 3_600_000]
    if not days:
        print("Not enough data: need history plus at least 6 hours of a test day.")
        return
    lookback = int(a.lookback_days * DAY_MS) if a.lookback_days else None

    def walk(cutoff, t_min, exclude=()):
        """All test days' alerts, each day's list scored only on what came before it."""
        out, per_day = [], []
        for d0, d1 in days:
            sc = scores(A, mo, d0, a.score_h, since_ms=(d0 - lookback) if lookback else None)
            ids = np.setdiff1d(pick(sc, a.min_n, t_min, cutoff), np.array(list(exclude), dtype=np.int64))
            idx = np.nonzero((A.t >= d0) & (A.t < d1) & on_trade & np.isin(A.wallet, ids))[0]
            als = alerts(A, idx)
            out += als
            per_day.append((d0, d1, len(ids), als))
        return out, per_day

    def stats(als, lag, hold):
        if not als:
            return None
        p = price(als, marks, lag, hold, LEVS)
        ok = np.isfinite(p["mid"])
        m = p["mid"][ok]
        tt = np.array([x.t_ms for x in als])[ok]
        return {"n": int(ok.sum()), "mid": float(m.mean()) if len(m) else np.nan, "t": t_hourly(tt, m),
                "win": float((m > 0).mean()) if len(m) else np.nan,
                "pt": {lev: float(np.nanmean(p["pt"][lev])) for lev in LEVS},
                "bust": float((p["pt"][841][ok] == -1.0).mean()) if ok.any() else np.nan, "raw": p, "ok": ok}

    als, per_day = walk(a.cutoff, a.t_min)
    test_h = sum(d1 - d0 for d0, d1 in days) / 3.6e6
    L = [f"BIG MOVERS v3  {len(days)} test days ({test_h:.0f} h) after {a.history_h:g} h of history · scored on "
         f"{', '.join(sorted(coins & set(g)))} · alerts on {', '.join(sorted(trade))}",
         f"Big mover = {a.min_n}+ decisions, t ≥ {a.t_min:g} and an average move of {a.cutoff:g}+ bps at {a.score_h}s, "
         f"scored only on days before the test day. Alert = big-mover decisions on one coin and side within 30s. "
         f"Main setting: filled {a.lag}s late, held {a.hold}s.", ""]

    # 1. day by day
    L.append("1. EACH TEST DAY (list rebuilt from the days before it)")
    L.append(f"   {'day':<12}{'on list':>8}{'alerts':>8}{'per hour':>9}{'bps':>8}{'win':>6}{'200x':>8}{'841x':>8}")
    for d0, d1, nw, day_als in per_day:
        s = stats(day_als, a.lag, a.hold)
        hrs = (d1 - d0) / 3.6e6
        day = time.strftime("%a %d %b", time.gmtime(d0 / 1000))
        if not s:
            L.append(f"   {day:<12}{nw:>8}{0:>8}")
            continue
        L.append(f"   {day:<12}{nw:>8}{len(day_als):>8}{len(day_als) / hrs:>9.1f}{f(s['mid']):>8}{s['win'] * 100:>5.0f}%"
                 f"{pct(s['pt'][200]):>8}{pct(s['pt'][841]):>8}")
    L.append("")

    # 2. lateness
    L.append(f"2. HOW LATE CAN YOU BE? All {len(als):,} alerts. bps at the mid (t from hourly blocks), win rate, Papertrade "
             f"% of margin per trade, and how often 841x busts")
    for hold in HOLDS:
        L.append(f"   held {hold}s")
        L.append(f"     {'filled':>7}{'bps (t)':>16}{'win':>6}{'200x':>8}{'841x':>8}{'busts 841x':>12}")
        for lag in LAGS:
            s = stats(als, lag, hold)
            if not s:
                continue
            tt = f"{s['t']:.1f}" if s["t"] is not None else "-"
            L.append(f"     {str(lag) + 's':>7}{f(s['mid']) + ' (t ' + tt + ')':>16}{s['win'] * 100:>5.0f}%"
                     f"{pct(s['pt'][200]):>8}{pct(s['pt'][841]):>8}{s['bust'] * 100:>11.1f}%")
    L.append("")

    # 3. concentration
    trig = {}
    for x in als:
        trig[x.trigger] = trig.get(x.trigger, 0) + 1
    top = sorted(trig, key=lambda w: -trig[w])
    main = stats(als, a.lag, a.hold)
    L.append(f"3. IS IT ONE OR TWO WALLETS? Filled {a.lag}s late, held {a.hold}s")
    L.append(f"   {'':<26}{'alerts':>8}{'bps':>8}{'win':>6}{'200x':>8}{'841x':>8}")
    rows = [("everyone on the list", als)]
    for k in (1, 2, 3):
        drop = top[:k]
        rows.append((f"without the top {k}", walk(a.cutoff, a.t_min, exclude=drop)[0]))
    for name, sub in rows:
        s = stats(sub, a.lag, a.hold)
        if s:
            L.append(f"   {name:<26}{len(sub):>8}{f(s['mid']):>8}{s['win'] * 100:>5.0f}%{pct(s['pt'][200]):>8}{pct(s['pt'][841]):>8}")
    if main:
        L.append(f"   who starts the alerts (top 8): " + ", ".join(
            f"{short(A.wallets[w])} {trig[w]} ({f(float(np.nanmean(main['raw']['mid'][[i for i, x in enumerate(als) if x.trigger == w]])))} bps)"
            for w in top[:8]))
    L.append("")

    # 4. cutoff and threshold
    L.append(f"4. WHO COUNTS AS A BIG MOVER? Filled {a.lag}s late, held {a.hold}s")
    L.append(f"   {'cutoff':>7}{'t min':>7}{'alerts/h':>10}{'bps':>8}{'win':>6}{'200x':>8}{'841x':>8}")
    for cutoff in (4.0, 6.0, 8.0):
        for t_min in (2.0, 2.5, 3.0):
            sub = walk(cutoff, t_min)[0]
            s = stats(sub, a.lag, a.hold)
            if s:
                L.append(f"   {cutoff:>6g}+{t_min:>7g}{len(sub) / test_h:>10.1f}{f(s['mid']):>8}{s['win'] * 100:>5.0f}%"
                         f"{pct(s['pt'][200]):>8}{pct(s['pt'][841]):>8}")
    L.append("")

    # 5. bankroll
    if main:
        ok = main["ok"]
        L.append(f"5. YOUR BALANCE. One trade at a time (alerts during an open trade are skipped), filled {a.lag}s late, "
                 f"held {a.hold}s, margin = {a.size:.0%} of the starting balance on every trade")
        for cap, label in ((a.trades_per_day, f"first {a.trades_per_day} alerts a day"), (None, "every alert you could take")):
            taken = one_at_a_time(als, ok, a.lag, a.hold, cap)
            L.append(f"   {label}: {len(taken):,} trades over {len(days)} days")
            for lev, start in SETUPS:
                r = main["raw"]["pt"][lev][taken]
                r = r[np.isfinite(r)]
                for compound in (False,):         # compounding runs away to silly numbers in a bootstrap: fixed margin only
                    how = f"${a.size * start:g} margin a trade"
                    daily = []
                    for d0, d1 in days:
                        k = [j for j, i in enumerate(taken) if d0 <= als[i].t_ms < d1]
                        rr = main["raw"]["pt"][lev][taken[k]] if k else np.array([])
                        rr = rr[np.isfinite(rr)]
                        daily.append(bankroll(rr, start, a.size, compound))
                    whole = bankroll(r, start, a.size, compound)
                    L.append(f"     {lev}x, ${start:g}, {how}")
                    L.append("       each day from a fresh start: " + ", ".join(
                        f"${d['end']:.0f}" + (" bust" if d["bust"] else "") for d in daily)
                        + f"   · all {len(days)} days in a row: ${whole['end']:.0f} (lowest ${whole['low']:.0f})"
                        + (" bust" if whole["bust"] else ""))
                    tpd = cap or max(1, len(taken) // len(days))
                    for n_days, name in ((1, "a day"), (7, "a week")):
                        b = bootstrap(r, start, a.size, compound, tpd * n_days)
                        if b:
                            L.append(f"       {name} ({tpd * n_days} trades), 5,000 replays: middle ${b['median']:.0f}, "
                                     f"bad 1 in 10 ${b['p10']:.0f}, good 1 in 10 ${b['p90']:.0f}, up {b['up']:.0%}, "
                                     f"bust {b['bust']:.0%}")
        L.append("")
    L.append("The test is section 1 and 3: every day positive, and still positive without the top wallets. Then pick "
             "the lateness in section 2 that matches your real fills.")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

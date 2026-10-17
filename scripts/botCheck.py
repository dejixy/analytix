"""
Why so few alerts? Replays the recordings through the live bot's own code (api/bot.py), with today's big-mover list
and your settings, and puts that next to what the live bot actually caught.

    python -m scripts.botCheck                     # last 7 days
    python -m scripts.botCheck --days 3

On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.botCheck > /tmp/botcheck.txt 2>&1 &'

Reading it: section 1 is the answer. If the replay and the live bot match, the live bot works and the low rate is
the list and the market. If the replay has many more, the live bot is losing trades. Section 2 shows alerts per day
and per hour of the day, section 3 each listed wallet's decisions per day, section 4 the silent TWAP test replayed.
The replay uses today's list on the days it was picked from, so its moves there look better than they will live.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from api.bot import DEFAULTS, LAGS, LIST_FILE, BotService
from api.twaptrack import END_AFTER_MS, END_CAP_S, HOLDS_S
from api.twaptrack import LAG_S as TWAP_LAG
from config import BARS_DB, BOT_FILE, RECORD_DIR
from engine.bigmovers import pt_of, settle
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

H = 3_600_000
D = 86_400_000


def utc(ms, fmt="%a %d %b %H:%M"):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(fmt)


def f(x, d=1):
    return "-" if x is None or not np.isfinite(x) else f"{x:+.{d}f}"


def pct(x):
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:+.0f}%"


def short(w):
    return f"{w[:6]}…{w[-4:]}"


def rows_of(g):
    """A study grid → the live bot's per-second rows {second: [low bid, high ask, last mid]} (real updates only)."""
    ok = np.flatnonzero(g.ok)
    return {int(g.t[i]): [float(g.bid[i]), float(g.ask[i]), float(g.mid[i])] for i in ok}


def live_alerts(db: Path, since_ms: int) -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            return [json.loads(d) for (d,) in con.execute("SELECT data FROM bot_alerts WHERE t_ms >= ? ORDER BY t_ms",
                                                          (since_ms,))]
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"  (couldn't read the live alerts from {db}: {exc})", file=sys.stderr)
        return []


def matched(a: list[dict], b: list[dict], tol_ms: int = 30_000) -> int:
    """How many alerts in a have one in b on the same coin and side within tol_ms."""
    by = defaultdict(list)
    for x in b:
        by[(x["coin"], x["side"])].append(x["t_ms"])
    n = 0
    for x in a:
        ts = by.get((x["coin"], x["side"]), [])
        n += any(abs(t - x["t_ms"]) <= tol_ms for t in ts)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--settings", default=str(BOT_FILE), help="the live bot's settings (read only)")
    ap.add_argument("--list", default=str(LIST_FILE), help="the live big-mover list (read only)")
    ap.add_argument("--db", default=str(BARS_DB), help="where the live bot keeps its alerts (read only)")
    a = ap.parse_args()

    try:
        saved = json.loads(Path(a.settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    cfg = {**DEFAULTS, **{k: v for k, v in (saved.get("settings") or {}).items() if k in DEFAULTS}}
    info = json.loads(Path(a.list).read_text(encoding="utf-8"))
    since_live = saved.get("since_ms")
    old = Path(a.settings).with_name(Path(a.settings).stem + ".old.json")
    if not since_live and old.exists():
        since_live = int(old.stat().st_mtime * 1000)          # written when the new bot first started
    coins = set(cfg["coins"])
    lev, fill, hold = cfg["leverage"], int(cfg["fill_s"]), int(cfg["hold_s"])

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * D]
    if not files:
        print("No recordings found.")
        return

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        s = BotService(SimpleNamespace(mode="replay", alerts=None), Path(tmp) / "bot.json", None,
                       list_file=Path(tmp) / "list.json")
        s.cfg.update(cfg)
        s.cfg.update(on=True, auto_pause=False, telegram=False)
        s.set_list(info)
        per_wallet: dict[str, Counter] = defaultdict(Counter)
        last_dec: dict[str, int] = {}
        decide = s._decision

        def counted(coin, wallet, side, ms, usd):
            per_wallet[wallet][ms // D] += 1
            last_dec[wallet] = max(last_dec.get(wallet, 0), ms)
            decide(coin, wallet, side, ms, usd)

        s._decision = counted
        last_tick = [0]
        fills = [0]

        def on_line(line: str) -> None:
            if '"channel":"trades"' not in line:
                return
            try:
                msg = json.loads(line)["msg"]
            except (ValueError, KeyError, TypeError):
                return
            fills[0] += len(msg.get("data") or [])
            s.on_message(msg)
            now = max(s.now_ms.values(), default=0)
            if now - last_tick[0] >= 5000:
                last_tick[0] = now
                s.tick()

        g = grids(files, coins, on_other=on_line)
        s.tick()
    print(f"  replayed {fills[0]:,} trades, {time.time() - t0:.0f}s", file=sys.stderr)

    rows = {c: rows_of(gr) for c, gr in g.items()}
    al = list(s.alerts)
    for x in al:
        x["res"] = {lag: settle(rows.get(x["coin"], {}), x["t_ms"] // 1000, lag, hold, x["side"])
                    for lag in sorted({*LAGS, fill})}
    start = min(int(gr.t[0]) for gr in g.values() if len(gr)) * 1000
    end = max(int(gr.t[-1]) for gr in g.values() if len(gr)) * 1000
    hours = (end - start) / H

    L = [f"BOT CHECK  {hours:.0f} h of recordings ({utc(start)} to {utc(end)} UTC) · today's list: "
         f"{len(s.wallets)} big movers (built {utc(info.get('built_ms', 0))}) · {', '.join(sorted(coins))}", ""]

    # 1. replay vs live
    L.append("1. REPLAY vs LIVE")
    if not since_live:
        L.append("   Don't know when the live bot started, so no comparison (no since_ms in bot.json, no bot.old.json).")
    else:
        lo = max(since_live, start)
        live = [x for x in live_alerts(Path(a.db), lo) if x["t_ms"] <= end]
        rep = [x for x in al if lo <= x["t_ms"] <= end]
        span = max((end - lo) / H, 1e-9)
        both = matched(rep, live)
        L.append(f"   Since the live bot started ({utc(since_live)} UTC), {span:.0f} h overlap with the recordings:")
        L.append(f"   replay {len(rep)} alerts ({len(rep) / span:.1f} an hour) · live {len(live)} "
                 f"({len(live) / span:.1f} an hour) · in both {both} · replay only {len(rep) - both} · "
                 f"live only {len(live) - matched(live, rep)}")
        if rep or live:
            hrs = sorted({x["t_ms"] // H for x in rep + live})
            rc, lc = Counter(x["t_ms"] // H for x in rep), Counter(x["t_ms"] // H for x in live)
            cells = [f"{utc(h * H, '%a %H')}h {rc[h]}/{lc[h]}" for h in hrs]
            L.append("   hours with any alert (replay/live):")
            for i in range(0, len(cells), 6):
                L.append("     " + "   ".join(f"{c:<14}" for c in cells[i:i + 6]))
        if not rep and not live:
            L.append("   Neither has an alert in that window.")
        elif len(live) >= 0.8 * len(rep):
            L.append("   → The live bot caught what the recordings say it should. It isn't losing trades: the low rate "
                     "is the list and the market.")
        else:
            L.append(f"   → The live bot caught {len(live)} of the {len(rep)} the recordings have. It's losing trades "
                     "somewhere between the feed and the bot.")
        L.append("   (The replay uses today's list; the live bot used each hour's list, so a few differences are normal.)")
    L.append("")

    # 2. per day and per hour of the day
    L.append(f"2. ALERTS PER DAY with today's list (UTC). Move = in {fill}s late, out after {hold}s, at the mid "
             f"(in-sample: the list was picked on these days)")
    L.append(f"   {'day':<12}{'hours':>6}{'alerts':>8}{'an hour':>9}{'move':>8}{str(int(lev)) + 'x':>8}{'decisions':>11}")
    days = sorted({x["t_ms"] // D for x in al} | {d for c in per_wallet.values() for d in c})
    for day in days:
        xs = [x for x in al if x["t_ms"] // D == day]
        b = [x["res"][fill]["bps"] for x in xs if x["res"].get(fill)]
        p = [pt_of(x["res"][fill], lev, x["coin"]) for x in xs if x["res"].get(fill)]
        h = min(end, (day + 1) * D) - max(start, day * D)
        h = max(h / H, 1e-9)
        dec = sum(c[day] for c in per_wallet.values())
        L.append(f"   {utc(day * D, '%a %d %b'):<12}{h:>6.1f}{len(xs):>8}{len(xs) / h:>9.1f}"
                 f"{f(np.mean(b)) if b else '-':>8}{pct(np.mean(p)) if p else '-':>8}{dec:>11}")
    if al:
        hod = Counter(int(utc(x["t_ms"], "%H")) for x in al)
        L.append("   by hour of the day (UTC), all days added up:")
        L.append("     " + " ".join(f"{h:>3}" for h in range(24)))
        L.append("     " + " ".join(f"{hod.get(h, 0) or '.':>3}" for h in range(24)))
    L.append("")

    # 3. the wallets
    L.append(f"3. THE LIST: each big mover's decisions (${cfg['min_usd']:,.0f}+ on {', '.join(sorted(coins))}) per day")
    L.append(f"   {'wallet':<13}{'avg':>6}{'t':>5}{'n':>5}" + "".join(f"{utc(d * D, '%a'):>5}" for d in days) + f"{'last seen':>18}")
    order = sorted(s.wallets, key=lambda w: -sum(per_wallet[w].values()))
    for w in order:
        info_w = s.wallets[w]
        seen = last_dec.get(w) or info_w.get("last_ms")
        L.append(f"   {short(w):<13}{info_w.get('mean_bps', 0):>6.1f}{info_w.get('t', 0):>5.1f}{info_w.get('n', 0):>5}"
                 + "".join(f"{per_wallet[w][d] or '.':>5}" for d in days)
                 + f"{utc(seen, '%a %d %b %H:%M') if seen else '-':>18}")
    quiet = [w for w in s.wallets if not last_dec.get(w) or last_dec[w] < end - D]
    L.append(f"   {len(quiet)} of {len(s.wallets)} haven't traded {', '.join(sorted(coins))} in the last 24 h. "
             f"(n and avg are over all four coins; alerts only come from {', '.join(sorted(coins))}.)")
    L.append("")

    # 4. the silent TWAP test, replayed
    tw = list(s.twaps.records)
    L.append(f"4. THE SILENT TWAP TEST, replayed: big Hyperliquid TWAPs ($20k+ slices every 30s), in {TWAP_LAG}s after "
             f"the 4th slice, their way")
    if not tw:
        L.append("   None spotted.")
    else:
        mid_t = float(np.median([r["t_ms"] for r in tw]))
        L.append(f"   {len(tw)} spotted ({len(tw) / max(hours, 1e-9) * 24:.1f} a day)")
        L.append(f"   {'hold':<12}{'n':>5}{'move':>8}{'win':>6}{str(int(lev)) + 'x':>8}{'busts':>7}{'1st half':>10}{'2nd half':>10}")
        for k in [*HOLDS_S, None]:
            res = []
            for r in tw:
                s0 = r["t_ms"] // 1000
                if k is None:
                    end_s = (r["end_ms"] // 1000 + END_AFTER_MS // 1000) if r.get("end_ms") else s0 + TWAP_LAG + END_CAP_S
                    h = min(max(end_s - (s0 + TWAP_LAG), 1), END_CAP_S)
                else:
                    h = k
                x = settle(rows.get(r["coin"], {}), s0, TWAP_LAG, h, r["side"])
                if x:
                    res.append((r["t_ms"], x, r["coin"]))
            if not res:
                continue
            b = np.array([x["bps"] for _, x, _ in res])
            t = np.array([t for t, _, _ in res])
            p = np.array([pt_of(x, lev, c) for _, x, c in res])
            first, second = b[t <= mid_t], b[t > mid_t]
            label = "to the end" if k is None else f"{k // 60}m"
            L.append(f"   {label:<12}{len(b):>5}{f(b.mean()):>8}{(b > 0).mean() * 100:>5.0f}%{pct(p.mean()):>8}"
                     f"{(p <= -1).mean() * 100:>6.0f}%{f(first.mean()) if len(first) else '-':>10}"
                     f"{f(second.mean()) if len(second) else '-':>10}")
    print(f"({time.time() - t0:.0f}s)\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()

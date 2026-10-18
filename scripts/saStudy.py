"""
SA (sentiment) basket, a study: does Analytix's own read predict the next move?

    the read     every minute, the 1m, 10m and 60m cards as the dashboard shows them (the recordings replayed through
                 the same pipeline). Each card row leans up, down or neither (+1, -1, 0). A card's score is the average
                 lean of its rows (-1 to +1); the SA score is the average of the three cards
    part 1       each row on its own: when it leans one way, what did price do over the next 5, 15 and 60 minutes,
                 that way? Positive = it reads the next move; negative = the move reverses (trade against it)
    part 2       the SA bot as asked: score past a threshold → in that way ("with") or the other way ("against"),
                 out at +20% / -10% of the margin (or +25% / -15%), at most 4 h; one trade at a time per coin, a
                 5 min wait after each. The bot trades itself, so it's in 2s after the read. Papertrade (BTC, ETH;
                 its cut on wins; stops fill at the real bid/ask) and Hyperliquid (all four at each coin's max
                 leverage, market orders, 9 bps a round trip). A random side at the same moments is the control
    t            from day-by-day results (minutes overlap, so per-trade t would read too high)

    python -m scripts.saStudy                  # last 14 days of recordings
On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.saStudy > /tmp/sa.txt 2>&1 &'
"""
import argparse
import math
import sys
import time
from array import array

import numpy as np

from api.informed import HL_MAX_LEV
from config import RECORD_DIR
from engine.analyzer import Analyzer
from engine.holdclock import Grid, papertrade_kept
from ingestion.parsers import coin_of
from ingestion.replay import _first_recv, read_session, recording_files
from models.signalModel import Direction
from pipeline import Pipeline

DAY_MS = 86_400_000
CARDS = {"1m": 60, "10m": 600, "60m": 3600}
EVERY_MS = 60_000
LAG_S = 2
STALE_S = 5
HORIZONS = (300, 900, 3600)
COOLDOWN_S = 300
MAX_HOLD_S = 4 * 3600
PT_LEVS = (200, 100, 50, 20)
EXITS = ((0.20, 0.10), (0.25, 0.15))
THRESHOLDS = (0.2, 0.35, 0.5)
HL_RT_BPS = 9.0
LEAN = {Direction.UP: 1, Direction.DOWN: -1}


# ── one pass: the cards every minute, and each coin's prices by the second ───
class Seconds:
    """Per second: lowest best bid, highest best ask, last mid (compact, for days of data)."""

    def __init__(self):
        self.s, self.lo, self.hi, self.mid = array("q"), array("d"), array("d"), array("d")
        self.cur = None

    def add(self, d: dict) -> None:
        try:
            bids, asks = d["levels"]
            bid, ask, s = float(bids[0]["px"]), float(asks[0]["px"]), int(d["time"]) // 1000
        except (KeyError, IndexError, TypeError, ValueError):
            return
        if bid <= 0 or ask <= 0:
            return
        c = self.cur
        if c is not None and c[0] == s:
            c[1], c[2], c[3] = min(c[1], bid), max(c[2], ask), (bid + ask) / 2
            return
        self._push()
        self.cur = [s, bid, ask, (bid + ask) / 2]

    def _push(self) -> None:
        if self.cur is not None:
            s, lo, hi, mid = self.cur
            self.s.append(s), self.lo.append(lo), self.hi.append(hi), self.mid.append(mid)
            self.cur = None

    def grid(self) -> Grid | None:
        self._push()
        if not len(self.s):
            return None
        s = np.frombuffer(self.s, dtype=np.int64)
        order = np.argsort(s, kind="stable")
        s = s[order]
        lo, hi, mid = (np.frombuffer(a, dtype=np.float64)[order] for a in (self.lo, self.hi, self.mid))
        first = np.r_[0, np.nonzero(np.diff(s))[0] + 1]
        last = np.r_[first[1:] - 1, len(s) - 1]
        s, lo, hi, mid = s[first], np.minimum.reduceat(lo, first), np.maximum.reduceat(hi, first), mid[last]
        t0, n = int(s[0]), int(s[-1] - s[0] + 1)
        bid, ask, m = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        bid[s - t0], ask[s - t0], m[s - t0] = lo, hi, mid
        have = ~np.isnan(m)
        idx = np.where(have, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        ok = (np.arange(n) - idx) <= STALE_S
        return Grid(np.arange(t0, t0 + n), bid[idx], ask[idx], m[idx], ok)


def snapshot(p: Pipeline) -> tuple[int, dict] | None:
    """The three cards' row leans now: {card: {row: +1/-1/0}}, cards with a full window only."""
    out = {}
    for w in CARDS:
        ex = p.analyzer.latest.get(w)
        if ex is None or not ex.summary or ex.coverage < 0.95:
            continue
        out[w] = {m.key: LEAN.get(m.lean, 0) for m in ex.summary if not m.partial}
    return (p.state.now_ms // 1000, out) if out else None


def replay(files, coins: set[str]):
    pipes = {c: Pipeline(c) for c in coins}
    for p in pipes.values():
        p.analyzer = Analyzer(p.state, windows=dict(CARDS), interval_ms=EVERY_MS)
    secs = {c: Seconds() for c in coins}
    reads = {c: [] for c in coins}
    t0 = time.time()
    for k, path in enumerate(files):
        for _, msg in read_session(path):
            coin = coin_of(msg)
            p = pipes.get(coin or "")
            if p is None:
                continue
            if msg.get("channel") == "l2Book":
                secs[coin].add(msg.get("data") or {})
            runs = p.analyzer.runs
            try:
                p.on_message(msg)
            except Exception:                              # one bad message shouldn't end a long replay
                continue
            if p.analyzer.runs != runs:
                snap = snapshot(p)
                if snap:
                    reads[coin].append(snap)
        if (k + 1) % 12 == 0:
            print(f"  replayed {k + 1}/{len(files)} files, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    return {c: g for c, s in secs.items() if (g := s.grid()) is not None}, reads


# ── scores and forward moves ─────────────────────────────────────────────────
def card_scores(cards: dict) -> dict:
    """{card: average lean}, plus "sa" (the average of the three) when all three are there."""
    out = {w: sum(rows.values()) / len(rows) for w, rows in cards.items() if rows}
    if all(w in out for w in CARDS):
        out["sa"] = sum(out[w] for w in CARDS) / len(CARDS)
    return out


def forward(g: Grid, t_s: int, h: int) -> float | None:
    """The mid's move from LAG_S after t_s to h seconds later, as a fraction; None across a feed gap."""
    e = t_s - int(g.t[0]) + LAG_S
    x = e + h
    if e < 0 or x >= len(g) or not g.ok[e:x + 1].all():
        return None
    return float(g.mid[x] / g.mid[e] - 1)


def day_t(days: dict[int, list[float]]) -> tuple[float, float | None, int, int]:
    """(mean of all, t from day means, days up, days)."""
    allv = [x for v in days.values() for x in v]
    means = [np.mean(v) for v in days.values() if v]
    if not allv:
        return float("nan"), None, 0, 0
    t = None
    if len(means) >= 3 and np.std(means, ddof=1) > 0:
        t = float(np.mean(means) / (np.std(means, ddof=1) / math.sqrt(len(means))))
    return float(np.mean(allv)), t, sum(m > 0 for m in means), len(means)


# ── the trades ───────────────────────────────────────────────────────────────
def ride(g: Grid, t_s: int, side: int, tp: float, sl: float) -> tuple[float, int, bool] | None:
    """In at the mid LAG_S after t_s; out at +tp (a limit at that price), at -sl (a stop: the real bid/ask that
    second, which can be worse), or after MAX_HOLD_S at the mid. → (move your way, seconds held, hit tp), None if
    the feed had a gap before the exit."""
    e = t_s - int(g.t[0]) + LAG_S
    if e < 0 or e >= len(g) - 1 or not g.ok[e]:
        return None
    entry = g.mid[e]
    end = min(e + MAX_HOLD_S, len(g) - 1)
    a = e + 1
    while a <= end:
        b = min(a + 900, end + 1)
        mid, bid, ask = g.mid[a:b], g.bid[a:b], g.ask[a:b]
        if side > 0:
            hit_tp, hit_sl = mid >= entry * (1 + tp), bid <= entry * (1 - sl)
        else:
            hit_tp, hit_sl = mid <= entry * (1 - tp), ask >= entry * (1 + sl)
        i_tp = int(hit_tp.argmax()) if hit_tp.any() else None
        i_sl = int(hit_sl.argmax()) if hit_sl.any() else None
        if i_sl is not None and (i_tp is None or i_sl <= i_tp):
            if not g.ok[e:a + i_sl + 1].all():
                return None
            px = bid[i_sl] if side > 0 else ask[i_sl]
            return side * (px / entry - 1), a + i_sl - e, False
        if i_tp is not None:
            if not g.ok[e:a + i_tp + 1].all():
                return None
            return tp, a + i_tp - e, True
        a = b
    if not g.ok[e:end + 1].all():
        return None
    return side * (g.mid[end] / entry - 1), end - e, False


def pt_result(move: float, lev: float, coin: str) -> float:
    return papertrade_kept(move, coin) * lev if move > 0 else max(move * lev, -1.0)


def hl_result(move: float, coin: str) -> float:
    lev = HL_MAX_LEV.get(coin, 10)
    return max(lev * (move - HL_RT_BPS / 1e4), -1.0)


def run_bot(grids: dict, reads: dict, coins, key: str, thr: float, mode: int, tp_m: float, sl_m: float,
            venue: str, lev: float, rng=None) -> list[tuple[int, float, bool]]:
    """The SA loop on each coin. key: which score ("sa", "1m", "10m", "60m", or "agree"); mode +1 = with the read,
    -1 = against; rng → a random side instead (the control). → [(day, result on margin, hit tp)]."""
    out = []
    for coin in coins:
        g = grids.get(coin)
        if g is None:
            continue
        L = lev if venue == "pt" else HL_MAX_LEV.get(coin, 10)
        tp, sl = tp_m / L, sl_m / L
        free = -1
        for t_s, sc in reads[coin]:
            if t_s < free:
                continue
            if key == "agree":
                if not all(w in sc for w in CARDS):
                    continue
                signs = {np.sign(sc[w]) for w in CARDS if abs(sc[w]) >= thr}
                if len(signs) != 1 or sum(abs(sc[w]) >= thr for w in CARDS) < len(CARDS):
                    continue
                side = int(signs.pop())
            else:
                s = sc.get(key)
                if s is None or abs(s) < thr:
                    continue
                side = 1 if s > 0 else -1
            side *= mode
            if rng is not None:
                side = int(rng.choice((-1, 1)))
            got = ride(g, t_s, side, tp, sl)
            if got is None:
                continue
            move, held, won = got
            res = pt_result(move, L, coin) if venue == "pt" else hl_result(move, coin)
            out.append((t_s // 86_400, res, won))
            free = t_s + LAG_S + held + COOLDOWN_S
    return out


def summary(trades: list[tuple[int, float, bool]], n_days: int) -> dict | None:
    if len(trades) < 10:
        return None
    days = {}
    for d, r, _ in trades:
        days.setdefault(d, []).append(r)
    mean, t, up, nd = day_t(days)
    ds = sorted(days)
    half = ds[len(ds) // 2] if ds else 0
    first = [r for d, r, _ in trades if d < half]
    second = [r for d, r, _ in trades if d >= half]
    return {"n": len(trades), "per_day": len(trades) / max(n_days, 1), "win": np.mean([w for *_, w in trades]),
            "avg": mean, "t": t, "up": up, "days": nd, "first": np.mean(first) if first else float("nan"),
            "second": np.mean(second) if second else float("nan"),
            "per_week": sum(r for _, r, _ in trades) / max(n_days, 1) * 7 * 0.2}


# ── the report ───────────────────────────────────────────────────────────────
def pct(x):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"


def tt(x):
    return "-" if x is None else f"{x:+.1f}"


def sa_lean(cards: dict, kind: str) -> int:
    sc = card_scores(cards)
    if kind == "≥0.35":
        x = sc.get("sa")
        return 0 if x is None or abs(x) < 0.35 else (1 if x > 0 else -1)
    signs = {np.sign(sc[w]) for w in CARDS if w in sc}
    ok = all(abs(sc.get(w, 0)) >= 0.2 for w in CARDS)
    return int(signs.pop()) if ok and len(signs) == 1 else 0


def part1(grids, reads, coins) -> list[str]:
    L = ["1. DOES EACH CARD ROW READ THE NEXT MOVE? Price that way after it leans (bps, t from days). Positive = with it,",
         "   negative = the move reverses. Every coin pooled.",
         f"   {'card · row':<22}{'leans':>8}{'5 min':>16}{'15 min':>16}{'60 min':>16}"]
    fwd = {c: [{h: forward(grids[c], t_s, h) for h in HORIZONS} if c in grids else {} for t_s, _ in reads[c]]
           for c in coins}
    keys = sorted({(w, r) for c in coins for _, cards in reads[c] for w, rows in cards.items() for r in rows},
                  key=lambda x: (list(CARDS).index(x[0]), x[1]))
    keys += [("SA score", "≥0.35"), ("SA score", "all agree")]
    for w, r in keys:
        cells, n = [], 0
        for h in HORIZONS:
            days = {}
            for c in coins:
                for (t_s, cards), mv in zip(reads[c], fwd[c]):
                    lean = sa_lean(cards, r) if w == "SA score" else cards.get(w, {}).get(r, 0)
                    if not lean or mv.get(h) is None:
                        continue
                    days.setdefault(t_s // 86_400, []).append(lean * mv[h] * 1e4)
            mean, t, _, _ = day_t(days)
            n = max(n, sum(len(v) for v in days.values()))
            cells.append(f"{mean:+.1f} ({tt(t)})" if days else "-")
        L.append(f"   {w + ' · ' + r:<22}{n:>8}" + "".join(f"{c:>16}" for c in cells))
    L.append("")
    return L


def part2(grids, reads, coins, n_days) -> list[str]:
    scored = {c: [(t, card_scores(cards)) for t, cards in reads[c]] for c in coins}
    L = ["2. THE SA BOT: in on the score, out at +TP / -SL of the margin, one at a time, 5 min wait. 'avg' = per trade, % of",
         "   margin after costs; 'a week' = on a balance putting 20% of it up as margin each trade (no compounding).",
         f"   {'score':<7}{'thr':>5}{'side':>9}{'venue':>18}{'exit':>11}{'n':>6}{'/day':>6}{'win':>6}{'avg (t)':>16}"
         f"{'days +':>8}{'halves':>18}{'a week':>9}"]
    pt_coins = [c for c in coins if c in ("BTC", "ETH")]
    rows = []
    for key in ("sa", "1m", "10m", "60m", "agree"):
        for thr in THRESHOLDS:
            for mode in (1, -1):
                for tp_m, sl_m in EXITS:
                    for venue, levs, cs in (("pt", PT_LEVS, pt_coins), ("hl", (0,), list(coins))):
                        for lev in levs:
                            s = summary(run_bot(grids, scored, cs, key, thr, mode, tp_m, sl_m, venue, lev), n_days)
                            if s:
                                rows.append(((key, thr, mode, tp_m, sl_m, venue, lev), s))

    def line(cfg, s):
        key, thr, mode, tp_m, sl_m, venue, lev = cfg
        vn = f"Papertrade {lev}x" if venue == "pt" else "Hyperliquid max"
        return (f"   {key:<7}{thr:>5}{'with' if mode > 0 else 'against':>9}{vn:>18}{f'+{tp_m:.0%}/-{sl_m:.0%}':>11}"
                f"{s['n']:>6}{s['per_day']:>6.0f}{s['win']:>6.0%}{pct(s['avg']) + ' (' + tt(s['t']) + ')':>16}"
                f"{s['up']:>5}/{s['days']:<2}{pct(s['first']) + ' / ' + pct(s['second']):>18}{pct(s['per_week']):>9}")

    L.append("   As asked (SA score 0.35, with the read):")
    for cfg, s in rows:
        if cfg[0] == "sa" and cfg[1] == 0.35 and cfg[2] == 1:
            L.append(line(cfg, s))
    L.append("   The same, against the read:")
    for cfg, s in rows:
        if cfg[0] == "sa" and cfg[1] == 0.35 and cfg[2] == -1:
            L.append(line(cfg, s))
    L.append("   Best 10 of every setup tried, by t (with this many tries, one good row is expected by luck):")
    for cfg, s in sorted(rows, key=lambda x: -(x[1]["t"] or -99))[:10]:
        L.append(line(cfg, s))
    rng = np.random.default_rng(11)
    L.append("   Control: a random side at the SA 0.35 moments:")
    for venue, lev, cs in (("pt", 200, pt_coins), ("pt", 50, pt_coins), ("hl", 0, list(coins))):
        s = summary(run_bot(grids, scored, cs, "sa", 0.35, 1, 0.20, 0.10, venue, lev, rng=rng), n_days)
        if s:
            L.append(line(("random", 0.35, 1, 0.20, 0.10, venue, lev), s))
    L.append(f"   ({len(rows)} setups tried.)")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14, help="0 = every recording")
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    a = ap.parse_args()
    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * DAY_MS]
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    t0 = time.time()
    grids, reads = replay(files, set(coins))
    n_reads = sum(len(v) for v in reads.values())
    print(f"  {n_reads:,} card reads, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    if n_reads < 100:
        print("Too few card reads.")
        return
    span = [r[0] for v in reads.values() for r in v]
    n_days = max(1, round((max(span) - min(span)) / 86_400))
    L = [f"SA STUDY · {n_days} days of recordings · {', '.join(coins)} · the 1m, 10m and 60m cards read every minute "
         f"({n_reads:,} reads)", ""]
    L += part1(grids, reads, coins)
    L += part2(grids, reads, coins, n_days)
    L += ["Reading it: the bot is worth building only if a setup beats the random control by a clear margin, is positive",
          "in both halves and on most days, and part 1 agrees on the direction (rows that read the move → 'with'; rows",
          "that reverse → 'against'). With ~600 setups tried, a lone t of 2 to 3 is what luck alone produces."]
    print("\n".join(L))


if __name__ == "__main__":
    main()

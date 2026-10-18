"""
Degen basket, a simulation: what are the odds of doubling a small pot in a week before losing half of it, if every
trade puts a big share of the pot on the bot's best signals? Study data only (the recordings), no money.

    the game     a week of trades, one at a time; each puts `frac` of the pot up as margin (the pot as it stands,
                 so wins grow the next trade); stop at 2x (a win) or at half (a loss; a bust can overshoot it). If
                 neither happens, the week ends where it is
    the weeks    WEEKS made-up weeks, each 7 random days from the recordings (each day's trades in order), so busy
                 and quiet days mix the way they did
    signals      strong TWAPs (2%+ of volume and $250k+ still to come, in 6s after slice 2, out after 15 min), the
                 $1M+ ones, and fades (score -5 or worse, scored walk-forward, out after 15 min)
    venues       Papertrade (BTC, ETH; 50, 100 or 200x; its cut on wins; bust when the best bid/ask goes 1/leverage
                 against) and Hyperliquid (each coin's max leverage; limit orders 3 bps a round trip, market orders 9;
                 liquidated at half that distance, its maintenance margin, counted as losing the whole margin)
    honesty      the best setups are rerun with half the edge and with none. No edge is a coin flip at best, and
                 worse at big sizes (a +10% then -10% leaves 99%); half the edge is the realistic one (live edges
                 usually come in at about half the study)

    python -m scripts.degenSim                 # last 14 days of recordings
On the server, from /opt/analytix:
    sudo -u analytix sh -c 'nohup nice -n 19 .venv/bin/python -m scripts.degenSim > /tmp/degen.txt 2>&1 &'
"""
import argparse
import json
import sys
import time
from collections import defaultdict

import numpy as np

from api.informed import HL_MAX_LEV
from config import LIQUIDATOR_ADDRESSES, RECORD_DIR
from engine.bigmovers import pt_of
from engine.informed import HiddenOrders, follow, scores, shrink_k
from engine.twap import Slices, ride, runs
from engine.walletstudy import taker_of
from ingestion.replay import _first_recv, recording_files
from scripts.survivalStudy import grids

DAY_MS = 86_400_000
LAG_S = 6
HOLD_S = 900
FRACS = (0.1, 0.2, 0.35, 0.5, 0.75, 1.0)
PT_LEVS = (50, 100, 200)
MAKER_RT, TAKER_RT = 3.0, 9.0
UP, DOWN = 2.0, 0.5
FADE_AT = -5.0


# ── per-trade results, as a share of the margin ──────────────────────────────
def pt_res(bps: float, adv: float, lev: float, coin: str) -> float:
    return pt_of({"bps": bps, "adv": adv}, lev, coin)


def hl_res(bps: float, adv: float, coin: str, fee_bps: float) -> float:
    """Hyperliquid at the coin's max leverage: liquidated (margin gone) once the worst point reaches the
    maintenance margin, half the initial margin away; else the move after fees."""
    lev = HL_MAX_LEV.get(coin, 10)
    if adv >= 1e4 / lev / 2:
        return -1.0
    return max(lev * (bps - fee_bps) / 1e4, -1.0)


def shifted(trades: list[dict], keep: float) -> list[dict]:
    """The same trades with only `keep` of the average edge: every move is lowered (and every worst point
    deepened) by the part of the edge taken away."""
    mean = float(np.mean([t["bps"] for t in trades])) if trades else 0.0
    cut = (1 - keep) * mean if mean > 0 else 0.0
    return [{**t, "bps": t["bps"] - cut, "adv": t["adv"] + cut} for t in trades]


# ── the weeks ────────────────────────────────────────────────────────────────
def by_day(trades: list[dict], day_list: list[int]) -> list[list[dict]]:
    """Each day's trades in time order, one at a time (a trade that starts while one is open is skipped)."""
    out = {d: [] for d in day_list}
    free = {}
    for t in sorted(trades, key=lambda x: x["t"]):
        d = t["t"] // DAY_MS
        if d not in out or t["t"] < free.get(d, -1):
            continue
        out[d].append(t)
        free[d] = t["t"] + HOLD_S * 1000
    return [out[d] for d in day_list]


def make_weeks(days: list[list[dict]], weeks: int, seed: int = 7) -> np.ndarray:
    """→ a weeks × slots array of trade indices into the flat list of every day's trades (-1 = no trade)."""
    rng = np.random.default_rng(seed)
    starts = np.cumsum([0] + [len(d) for d in days])
    picks = rng.integers(0, len(days), (weeks, 7))
    lens = np.array([len(d) for d in days])
    width = max(1, int(lens[picks].sum(axis=1).max()))
    idx = np.full((weeks, width), -1, dtype=np.int64)
    for w in range(weeks):
        k = 0
        for d in picks[w]:
            n = lens[d]
            idx[w, k:k + n] = np.arange(starts[d], starts[d] + n)
            k += n
    return idx


def play(res: np.ndarray, idx: np.ndarray, frac: float) -> dict:
    """Every week at once: the pot after each trade, stopped at UP or DOWN. res = each trade's result on margin."""
    r = np.where(idx >= 0, res[np.maximum(idx, 0)], 0.0)
    pot = np.cumprod(np.maximum(1 + frac * r, 0.0), axis=1)
    pot = np.concatenate([np.ones((len(pot), 1)), pot], axis=1)
    hit_up, hit_dn = pot >= UP, pot <= DOWN
    big = pot.shape[1] + 1
    first_up = np.where(hit_up.any(axis=1), hit_up.argmax(axis=1), big)
    first_dn = np.where(hit_dn.any(axis=1), hit_dn.argmax(axis=1), big)
    up, dn = first_up < first_dn, first_dn < first_up
    end = np.where(up, pot[np.arange(len(pot)), np.minimum(first_up, pot.shape[1] - 1)],
                   np.where(dn, pot[np.arange(len(pot)), np.minimum(first_dn, pot.shape[1] - 1)], pot[:, -1]))
    none = ~(up | dn)
    return {"up": float(up.mean()), "down": float(dn.mean()), "none": float(none.mean()),
            "down_pot": float(end[dn].mean()) if dn.any() else None,
            "none_pot": float(np.median(end[none])) if none.any() else None,
            "ev": float(end.mean() - 1), "growth": float(np.exp(np.log(np.maximum(end, 1e-3)).mean()) - 1)}


# ── the signals, from one pass over the recordings ───────────────────────────
def load(files, coins: set[str], twap_coins: set[str], min_usd: float):
    ho = HiddenOrders(min_usd=min_usd)
    sl = Slices()
    vol = defaultdict(float)

    def on_line(line: str) -> None:
        if '"channel":"trades"' not in line:
            return
        try:
            data = json.loads(line)["msg"]["data"]
        except (ValueError, KeyError, TypeError):
            return
        for tr in data or []:
            coin = tr.get("coin")
            if coin not in coins:
                continue
            try:
                usd, ms = float(tr["px"]) * float(tr["sz"]), int(tr["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if coin in twap_coins:
                vol[(coin, ms // 3_600_000)] += usd
            who = taker_of(tr)
            if who is None or who[0].lower() in LIQUIDATOR_ADDRESSES:
                continue
            w = who[0].lower()
            ho.add(w, coin, who[1], ms, usd)
            if coin in twap_coins:
                sl.add(w, coin, who[1], ms, usd)

    g = grids(files, coins, on_other=on_line)
    return g, [h for h in ho.finish() if h.coin in g], sl, vol


def twap_trades(g, sl, vol, min_slice: float) -> list[dict]:
    def per_second(coin, t_ms):
        h = t_ms // 3_600_000
        past = [x for x in (vol.get((coin, h - k), 0.0) for k in range(1, 25)) if x > 0]
        return np.mean(past) / 3600 if past else np.nan

    out = []
    for r in runs(sl, 4, 1000):
        if r.kind != "native" or np.median(r.usd) < min_slice or r.coin not in g:
            continue
        spot = r.times[1]
        left = float(sum(r.usd[2:]))
        left_s = max((r.times[-1] - spot) / 1000 + 30, 30)
        share = left / (per_second(r.coin, spot) * left_s)
        if not (share >= 0.02 and left >= 250_000):
            continue
        res = ride(g[r.coin], r, 2, LAG_S, HOLD_S, (), r.coin)
        if res:
            out.append({"t": spot + LAG_S * 1000, "coin": r.coin, "bps": res["bps"], "adv": res["adv"], "left": left})
    return out


def fade_trades(g, orders, history_h: float) -> tuple[list[dict], int]:
    """Fades of score -5 or worse, scored walk-forward day by day (as scripts/fadeStudy does). → trades, first day."""
    wl = {}
    W = np.array([wl.setdefault(h.wallet, len(wl)) for h in orders])
    S = np.array([h.side for h in orders])
    TD = np.array([h.t_detect for h in orders], dtype=np.int64)
    coin = np.array([h.coin for h in orders])
    follow15 = np.full(len(orders), np.nan)
    fb, fa = np.full(len(orders), np.nan), np.full(len(orders), np.nan)
    for c in set(coin):
        k = np.nonzero(coin == c)[0]
        follow15[k], _ = follow(g[c], TD[k], S[k], LAG_S, HOLD_S, 200, c)
        fb[k], _, fa[k] = follow(g[c], TD[k], -S[k], LAG_S, HOLD_S, 200, c, with_adv=True)
    first = int(TD.min()) + int(history_h * 3.6e6)
    d0 = -(-first // DAY_MS) * DAY_MS
    ready = TD + (LAG_S + HOLD_S + 1) * 1000
    score = np.full(len(orders), np.nan)
    for day in range(d0, int(TD.max()) + 1, DAY_MS):
        past = ready <= day
        k = shrink_k(W[past], follow15[past])
        sc = scores(W, follow15, ready, day, len(wl), k)
        today = np.nonzero((TD >= day) & (TD < day + DAY_MS))[0]
        score[today] = np.where(sc["n"][W[today]] > 0, sc["score"][W[today]], np.nan)
    pick = np.nonzero((score <= FADE_AT) & np.isfinite(fb) & np.isfinite(fa))[0]
    wallets = list(wl)
    return [{"t": int(TD[i]) + LAG_S * 1000, "coin": str(coin[i]), "bps": float(fb[i]), "adv": float(fa[i]),
             "wallet": wallets[W[i]], "score": float(score[i])} for i in pick], d0 // DAY_MS


# ── the report ───────────────────────────────────────────────────────────────
def venues(set_coins: set[str]) -> list[tuple[str, callable]]:
    out = []
    if set_coins <= {"BTC", "ETH"}:
        out += [(f"Papertrade {lev}x", lambda t, lev=lev: pt_res(t["bps"], t["adv"], lev, t["coin"])) for lev in PT_LEVS]
    out += [("Hyperliquid max lev, limit", lambda t: hl_res(t["bps"], t["adv"], t["coin"], MAKER_RT)),
            ("Hyperliquid max lev, market", lambda t: hl_res(t["bps"], t["adv"], t["coin"], TAKER_RT))]
    return out


def report(name: str, trades: list[dict], day_list: list[int], weeks: int) -> list[str]:
    L = [name]
    if len(trades) < 10:
        return L + ["   too few trades", ""]
    b = np.array([t["bps"] for t in trades])
    days = by_day(trades, day_list)
    per_week = 7 * np.mean([len(d) for d in days])
    L.append(f"   {len(trades)} trades over {len(day_list)} days; {b.mean():+.1f} bps average, {(b > 0).mean():.0%} wins; "
             f"about {per_week:.0f} a week when taken one at a time")
    idx = make_weeks(days, weeks)
    flat = [t for d in days for t in d]
    rows = []
    for vname, fn in venues({t["coin"] for t in trades}):
        res = {k: np.array([fn(t) for t in shifted(flat, k)]) for k in (1.0, 0.5, 0.0)}
        for frac in FRACS:
            rows.append((vname, frac, {k: play(res[k], idx, frac) for k in res}))
    rows.sort(key=lambda x: x[2][1.0]["up"] - x[2][1.0]["down"], reverse=True)
    L.append(f"   {'venue':<29}{'margin':>7}{'2x first':>10}{'-50% first':>11}{'neither':>9}{'if neither':>11}"
             f"{'avg week':>10}{'rolled weekly':>14}")
    for vname, frac, s in rows[:6]:
        x = s[1.0]
        L.append(f"   {vname:<29}{frac:>7.0%}{x['up']:>10.0%}{x['down']:>11.0%}{x['none']:>9.0%}"
                 f"{(x['none_pot'] or 1) - 1:>+11.0%}{x['ev']:>+10.0%}{x['growth']:>+14.0%}")
    vname, frac, s = rows[0]
    L.append(f"   The best one ({vname}, {frac:.0%} margin) with less edge:")
    for k, lab in ((1.0, "the study's edge"), (0.5, "half of it (realistic)"), (0.0, "no edge (after costs)")):
        x = s[k]
        L.append(f"     {lab:<26} 2x first {x['up']:>4.0%} · -50% first {x['down']:>4.0%} · neither {x['none']:>4.0%} · "
                 f"rolled weekly {x['growth']:+.0%}")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14, help="0 = every recording")
    ap.add_argument("--weeks", type=int, default=20_000)
    ap.add_argument("--min-slice", type=float, default=20_000)
    ap.add_argument("--min-usd", type=float, default=50_000)
    ap.add_argument("--history-h", type=float, default=72)
    a = ap.parse_args()

    files = recording_files(a.paths)
    if a.days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - a.days * DAY_MS]
    t0 = time.time()
    g, orders, sl, vol = load(files, {"BTC", "ETH", "SOL", "HYPE"}, {"BTC", "ETH"}, a.min_usd)
    print(f"  loaded, {time.time() - t0:.0f}s", file=sys.stderr)
    tw = twap_trades(g, sl, vol, a.min_slice)
    fades, fade_day0 = fade_trades(g, orders, a.history_h) if len(orders) > 50 else ([], 0)
    print(f"  {len(tw)} strong TWAPs, {len(fades)} fades, {time.time() - t0:.0f}s", file=sys.stderr)

    first_s = min(int(gr.t[0]) for gr in g.values() if len(gr))
    last_s = max(int(gr.t[-1]) for gr in g.values() if len(gr))
    all_days = [d for d in range(first_s // 86_400, last_s // 86_400 + 1)
                if min((d + 1) * 86_400, last_s) - max(d * 86_400, first_s) >= 6 * 3600]
    fade_days = [d for d in all_days if d >= fade_day0]

    L = [f"DEGEN SIMULATION · {len(all_days)} days of recordings · {a.weeks:,} made-up weeks of 7 random days · "
         f"stop at 2x or -50%", ""]
    L += report("1. STRONG TWAPs (BTC, ETH)", tw, all_days, a.weeks)
    L += report("2. STRONG TWAPs with $1M+ still to come", [t for t in tw if t["left"] >= 1_000_000], all_days, a.weeks)
    L += report("3. FADES, all four coins", fades, fade_days, a.weeks)
    L += report("4. FADES, BTC and ETH only", [t for t in fades if t["coin"] in ("BTC", "ETH")], fade_days, a.weeks)
    L += ["Reading it: '2x first' is the chance a week doubles before it halves. Worth a degen basket if the HALF-edge",
          "line still has 2x first clearly above -50% first (say 55% or more). 'Rolled weekly' = the average growth if",
          "you keep playing the whole pot week after week; negative means it shrinks over time even with some great",
          "weeks. Few days of data, so the made-up weeks reuse the same days: treat the odds as rough."]
    print("\n".join(L))


if __name__ == "__main__":
    main()

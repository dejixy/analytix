"""
Watchers: fixed rules that turn live market data into trade tickets for a person to place by hand. Each ticket is
then followed as a paper trade (prices, funding and fees), so every watcher builds a record on days it has never
seen. Pure rules and a small book here; api/watchers.py feeds them every minute and sends the alerts.

    roll    trade.xyz oil (WTI, Brent) around its monthly roll (engine/edges.py has the schedule). With both
            contracts unchanged, the oracle still has to move w x (next - front) from the roll alone (w = the front
            contract's weight now). Hyperliquid's price may already sit that far from the oracle (then it's priced
            in, and funding charges whoever holds the side the roll favours). The part not priced in:
                unpriced = roll left - (price vs oracle)                                     (bps)
            A ticket opens in its direction while the real market is open, from 24 h before the window to 2 h
            before the last step, if after funding to the end of the roll and a taker round trip it still expects
            ROLL_MIN_BPS. It closes 30 minutes after the last step reaches the oracle.
    carry   Hyperliquid's main perps with a spot market (HYPE, BTC as UBTC, ETH as UETH, SOL as USOL): buy spot,
            short the same amount of perp, collect funding with the price risk cancelled. Opens when funding has
            averaged CARRY_ENTER_APR over 3 days and is still CARRY_NOW_APR now, expecting half of the 3-day rate
            to last CARRY_HOLD_H after both legs' taker fees (until scripts/carryStudy.py measures how much lasts).
            Closes when the last day's funding averages under CARRY_EXIT_APR, or after CARRY_MAX_DAYS.

Every rule is fixed here. Changing one starts that watcher's record again (bump RULES).
"""
import json
import math
import os
from datetime import date, datetime
from pathlib import Path

from engine import edges as E

HOUR = 3_600_000
DAY = 24 * HOUR
RULES = {"roll": 1, "carry": 1}
NAMES = {"roll": "Oil roll", "carry": "Funding carry"}

ROLL_COST_BPS = 18.0           # trade.xyz taker, 9 bps in and 9 out
ROLL_MIN_BPS = 30.0
ROLL_EARLY_H = 24
ROLL_LAST_ENTRY_H = 2
ROLL_EXIT_MIN = 30
ROLL_QUOTE_AGE_H = 72          # Yahoo's last futures prices may be Friday's over a weekend

CARRY_SPOT = {"HYPE": "HYPE", "BTC": "UBTC", "ETH": "UETH", "SOL": "USOL"}
PERP_TAKER_BPS, SPOT_TAKER_BPS = 4.5, 7.0
CARRY_COST_BPS = 2 * PERP_TAKER_BPS + 2 * SPOT_TAKER_BPS
CARRY_ENTER_APR, CARRY_NOW_APR, CARRY_EXIT_APR = 20.0, 15.0, 8.0
CARRY_KEEP = 0.5
CARRY_HOLD_H = 14 * 24
CARRY_MIN_BPS = 15.0
CARRY_MAX_DAYS = 21
CARRY_REST_H = 24              # after a carry closes, a day before the same coin can open again
CARRY_MIN_SPOT_USD = 1_000_000


def apr(hourly: float | None) -> float | None:
    return None if hourly is None else hourly * 24 * 365 * 100


def bps(x: float) -> str:
    r = round(x)
    return f"{'+' if r > 0 else '−' if r < 0 else ''}{abs(r)} bps"


def day_move_pct(candles: list[tuple[int, float]], days: int = 30) -> float | None:
    """A typical day's move (the standard deviation of 24-hour moves over the last `days`), %, from hourly closes."""
    by = dict(candles)
    if not by:
        return None
    end = max(by)
    moves = []
    for k in range(days):
        a, b = by.get(end - (k + 1) * DAY), by.get(end - k * DAY)
        if a and b:
            moves.append(math.log(b / a))
    if len(moves) < 10:
        return None
    m = sum(moves) / len(moves)
    return math.sqrt(sum((x - m) ** 2 for x in moves) / (len(moves) - 1)) * 100


def mean_rate(samples: list[tuple[int, float]], since_ms: int, now_ms: int) -> float | None:
    """The average hourly funding rate since `since_ms` (Hyperliquid's funding history, one rate an hour); None
    with under half the hours there."""
    xs = [r for t, r in samples if since_ms <= t <= now_ms]
    return sum(xs) / len(xs) if xs and len(xs) >= (now_ms - since_ms) / HOUR / 2 else None


# ── the rules ────────────────────────────────────────────────────────────────
def roll_read(name: str, state: dict, front_px: float | None, next_px: float | None, quote_ms: int | None,
              ctx: dict, now_ms: int) -> dict:
    """What the roll watcher sees for one market (shown on the terminal, ticket or not)."""
    out = {"market": name, "label": E.SESSIONS[E.bare(name)].label, "phase": state["phase"], "front": state["front"],
           "next": state["next"], "front_weight": state["front_weight"], "next_step_ms": state.get("next_step_ms"),
           "end_ms": state["end_ms"], "start": state["start"],
           "first_set_ms": E._ny(date.fromisoformat(state["start"]), 0) + E.ROLL_STEP_MIN * 60_000}
    mark, oracle, funding = ctx.get("mark"), ctx.get("oracle"), ctx.get("funding")
    drift = E.roll_drift(state, front_px, next_px)
    if not drift or not mark or not oracle:
        out["why_not"] = "no futures prices" if not drift else "no Hyperliquid price"
        return out
    basis = (mark / oracle - 1) * 1e4
    unpriced = drift["left_bps"] - basis
    hours = max(0.0, (state["end_ms"] - now_ms) / HOUR)
    fund = (funding or 0.0) * hours * 1e4                         # what a long pays to the end (+ = longs pay)
    side = 1 if unpriced > 0 else -1
    expect = side * unpriced - side * fund - ROLL_COST_BPS
    out.update(left_bps=round(drift["left_bps"], 1), gap_pct=drift["gap_pct"], basis_bps=round(basis, 1),
               unpriced_bps=round(unpriced, 1), funding_bps=round(-side * fund, 1), hours_left=round(hours, 1),
               side=side, expect_bps=round(expect, 1), mark=mark, oracle=oracle,
               quote_age_h=round((now_ms - quote_ms) / HOUR, 1) if quote_ms else None)
    return out


def roll_ticket(read: dict, open_now: bool, now_ms: int, day_pct: float | None = None) -> dict | None:
    """A roll ticket from a roll_read, or None (with the reason in read["why_not"])."""
    if "expect_bps" not in read:
        return None
    if read["phase"] == "after" or now_ms < read["first_set_ms"] - ROLL_EARLY_H * HOUR:
        read["why_not"] = "outside the window"
        return None
    if now_ms > read["end_ms"] - ROLL_LAST_ENTRY_H * HOUR:
        read["why_not"] = "too close to the last step"
        return None
    if not open_now:
        read["why_not"] = "the real market is shut"
        return None
    if read.get("quote_age_h") is not None and read["quote_age_h"] > ROLL_QUOTE_AGE_H:
        read["why_not"] = "futures prices too old"
        return None
    if read["expect_bps"] < ROLL_MIN_BPS:
        read["why_not"] = f"expects {bps(read['expect_bps'])}, needs {bps(ROLL_MIN_BPS)}"
        return None
    s, lab = read["side"], read["label"]
    word = "Long" if s > 0 else "Short"
    nxt = "dearer" if read["gap_pct"] > 0 else "cheaper"
    why = [f"{lab} rolls {read['front']} → {read['next']} with {read['front_weight']:.0%} still on {read['front']}. "
           f"{read['next']} is {abs(read['gap_pct']):.2f}% {nxt}, so the oracle has {bps(read['left_bps'])} still to move "
           f"from the roll alone.",
           f"Hyperliquid's price is {bps(read['basis_bps'])} against the oracle, so {bps(read['unpriced_bps'])} isn't "
           f"priced in. Funding to the end of the roll: {bps(read['funding_bps'])} for a {word.lower()}.",
           f"After an {ROLL_COST_BPS:.0f} bps taker round trip: {bps(read['expect_bps'])} expected if oil itself doesn't move."]
    if day_pct:
        why.append(f"Risk: {lab}'s usual day is ±{day_pct:.1f}% ({day_pct * 100:.0f} bps), far bigger than this edge. "
                   "Keep it small; it pays over many rolls, not on one.")
    return {"key": f"roll:{read['market']}:{read['start']}", "watcher": "roll", "market": read["market"],
            "title": f"{word} {lab} into the roll: {bps(read['expect_bps'])} expected",
            "side": "long" if s > 0 else "short", "opened_ms": now_ms,
            "expires_ms": read["end_ms"] + ROLL_EXIT_MIN * 60_000, "expect_bps": read["expect_bps"],
            "cost_bps": ROLL_COST_BPS, "why": why,
            "exit": f"Close 30 minutes after the last step ({_when(read['end_ms'] + ROLL_EXIT_MIN * 60_000)}).",
            "legs": [{"kind": "perp", "coin": read["market"], "dir": s, "entry": read["mark"], "last": read["mark"]}]}


def carry_read(coin: str, hist: list[tuple[int, float]], ctx: dict, spot: dict | None, now_ms: int) -> dict:
    """What the carry watcher sees for one coin: funding now, over a day and over 3 days (% a year)."""
    out = {"market": coin, "spot": CARRY_SPOT.get(coin), "now_apr": apr(ctx.get("funding")),
           "day_apr": apr(mean_rate(hist, now_ms - DAY, now_ms)),
           "three_day_apr": apr(mean_rate(hist, now_ms - 3 * DAY, now_ms)),
           "spot_px": (spot or {}).get("px"), "spot_usd": (spot or {}).get("day_volume"), "mark": ctx.get("mark")}
    if out["three_day_apr"] is not None:
        hourly = out["three_day_apr"] / 100 / (24 * 365)
        out["expect_bps"] = round(hourly * CARRY_HOLD_H * 1e4 * CARRY_KEEP - CARRY_COST_BPS, 1)
    return out


def carry_ticket(read: dict, now_ms: int) -> dict | None:
    if read.get("expect_bps") is None or read["now_apr"] is None:
        read["why_not"] = "no funding history yet"
        return None
    if not read["spot_px"] or not read["mark"]:
        read["why_not"] = "no spot price"
        return None
    if (read["spot_usd"] or 0) < CARRY_MIN_SPOT_USD:
        read["why_not"] = "spot market too thin"
        return None
    if read["three_day_apr"] < CARRY_ENTER_APR or read["now_apr"] < CARRY_NOW_APR:
        read["why_not"] = (f"funding {read['three_day_apr']:.0f}% a year over 3 days, {read['now_apr']:.0f}% now; "
                           f"needs {CARRY_ENTER_APR:.0f}% and {CARRY_NOW_APR:.0f}%")
        return None
    if read["expect_bps"] < CARRY_MIN_BPS:
        read["why_not"] = f"expects {bps(read['expect_bps'])}, needs {bps(CARRY_MIN_BPS)}"
        return None
    c, sp = read["market"], read["spot"]
    days = CARRY_HOLD_H // 24
    why = [f"{c} funding has averaged {read['three_day_apr']:.0f}% a year over 3 days and is {read['now_apr']:.0f}% now: "
           f"shorts are being paid that much to hold.",
           f"Buy {sp} spot and short the same amount of {c} perp: the price moves cancel, the funding stays.",
           f"If half of that rate lasts {days} days, after both legs' taker fees ({CARRY_COST_BPS:.0f} bps): "
           f"{bps(read['expect_bps'])} on the position.",
           "Risk: the short needs margin if the price jumps; keep spare collateral, or the short is closed and "
           "the spot is left unhedged."]
    return {"key": f"carry:{c}:{now_ms // DAY}", "watcher": "carry", "market": c,
            "title": f"Carry {c}: buy {sp}, short the perp, {bps(read['expect_bps'])} expected over {days} days",
            "side": "carry", "opened_ms": now_ms, "expires_ms": now_ms + CARRY_MAX_DAYS * DAY,
            "expect_bps": read["expect_bps"], "cost_bps": CARRY_COST_BPS, "why": why,
            "exit": f"Close when a day's funding averages under {CARRY_EXIT_APR:.0f}% a year, or after {CARRY_MAX_DAYS} days.",
            "legs": [{"kind": "spot", "coin": sp, "dir": 1, "entry": read["spot_px"], "last": read["spot_px"]},
                     {"kind": "perp", "coin": c, "dir": -1, "entry": read["mark"], "last": read["mark"]}]}


def carry_done(read: dict) -> bool:
    return read.get("day_apr") is not None and read["day_apr"] < CARRY_EXIT_APR


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, E.NY).strftime("%a %d %b %H:%M New York")


# ── the paper book ───────────────────────────────────────────────────────────
def pnl(t: dict) -> dict:
    """A ticket's paper result in bps of the position: price, funding, fees, net."""
    price = sum(g["dir"] * (g["last"] / g["entry"] - 1) * 1e4 for g in t["legs"] if g["entry"] and g["last"])
    gross = price + t.get("funding_bps", 0.0)
    return {"price_bps": round(price, 1), "funding_bps": round(t.get("funding_bps", 0.0), 1),
            "cost_bps": t["cost_bps"], "net_bps": round(gross - t["cost_bps"], 1)}


class Book:
    """Every ticket the watchers opened, followed to its close. Kept in a JSON file so a restart keeps the record."""

    MAX_GAP_H = 2.0                # funding isn't counted across a longer outage (the record says so)

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.tickets: list[dict] = []
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            self.tickets = json.loads(self.path.read_text()).get("tickets", [])
        except (OSError, ValueError):
            self.tickets = []

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tickets": self.tickets[-2000:]}))
        os.replace(tmp, self.path)

    def open_tickets(self, watcher: str | None = None) -> list[dict]:
        return [t for t in self.tickets if t["status"] == "open" and (watcher is None or t["watcher"] == watcher)]

    def has(self, key: str) -> bool:
        return any(t["key"] == key for t in self.tickets)

    def resting(self, watcher: str, market: str, now_ms: int, hours: float) -> bool:
        """An open ticket on this market, or one closed less than `hours` ago."""
        for t in self.tickets:
            if t["watcher"] == watcher and t["market"] == market and t.get("rule") == RULES[watcher]:
                if t["status"] == "open" or (t.get("closed_ms") or 0) > now_ms - hours * HOUR:
                    return True
        return False

    def add(self, t: dict) -> dict:
        t = {**t, "id": f"{t['key']}:{t['opened_ms']}", "status": "open", "funding_bps": 0.0, "last_ms": t["opened_ms"],
             "closed_ms": None, "closed_why": None, "gaps": 0, "rule": RULES[t["watcher"]]}
        self.tickets.append(t)
        return t

    def mark(self, prices: dict, funding: dict, now_ms: int) -> None:
        """Bring every open ticket up to date. prices {(kind, coin): px}, funding {perp coin: hourly rate}."""
        for t in self.open_tickets():
            dt = (now_ms - t["last_ms"]) / HOUR
            if dt <= 0:
                continue
            if dt > self.MAX_GAP_H:
                t["gaps"] += 1
                dt = 0.0
            for g in t["legs"]:
                px = prices.get((g["kind"], g["coin"]))
                if px:
                    g["last"] = px
                if g["kind"] == "perp" and funding.get(g["coin"]) is not None:
                    t["funding_bps"] += -g["dir"] * funding[g["coin"]] * dt * 1e4
            t["last_ms"] = now_ms

    def close(self, t: dict, now_ms: int, why: str) -> dict:
        t["status"], t["closed_ms"], t["closed_why"] = "closed", now_ms, why
        t["result"] = pnl(t)
        return t

    def expire(self, now_ms: int) -> list[dict]:
        return [self.close(t, now_ms, "time") for t in self.open_tickets() if now_ms >= t["expires_ms"]]

    def record(self, watcher: str) -> dict:
        """The watcher's closed tickets under its current rule: count, share that made money after costs, average
        and total net (bps), and against what each ticket expected."""
        done = [t for t in self.tickets if t["watcher"] == watcher and t["status"] == "closed"
                and t.get("rule") == RULES[watcher]]
        nets = [t["result"]["net_bps"] for t in done]
        exp = [t["expect_bps"] for t in done]
        return {"n": len(done), "won": sum(1 for x in nets if x > 0), "avg_bps": round(sum(nets) / len(nets), 1) if nets else None,
                "total_bps": round(sum(nets), 1), "expected_avg_bps": round(sum(exp) / len(exp), 1) if exp else None,
                "open": len(self.open_tickets(watcher))}


# ── the carry rule on history (scripts/carryStudy.py) ────────────────────────
def carry_history(coin: str, hist: list[tuple[int, float]], perp: list[tuple[int, float]],
                  spot: list[tuple[int, float]]) -> dict:
    """The live carry rule, unchanged, run hour by hour over a coin's funding history. Each trade: funding collected
    (the short is paid each hour's rate), the spot-perp price gap's change (long spot, short perp, from the closes at
    or before entry and exit), the taker fees, and how much of the 3-day rate at entry the hold actually earned.
    Also the always-on carry: holding spot and short perp the whole time."""
    hist = sorted(hist)
    pp, sp = _series(perp), _series(spot)
    trades, cur, rest_until = [], None, 0
    for i in range(len(hist)):
        t, rate = hist[i]
        window = hist[max(0, i - 96):i + 1]
        if cur is not None:
            cur["funding_bps"] += rate * 1e4
            cur["hours"] += 1
            read = carry_read(coin, window, {"mark": pp(t), "funding": rate}, None, t)
            if carry_done(read) or t >= cur["t"] + CARRY_MAX_DAYS * DAY:
                p0, p1, s0, s1 = pp(cur["t"]), pp(t), sp(cur["t"]), sp(t)
                basis = ((s1 / s0) - (p1 / p0)) * 1e4 if p0 and p1 and s0 and s1 else 0.0
                net = cur["funding_bps"] + basis - CARRY_COST_BPS
                trades.append({"t": cur["t"], "end": t, "days": cur["hours"] / 24, "funding_bps": cur["funding_bps"],
                               "basis_bps": basis, "net_bps": net, "expect_bps": cur["expect_bps"],
                               "kept": (cur["funding_bps"] / cur["hours"] / 1e4) / cur["entry_rate"] if cur["entry_rate"] > 0 else None})
                cur, rest_until = None, t + CARRY_REST_H * HOUR
            continue
        if t < rest_until or not pp(t) or not sp(t):
            continue
        read = carry_read(coin, window, {"mark": pp(t), "funding": rate}, {"px": sp(t), "day_volume": float("inf")}, t)
        if carry_ticket(read, t):
            cur = {"t": t, "funding_bps": 0.0, "hours": 0, "expect_bps": read["expect_bps"],
                   "entry_rate": read["three_day_apr"] / 100 / (24 * 365)}
    rates = [r for _, r in hist]
    return {"coin": coin, "from": hist[0][0] if hist else None, "to": hist[-1][0] if hist else None, "trades": trades,
            "open": cur is not None, "always_apr": apr(sum(rates) / len(rates)) if rates else None,
            "negative_share": sum(1 for r in rates if r < 0) / len(rates) if rates else None,
            "worst_30d_bps": _worst_window(rates, 30 * 24)}


def _series(rows: list[tuple[int, float]]):
    """'The last value at or before t' over sorted (t, value) rows, as a function."""
    from bisect import bisect_right
    rows = sorted(rows)
    ts = [r[0] for r in rows]

    def at(t: int):
        i = bisect_right(ts, t) - 1
        return rows[i][1] if i >= 0 else None
    return at


def _worst_window(rates: list[float], n: int) -> float | None:
    if len(rates) < n:
        return None
    s = sum(rates[:n])
    worst = s
    for i in range(n, len(rates)):
        s += rates[i] - rates[i - n]
        worst = min(worst, s)
    return worst * 1e4


def summarise(trades: list[dict]) -> dict:
    """Count, average net and its t, the share that made money, each half's average, and the median share of the
    3-day rate that lasted."""
    nets = [t["net_bps"] for t in trades]
    if not nets:
        return {"n": 0}
    n = len(nets)
    m = sum(nets) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in nets) / (n - 1)) if n > 1 else 0.0
    kept = sorted(t["kept"] for t in trades if t.get("kept") is not None)
    h = n // 2
    return {"n": n, "avg_bps": m, "t": m / (sd / math.sqrt(n)) if sd > 0 else 0.0, "won": sum(1 for x in nets if x > 0) / n,
            "first": sum(nets[:h]) / h if h else None, "second": sum(nets[h:]) / (n - h),
            "days": sum(t["days"] for t in trades) / n, "funding_bps": sum(t["funding_bps"] for t in trades) / n,
            "basis_bps": sum(t["basis_bps"] for t in trades) / n, "kept": kept[len(kept) // 2] if kept else None}

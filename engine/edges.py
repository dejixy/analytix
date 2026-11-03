"""
Event edges: the scheduled moments the edge terminal watches, as pure functions. api/edges.py feeds them live data.

    sessions    when the real market behind each trade.xyz commodity is open (New York time, so daylight saving is
                handled): Sunday 18:00 to Friday 17:00 (Brent 18:00), with a 17:00 to 18:00 break Monday to Thursday.
                While it's shut, Hyperliquid's own order book sets the price, inside a band of ±1/max leverage
                around the last real price; at the reopen the price snaps back to the real market
    weekends    every past weekend closure, from hourly candles:
                    W   last hour before the reopen against Friday's close (what Hyperliquid priced)
                    R   the hour after the reopen against Friday's close (what the real market said)
                and the fitted R = a + b·W per market and pooled. b below 1 means big weekend moves give some back
                at the reopen; fading them is judged after a taker round trip
    roll        oil's monthly switch to the next futures contract: from the 5th to the 10th business day the
                oracle moves 20% a day from the front contract to the next, so if the next one is cheaper the
                oracle drifts down by the gap even when nothing else happens
    events      the recurring moments of the next days: oil reports, closes and reopens, roll days, options expiry

Holidays: full closures (Good Friday, Christmas, New Year) are modelled; part-days (Thanksgiving, July 4 and the
like) are not, and weekends next to any US market holiday are left out of the weekend record.
"""
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
HOUR = 3_600_000
DAY = 86_400_000

TAKER_BPS = 9.0          # trade.xyz tier 0 (twice Hyperliquid's own perps); growth mode cuts some markets by 90%
MAKER_BPS = 3.0
FADE_MIN_BPS = 100.0     # a weekend move this big or more counts as "big" for the fade
MIN_FIT = 8              # weekends a market needs before its own fit replaces the published one
# Blockworks Research, 17 weekends (Mar to Jul 2026), 68 trade.xyz markets pooled: reopen = 0.834 · weekend − 15 bps
PUBLISHED_FIT = {"a": -15.0, "b": 0.834, "n": 614, "source": "Blockworks Research, 68 markets, Mar to Jul 2026"}


@dataclass(frozen=True)
class Session:
    name: str              # trade.xyz ticker
    label: str
    kind: str              # "oil" | "metal"
    fri_close_h: int       # Friday close, New York hour
    bound: float           # closed-hours band, fraction (1 / max leverage)


SESSIONS = {
    "CL": Session("CL", "WTI oil", "oil", 17, 0.05),          # WTIOIL on trade.xyz's screens, CL in the API
    "BRENTOIL": Session("BRENTOIL", "Brent oil", "oil", 18, 0.05),
    "GOLD": Session("GOLD", "Gold", "metal", 17, 0.04),
    "SILVER": Session("SILVER", "Silver", "metal", 17, 0.04),
}

# Whole-day closures of US futures (no session for that trade date)
FULL_CLOSURES = {date(2026, 4, 3), date(2026, 12, 25), date(2027, 1, 1), date(2027, 3, 26), date(2027, 12, 24)}
# US exchange holidays: business days for the roll, and weekends next to them are left out of the weekend record
US_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}


ALIASES = {"WTIOIL": "CL"}                                # trade.xyz's display name → its API name


def bare(name: str) -> str:
    """"xyz:GOLD" → "GOLD" (and the display name "WTIOIL" → "CL")."""
    b = name.split(":", 1)[1] if ":" in name else name
    return ALIASES.get(b, b)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ny(d: date, h: int) -> int:
    return _ms(datetime(d.year, d.month, d.day, h, tzinfo=NY))


def ny_date(t_ms: float) -> date:
    return datetime.fromtimestamp(t_ms / 1000, NY).date()


# ── sessions ──────────────────────────────────────────────────────────────
def boundaries(name: str, start: date, days: int) -> list[tuple[int, str, str]]:
    """(ms, "open" | "close", "weekend" | "break" | "holiday") for New York dates start .. start + days."""
    s = SESSIONS[bare(name)]
    out = []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        wd = d.weekday()                                   # Monday 0 .. Sunday 6
        nxt = d + timedelta(days=1)
        if wd <= 3:                                        # Monday to Thursday: the 17:00 to 18:00 break
            if nxt in FULL_CLOSURES:                       # tomorrow is shut: tonight's close lasts until after it
                out.append((_ny(d, 17), "close", "holiday"))
            elif d in FULL_CLOSURES:
                out.append((_ny(d, 18), "open", "holiday"))
            else:
                out += [(_ny(d, 17), "close", "break"), (_ny(d, 18), "open", "break")]
        elif wd == 4 and d not in FULL_CLOSURES:
            out.append((_ny(d, s.fri_close_h), "close", "weekend"))
        elif wd == 6:
            out.append((_ny(d, 18), "open", "weekend"))
    return sorted(out)


def session_state(name: str, t_ms: float) -> dict:
    """Whether the real market is open at t, why not, and when that changes."""
    d0 = ny_date(t_ms)
    bs = boundaries(name, d0 - timedelta(days=6), 14)
    past = [b for b in bs if b[0] <= t_ms]
    future = [b for b in bs if b[0] > t_ms]
    last = past[-1] if past else None
    is_open = last is None or last[1] == "open"
    nxt = next((b for b in future if b[1] != (last[1] if last else "open")), None) if last else None
    nxt_close = next((b for b in future if b[1] == "close"), None)
    nxt_weekend = next((b for b in future if b[1] == "close" and b[2] in ("weekend", "holiday")), None)
    return {"open": is_open, "why": None if is_open else last[2], "since_ms": last[0] if last else None,
            "next_change_ms": nxt[0] if nxt else None, "next_close_ms": nxt_close[0] if nxt_close else None,
            "next_long_close_ms": nxt_weekend[0] if nxt_weekend else None}


def weekends(name: str, start_ms: float, end_ms: float) -> list[tuple[int, int]]:
    """(Friday close, Sunday reopen) of every whole weekend closure between start and end, holiday weekends out."""
    out = []
    d = ny_date(start_ms)
    d += timedelta(days=(4 - d.weekday()) % 7)             # the first Friday
    s = SESSIONS[bare(name)]
    while _ny(d, 0) < end_ms:
        mon = d + timedelta(days=3)
        near_holiday = any(x in US_HOLIDAYS for x in (d - timedelta(days=1), d, mon))
        close, reopen = _ny(d, s.fri_close_h), _ny(d + timedelta(days=2), 18)
        if not near_holiday and close >= start_ms and reopen + HOUR <= end_ms:
            out.append((close, reopen))
        d += timedelta(days=7)
    return out


# ── weekend record ────────────────────────────────────────────────────────
def _price_at(closes: dict[int, float], hour_start: int, look_back: int = 3) -> float | None:
    """The close of the hourly candle starting at hour_start, or of the last one before it within look_back hours
    (thin weekend hours can have no trades)."""
    for k in range(look_back + 1):
        p = closes.get(hour_start - k * HOUR)
        if p:
            return p
    return None


def weekend_rows(name: str, candles: list[tuple[int, float]]) -> list[dict]:
    """One row per past weekend: Friday close, last price before the reopen, price an hour after, W, R, R − W."""
    if not candles:
        return []
    closes = {int(t): float(c) for t, c in candles if c}
    rows = []
    for close, reopen in weekends(name, min(closes), max(closes) + HOUR):
        p0 = _price_at(closes, close - HOUR)               # the candle ending at Friday's close
        p1 = _price_at(closes, reopen - HOUR)              # the candle ending at the reopen
        p2 = closes.get(reopen)                            # the candle from the reopen to an hour after
        if not (p0 and p1 and p2):
            continue
        w = math.log(p1 / p0) * 1e4
        r = math.log(p2 / p0) * 1e4
        rows.append({"close_ms": close, "reopen_ms": reopen, "fri": p0, "pre": p1, "post": p2,
                     "w_bps": round(w, 1), "r_bps": round(r, 1), "snap_bps": round(r - w, 1)})
    return rows


def fit(rows: list[dict]) -> dict | None:
    """R = a + b·W by least squares, with how much of R it explains."""
    n = len(rows)
    if n < 3:
        return None
    w = [x["w_bps"] for x in rows]
    r = [x["r_bps"] for x in rows]
    mw, mr = sum(w) / n, sum(r) / n
    sww = sum((x - mw) ** 2 for x in w)
    if sww <= 0:
        return None
    b = sum((x - mw) * (y - mr) for x, y in zip(w, r)) / sww
    a = mr - b * mw
    ss = sum((y - mr) ** 2 for y in r)
    res = sum((y - a - b * x) ** 2 for x, y in zip(w, r))
    return {"a": round(a, 1), "b": round(b, 3), "r2": round(1 - res / ss, 2) if ss > 0 else None, "n": n}


def fade(rows: list[dict], min_bps: float = FADE_MIN_BPS, cost_bps: float = 2 * TAKER_BPS) -> dict:
    """Fading every weekend move of min_bps or more: in just before the reopen, out an hour after, after costs."""
    picks = [(-1 if x["w_bps"] > 0 else 1) * x["snap_bps"] - cost_bps for x in rows if abs(x["w_bps"]) >= min_bps]
    n = len(picks)
    if not n:
        return {"n": 0, "min_bps": min_bps, "cost_bps": cost_bps}
    m = sum(picks) / n
    sd = math.sqrt(sum((p - m) ** 2 for p in picks) / (n - 1)) if n > 1 else None
    return {"n": n, "min_bps": min_bps, "cost_bps": cost_bps, "avg_bps": round(m, 1),
            "t": round(m / (sd / math.sqrt(n)), 2) if sd else None, "win": round(sum(p > 0 for p in picks) / n, 2)}


def expected_snap(w_bps: float, f: dict | None) -> dict:
    """What the reopen is expected to do from here, R − W, with whose fit."""
    use = f if f and f.get("n", 0) >= MIN_FIT else PUBLISHED_FIT
    return {"snap_bps": round(use["a"] + (use["b"] - 1) * w_bps, 1), "from": "ours" if use is f else "published"}


# ── oil roll ──────────────────────────────────────────────────────────────
CODES = "FGHJKMNQUVXZ"                                     # futures month codes, January to December
ACTIVE = {"CL": "GHJKMNQUVXZF", "BRENTOIL": "HJKMNQUVXZFG"}   # contract live at the start of each month
YAHOO_ROOT = {"CL": "CL", "BRENTOIL": "BZ"}           # NYMEX symbols (Brent's NYMEX look-alike)


def business_days(year: int, month: int) -> list[date]:
    d, out = date(year, month, 1), []
    while d.month == month:
        if d.weekday() < 5 and d not in US_HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def contract(name: str, year: int, month: int) -> tuple[str, int, int]:
    """(code, contract year, contract month) live at the start of that month."""
    code = ACTIVE[bare(name)][month - 1]
    cm = CODES.index(code) + 1
    return code, year if cm > month else year + 1, cm


def _label(code: str, y: int) -> str:
    return f"{code}{y % 100:02d}"


def roll_state(name: str, t_ms: float) -> dict:
    """This month's roll window (or next month's, once it's over), today's weights and the two contracts."""
    today = ny_date(t_ms)
    y, m = today.year, today.month
    for _ in range(2):
        bd = business_days(y, m)
        start, end = bd[4], bd[9]
        if today <= end:
            break
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    front, fy, _ = contract(name, y, m)
    nm_y, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nxt, ny_, _ = contract(name, nm_y, nm)
    if today < start:
        phase, w_front = "before", 1.0
    else:
        k = bd.index(today) if today in bd else sum(d <= today for d in bd) - 1   # weekends keep Friday's weight
        w_front = max(0.0, 1.0 - 0.2 * (k - 4))
        phase = "during" if today <= end else "after"
    root = YAHOO_ROOT[bare(name)]
    return {"start": start.isoformat(), "end": end.isoformat(), "phase": phase, "front_weight": round(w_front, 2),
            "front": _label(front, fy), "next": _label(nxt, ny_),
            "symbols": [f"{root}{_label(front, fy)}.NYM", f"{root}{_label(nxt, ny_)}.NYM"],
            "days": [{"date": d.isoformat(), "front_weight": round(max(0.0, 1.0 - 0.2 * (i - 4)), 1) if i >= 4 else 1.0}
                     for i, d in enumerate(bd[:11])]}


def roll_drift(state: dict, front_px: float | None, next_px: float | None) -> dict | None:
    """How far the oracle still drifts from the roll alone (both contracts unchanged), bps."""
    if not front_px or not next_px:
        return None
    w = state["front_weight"]
    blend = w * front_px + (1 - w) * next_px
    return {"gap_pct": round((next_px / front_px - 1) * 100, 2), "left_bps": round(w * (next_px - front_px) / blend * 1e4, 1)}


# ── what's coming ─────────────────────────────────────────────────────────
def _last_friday(y: int, m: int) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 4) % 7)


def recurring(t_ms: float, days: int = 7) -> list[dict]:
    """The recurring moments of the next days: {"t_ms", "title", "group", "note"}."""
    out = []
    d0 = ny_date(t_ms)
    for i in range(days + 1):
        d = d0 + timedelta(days=i)
        wd = d.weekday()
        if wd == 4:                                        # crypto options don't keep US holidays
            utc8 = _ms(datetime(d.year, d.month, d.day, 8, tzinfo=UTC))
            last = _last_friday(d.year, d.month) == d
            big = "quarterly" if last and d.month in (3, 6, 9, 12) else "monthly" if last else "weekly"
            out.append((utc8, f"Deribit options expiry ({big})", "crypto", "BTC and ETH options settle"))
        if d in US_HOLIDAYS:
            continue
        if wd == 1:
            out.append((_ny(d, 16) + 30 * 60_000, "API crude stocks", "oil", "industry's weekly count, a day before the EIA"))
        if wd == 2:
            out.append((_ny(d, 10) + 30 * 60_000, "EIA crude stocks", "oil", "official weekly US inventories"))
        if wd == 4:
            out.append((_ny(d, 13), "Baker Hughes rig count", "oil", ""))
            out.append((_ny(d, 15) + 30 * 60_000, "CFTC positioning", "macro", "who is long and short futures, as of Tuesday"))
            out.append((_ny(d, 17), "WTI, gold and silver close", "session", "Hyperliquid keeps trading them alone"))
            out.append((_ny(d, 18), "Brent closes", "session", "Hyperliquid keeps trading it alone"))
        if wd == 6:
            out.append((_ny(d, 18), "Oil, gold and silver reopen", "session", "prices snap back to the real market"))
    for name in ("CL", "BRENTOIL"):
        r = roll_state(name, t_ms)
        for key, what in (("start", "starts"), ("end", "ends")):
            d = date.fromisoformat(r[key])
            if d0 <= d <= d0 + timedelta(days=days):
                out.append((_ny(d, 0), f"{SESSIONS[name].label} roll {what}", "oil",
                            f"{r['front']} → {r['next']}, 20% a day", True))
    rows = [{"t_ms": x[0], "title": x[1], "group": x[2], "note": x[3], "all_day": len(x) > 4} for x in out]
    return sorted((e for e in rows if e["t_ms"] > t_ms or (e["all_day"] and ny_date(e["t_ms"]) == d0)),
                  key=lambda e: e["t_ms"])

"""
Where did a move start: on Hyperliquid, or on a bigger exchange first?

Most price discovery for majors happens on Binance's and Bybit's perps, and Hyperliquid usually follows
within a second. A move that shows up on Binance clearly *before* Hyperliquid was a move across the market
that arrived here. One that shows up here first, or only here, came from Hyperliquid's own traders (a
liquidation cascade, a whale, a TWAP); when other exchanges don't follow, arbitrage traders tend to pull
Hyperliquid's price back toward theirs.

The stretch compared: the whole window for 1m; for 10m and 60m the sharpest minute, with 30 seconds of
run-up. Two tests:

  1. Size. If the other exchange moved less than ONLY_HERE as far in the same direction, the move was
     Hyperliquid's own ("Hyperliquid only").
  2. Timing, the standard lead-lag measure. Both prices are put on a GRID_MS grid (each on its own
     exchange's clock, both kept close to true time) and the quarter-second price changes of one are
     compared with the other's shifted by up to MAX_LAG_S either way. The shift where they match best
     (highest correlation) is how far one led the other:

        the other exchange led by ≥ LEAD_S     "started on Binance": Hyperliquid followed
        Hyperliquid led by ≥ LEAD_S            "started on Hyperliquid"
        within LEAD_S                          "moved with Binance": a move across the whole market

     If no shift matches well (correlation under MIN_CORR), no claim is made.
"""
import math
from bisect import bisect_right

import numpy as np

from models.explanationModel import Origin

VENUES = {"binance": "Binance", "bybit": "Bybit"}          # in order of preference when both have data
GRID_MS = 250
MAX_LAG_S = 10
LEAD_S = 1.0
MIN_CORR = 0.15
ONLY_HERE = 0.3                                            # the other exchange moved less than 30% as far
MIN_POINTS = 4
RUN_UP_S = 30


def clip(path, start_ms: int, end_ms: int) -> tuple:
    """The part of a (time, price) path from the last point at or before start_ms through end_ms."""
    lo = max(0, bisect_right(path, start_ms, key=lambda p: p[0]) - 1)
    hi = bisect_right(path, end_ms, key=lambda p: p[0])
    return tuple(path[lo:hi])


def external_paths(buffers: dict, start_ms: int, end_ms: int) -> dict:
    """venue → ((time, mid), ...) for [start_ms, end_ms] plus the quote just before, read from the newest end."""
    out = {}
    for venue, buf in buffers.items():
        pts = []
        for q in reversed(buf._buf):
            if q.timestamp > end_ms:
                continue
            pts.append((q.timestamp, q.mid))
            if q.timestamp <= start_ms:
                break
        if pts:
            out[venue] = tuple(reversed(pts))
    return out


def origin_for(here_path, buffers: dict | None, start_ms: int, end_ms: int, seconds: int, burst_end_ms: int,
               burst_span_s: int, sign: int) -> Origin | None:
    """Where a window's move started, reading only the stretch that's compared (cheap enough to run on every
    window that's moving; never run on quiet ones)."""
    if not buffers or sign == 0:
        return None
    t0, t1 = segment(start_ms, end_ms, seconds, burst_end_ms, burst_span_s)
    return find_origin(clip(here_path, t0 - 1_000, t1), external_paths(buffers, t0 - MAX_LAG_S * 1000 - 1_000, t1),
                       t0, t1, sign)


def segment(start_ms: int, end_ms: int, seconds: int, burst_end_ms: int, burst_span_s: int) -> tuple[int, int]:
    """The stretch to time: the whole minute, or the sharpest minute (with its run-up) of a longer window."""
    if seconds <= 60 or not burst_end_ms:
        return start_ms, end_ms
    t1 = min(end_ms, burst_end_ms + 5_000)
    return max(start_ms, burst_end_ms - burst_span_s * 1000 - RUN_UP_S * 1000), t1


def _grid(path, t0: int, n: int) -> np.ndarray | None:
    """Log price at t0, t0 + GRID_MS, ... (n points), each the last price at or before that moment."""
    ts = np.array([p[0] for p in path], dtype=np.int64)
    px = np.array([p[1] for p in path], dtype=float)
    at = t0 + np.arange(n, dtype=np.int64) * GRID_MS
    idx = np.searchsorted(ts, at, side="right") - 1
    if idx[0] < 0 or np.any(px <= 0):
        return None
    return np.log(px[idx])


def lead_lag(here, there, t0: int, t1: int) -> tuple[float, float] | None:
    """(seconds the other exchange led by, correlation at that shift); negative = Hyperliquid led.
    Hyperliquid's changes from t0 to t1 − MAX_LAG_S are compared with the other exchange's, shifted by up to
    MAX_LAG_S either way (the other exchange's prices from t0 − MAX_LAG_S to t1 are needed for that)."""
    k_max = MAX_LAG_S * 1000 // GRID_MS
    n = (t1 - t0) // GRID_MS + 1
    a, b = _grid(here, t0, n), _grid(there, t0 - MAX_LAG_S * 1000, n + k_max)
    if a is None or b is None:
        return None
    da, db = np.diff(a), np.diff(b)
    m = len(da) - k_max
    if m < 8 or not da[:m].std() > 0:
        return None
    x = da[:m] - da[:m].mean()
    # row j holds the other exchange's changes starting j steps in: here(t) against there(t − k), k = k_max − j
    y = np.lib.stride_tricks.sliding_window_view(db, m)[: 2 * k_max + 1]
    y = y - y.mean(axis=1, keepdims=True)
    norm = np.sqrt((y * y).sum(axis=1)) * np.sqrt((x * x).sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(norm > 0, (y @ x) / norm, -np.inf)
    lags = k_max - np.arange(len(corr))
    top = corr.max()
    if not np.isfinite(top):
        return None
    k = int(lags[np.flatnonzero(corr >= top - 1e-9)][np.argmin(np.abs(lags[corr >= top - 1e-9]))])
    return k * GRID_MS / 1000, float(top)


def _move(path, t0: int, t1: int) -> float | None:
    g = _grid(path, t0, 1)
    e = _grid(path, t1, 1)
    return None if g is None or e is None else float(e[0] - g[0])


def find_origin(here: tuple, ext: dict | None, t0: int, t1: int, sign: int) -> Origin | None:
    """`here`: Hyperliquid's (time, mid) path; `ext`: venue → path. None when there's nothing to compare."""
    if not ext or sign == 0 or len(here) < MIN_POINTS:
        return None
    m_here = _move(here, t0, t1)
    if m_here is None or m_here * sign <= 0:
        return None
    for venue, name in VENUES.items():
        path = ext.get(venue)
        if (not path or sum(1 for ts, _ in path if t0 <= ts <= t1) < MIN_POINTS
                or path[0][0] > t0 - MAX_LAG_S * 1000):
            continue
        m_there = _move(path, t0, t1)
        if m_there is None:
            continue
        span = max(1, round((t1 - t0) / 1000))
        pct = lambda m: math.expm1(m) * 100                 # noqa: E731
        if m_there * sign < ONLY_HERE * abs(m_here):
            return _origin("here_only", name, 0.0, pct(m_here), pct(m_there), span)
        ll = lead_lag(here, path, t0, t1)
        if ll is None or ll[1] < MIN_CORR:
            return None
        lead = ll[0]
        verdict = "elsewhere" if lead >= LEAD_S else "here" if lead <= -LEAD_S else "together"
        return _origin(verdict, name, lead, pct(m_here), pct(m_there), span)
    return None


def _origin(verdict: str, venue: str, lead: float, here: float, there: float, span: int) -> Origin:
    label = {"elsewhere": f"started on {venue}", "here": "started on Hyperliquid",
             "together": f"moved with {venue}", "here_only": "Hyperliquid only"}[verdict]
    return Origin(verdict, venue, round(lead, 2), here, there, span, label)


def origin_sentence(o: Origin) -> str:
    over = "over the minute" if o.span_s <= 75 else "over the sharpest minute"
    moves = f"{o.venue} {o.there_pct:+.2f}%, Hyperliquid {o.here_pct:+.2f}% {over}"
    if o.verdict == "elsewhere":
        return (f"Where it started: {o.venue} moved first, about {o.lead_s:.1f}s before Hyperliquid ({moves}). "
                f"A move across the market that reached Hyperliquid, not one started by its own traders.")
    if o.verdict == "here":
        return (f"Where it started: Hyperliquid moved first, {o.venue} followed about {-o.lead_s:.1f}s later "
                f"({moves}). It began with Hyperliquid's own traders.")
    if o.verdict == "together":
        return (f"Where it started: Hyperliquid and {o.venue} moved together, within {LEAD_S:g}s of each other "
                f"({moves}). A move across the whole market.")
    return (f"Where it started: only on Hyperliquid. {o.venue} barely moved ({moves}), so this was Hyperliquid's "
            f"own flow. When other exchanges don't follow, arbitrage traders tend to pull the price back toward theirs.")


def origin_alert_line(o: Origin) -> str:
    """One short line for a Telegram alert."""
    if o.verdict == "elsewhere":
        return f"{o.venue} moved first, about {o.lead_s:.1f}s earlier: this came from the wider market."
    if o.verdict == "here":
        return f"Hyperliquid moved first; {o.venue} followed about {-o.lead_s:.1f}s later."
    if o.verdict == "together":
        return f"{o.venue} moved at the same time: a move across the whole market."
    return f"{o.venue} barely moved ({o.there_pct:+.2f}%): this was Hyperliquid's own flow."

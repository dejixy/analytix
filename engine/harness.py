"""
The profit harness: prices trade ideas the way a bot on this server would really trade them, from the fast store
(storage/tickStore.py), so every idea is judged on the same honest terms.

    timing      a decision at t acts at t + DELAY_MS (the feed reaches the server ~0.4 s late, the order takes time)
    market      a market buy fills at the first taker-buy print from then on, a market sell at the first taker-sell
                print: prices someone really paid, never the mid (on a quiet coin with no such print within
                MAX_GAP_MS, the ask or bid as known then)
    limit       a buy posted at the best bid fills only if a later taker sell prints *below* it within the wait
                ("through": the queue ahead at our price is assumed never to clear, which is conservative). Unfilled,
                the entry is skipped or chased with a market order; an unfilled exit is always chased
    best quote  the freshest of the recorded best bid/offer (bbo, from 28 Oct 2026), the 20-level book (every ~5.4 s)
                and the last print on that side (a taker sell prints at the bid, a taker buy at the ask)
    fees        TAKER_BPS a side for market orders, MAKER_BPS for limit orders (Hyperliquid's base tier)
    holds       from the entry fill, or to a fixed exit time (e.g. when a TWAP is seen to stop)

    STYLES      the five ways in and out that are compared for every idea
"""
import math
from dataclasses import dataclass

import numpy as np

from storage import tickStore

DAY_MS = 86_400_000
DELAY_MS = 500
TAKER_BPS = 4.5
MAKER_BPS = 1.5
WAIT_MS = 30_000
MAX_GAP_MS = 10_000           # a print more than this after it was needed means the feed was down: no price
QUOTE_STALE_MS = 30_000

STYLES = {                                  # (entry, exit)
    "market": ("market", "market"),
    "market in, limit out": ("market", "limit"),
    "limit in or skip, market out": ("limit-skip", "market"),
    "limit in or chase, market out": ("limit-chase", "market"),
    "limit both, skip": ("limit-skip", "limit"),
}


@dataclass(slots=True)
class Signal:
    coin: str
    t_ms: int                     # when the idea could first be known
    side: int                     # +1 long, -1 short
    hold_ms: int | None = None    # held this long from the entry fill
    exit_ms: int | None = None    # or closed at this time (if later than the entry)
    tag: str = ""


class Market:
    """One coin's trade prints and quotes over a time range."""

    def __init__(self, coin: str, start_ms: float, end_ms: float, store=tickStore.STORE_DIR):
        z = tickStore.load(coin, ["trades", "book", "bbo"], start_ms, end_ms, store,
                           only=["trade_px", "trade_side", "bid_px", "ask_px", "bbo_bid", "bbo_ask"])
        self.coin = coin
        t, px, side = z["trade_t"], z["trade_px"], z["trade_side"]
        self.buy_t, self.buy_px = t[side > 0], px[side > 0]          # taker buys: prints at the ask
        self.sell_t, self.sell_px = t[side < 0], px[side < 0]        # taker sells: prints at the bid
        qt = np.concatenate([z["book_t"], z["bbo_t"]])
        qb = np.concatenate([z["bid_px"][:, 0] if len(z["book_t"]) else np.zeros(0), z["bbo_bid"]])
        qa = np.concatenate([z["ask_px"][:, 0] if len(z["book_t"]) else np.zeros(0), z["bbo_ask"]])
        ok = np.isfinite(qb) & np.isfinite(qa) & (qa > qb) & (qb > 0)
        order = np.argsort(qt[ok], kind="stable")
        self.q_t, self.q_bid, self.q_ask = qt[ok][order], qb[ok][order], qa[ok][order]

    # ── prices ─────────────────────────────────────────────────────────────
    def market(self, buy: bool, t: float) -> tuple[float, float] | None:
        """(time, price) of a market order sent at t: the first print on that side from then on, or, when nobody
        trades that side within MAX_GAP_MS (a quiet coin), the ask (or bid) as known at t."""
        tt, pp = (self.buy_t, self.buy_px) if buy else (self.sell_t, self.sell_px)
        i = np.searchsorted(tt, t, side="left")
        if i < len(tt) and tt[i] - t <= MAX_GAP_MS:
            return float(tt[i]), float(pp[i])
        q = self.quote(t)
        return (float(t), q[1] if buy else q[0]) if q else None

    def quote(self, t: float) -> tuple[float, float] | None:
        """Best bid and ask as known at t (freshest of the recorded quotes and the last print on each side)."""
        i = np.searchsorted(self.q_t, t, side="right") - 1
        if i < 0 or t - self.q_t[i] > QUOTE_STALE_MS:
            return None
        bid, ask, qt = float(self.q_bid[i]), float(self.q_ask[i]), self.q_t[i]
        j = np.searchsorted(self.sell_t, t, side="right") - 1
        k = np.searchsorted(self.buy_t, t, side="right") - 1
        b2 = float(self.sell_px[j]) if j >= 0 and self.sell_t[j] > qt else bid
        a2 = float(self.buy_px[k]) if k >= 0 and self.buy_t[k] > qt else ask
        return (b2, a2) if a2 > b2 else (bid, ask)

    def limit(self, buy: bool, t: float, wait_ms: float = WAIT_MS) -> tuple[float, float] | None:
        """(time, price) a limit order posted at the touch at t fills, or None if nothing trades through it."""
        q = self.quote(t)
        if q is None:
            return None
        price = q[0] if buy else q[1]
        tt, pp = (self.sell_t, self.sell_px) if buy else (self.buy_t, self.buy_px)
        i0, i1 = np.searchsorted(tt, t, side="right"), np.searchsorted(tt, t + wait_ms, side="right")
        hit = pp[i0:i1] < price if buy else pp[i0:i1] > price
        if not hit.any():
            return None
        return float(tt[i0 + int(np.argmax(hit))]), price


# ── one trade ───────────────────────────────────────────────────────────────
def trade(m: Market, s: Signal, style: str, wait_ms: float = WAIT_MS, delay_ms: float = DELAY_MS) -> dict | None:
    """One idea traded one way: gross and net bps, or None if it couldn't be priced. A skipped entry returns
    {"missed": True}."""
    how_in, how_out = STYLES[style]
    long = s.side > 0
    t0 = s.t_ms + delay_ms
    if how_in == "market":
        e = m.market(long, t0)
        fee_in, maker_in = TAKER_BPS, False
    else:
        e = m.limit(long, t0, wait_ms)
        fee_in, maker_in = MAKER_BPS, True
        if e is None:
            if how_in == "limit-skip":
                return {"missed": True}
            e = m.market(long, t0 + wait_ms)
            fee_in, maker_in = TAKER_BPS, False
    if e is None:
        return None
    t_in, p_in = e
    t_out = max(t_in + (s.hold_ms or 0), s.exit_ms or 0) if s.exit_ms else t_in + (s.hold_ms or 0)
    if how_out == "market":
        x = m.market(not long, t_out)
        fee_out, maker_out = TAKER_BPS, False
    else:
        x = m.limit(not long, t_out, wait_ms)
        fee_out, maker_out = MAKER_BPS, True
        if x is None:
            x = m.market(not long, t_out + wait_ms)
            fee_out, maker_out = TAKER_BPS, False
    if x is None:
        return None
    gross = s.side * (x[1] / p_in - 1) * 1e4
    return {"missed": False, "t_ms": s.t_ms, "coin": s.coin, "side": s.side, "gross": gross,
            "fees": fee_in + fee_out, "net": gross - fee_in - fee_out, "maker_in": maker_in, "maker_out": maker_out,
            "held_s": (x[0] - t_in) / 1000}


def one_at_a_time(signals: list[Signal], delay_ms: float = DELAY_MS) -> list[Signal]:
    """At most one open trade per coin: a signal while the last one is still held is dropped (as the bot does)."""
    free: dict[str, float] = {}
    out = []
    for s in sorted(signals, key=lambda x: x.t_ms):
        if s.t_ms < free.get(s.coin, -math.inf):
            continue
        out.append(s)
        end = s.t_ms + delay_ms + (s.hold_ms or 0)
        free[s.coin] = max(end, s.exit_ms or 0)
    return out


# ── summaries ───────────────────────────────────────────────────────────────
def day_t(t_ms: np.ndarray, v: np.ndarray) -> float | None:
    """t of the mean with days as the independent blocks."""
    if len(v) < 3:
        return None
    _, inv = np.unique(t_ms // DAY_MS, return_inverse=True)
    per = np.bincount(inv, weights=v) / np.bincount(inv)
    if len(per) < 3 or per.std(ddof=1) == 0:
        return None
    return float(per.mean() / (per.std(ddof=1) / math.sqrt(len(per))))


def summary(rows: list[dict | None], days: float, notional: float) -> dict:
    """What a set of priced trades did: per trade, per day, and in dollars at `notional` per trade."""
    tried = [r for r in rows if r is not None]
    done = [r for r in tried if not r["missed"]]
    out = {"signals": len(rows), "trades": len(done), "fill": len(done) / len(tried) if tried else None,
           "gross": None, "net": None, "t": None, "win": None, "per_day": None, "usd_day": None,
           "first": None, "second": None}
    if not done:
        return out
    t = np.array([r["t_ms"] for r in done], dtype=np.int64)
    net = np.array([r["net"] for r in done])
    mid = np.median(t)
    out.update(gross=float(np.mean([r["gross"] for r in done])), net=float(net.mean()), t=day_t(t, net),
               win=float((net > 0).mean()), per_day=len(done) / days if days else None,
               usd_day=float(net.sum() * notional / 1e4 / days) if days else None,
               first=float(net[t <= mid].mean()), second=float(net[t > mid].mean()) if (t > mid).any() else None)
    return out

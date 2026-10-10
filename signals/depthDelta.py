"""
How did resting liquidity near the touch change over the window?

Asks thinning (offers pulled or eaten) removes the ceiling → bullish.
Bids thinning removes the floor → bearish. The score compares the two sides'
relative change, so a book that thins evenly scores ~0.
"""
from config import DEPTH_FULL_STRENGTH
from models.signalModel import Direction, SignalResult
from signals.base import WallStats, WindowSlice, clip, fmt_usd

NAME, LABEL = "depth_delta", "Order book"


def _chg(a: float, b: float) -> float:
    return 0.0 if a <= 0 else (b - a) / a


STACKING = 0.2      # a side whose depth grew this much is "stacking"


def _wall_note(w: WallStats | None, bid_chg: float, ask_chg: float) -> str:
    """Is the side that's stacking up backed by walls that have proven real, or ones that keep getting pulled?"""
    if w is None:
        return ""
    for side, chg in (("ask", ask_chg), ("bid", bid_chg)):
        if chg < STACKING:
            continue
        pulled, real = w.pulled_near[side], w.eaten[side] + w.held[side]
        noun = "sell" if side == "ask" else "buy"
        if pulled >= 2 and pulled > real:
            return (f" Careful: {pulled} of the last {pulled + real} big {noun} walls vanished as price got close "
                    f"(last 30 min), so these may not be real.")
        if real >= 2 and real >= pulled:
            return (f" Big {noun} walls have been real lately: {real} of {pulled + real} held or got traded into "
                    f"(last 30 min).")
    return ""


def depth_delta(s: WindowSlice) -> SignalResult:
    a, b = s.book_start, s.book_end
    if not a or not b or a.timestamp == b.timestamp:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "Not enough order book history yet.", stat="")

    bid_chg = _chg(a.bid_notional, b.bid_notional)
    ask_chg = _chg(a.ask_notional, b.ask_notional)
    score = clip((bid_chg - ask_chg) / DEPTH_FULL_STRENGTH)

    if score >= 0:
        asks_lead = -ask_chg >= bid_chg
        phrase = (f"sell orders thinning out ({ask_chg:+.0%})" if asks_lead
                  else f"buy orders piling up ({bid_chg:+.0%})")
        stat = f"asks {ask_chg:+.0%}" if asks_lead else f"bids {bid_chg:+.0%}"
    else:
        bids_lead = -bid_chg >= ask_chg
        phrase = (f"buy orders being pulled ({bid_chg:+.0%})" if bids_lead
                  else f"sell orders piling up ({ask_chg:+.0%})")
        stat = f"bids {bid_chg:+.0%}" if bids_lead else f"asks {ask_chg:+.0%}"

    lean = "buyers" if b.imbalance > 0 else "sellers"
    summary = (
        f"Sell orders close to the price {ask_chg:+.0%} ({fmt_usd(a.ask_notional)} → {fmt_usd(b.ask_notional)}), "
        f"buy orders {bid_chg:+.0%} ({fmt_usd(a.bid_notional)} → {fmt_usd(b.bid_notional)}). "
        f"The book now leans {abs(b.imbalance):.0%} toward {lean}."
    )
    summary += _wall_note(s.walls, bid_chg, ask_chg)
    return SignalResult(
        NAME, LABEL,
        score=score,
        strength=abs(score),
        direction=Direction.of(score, eps=0.05),
        summary=summary,
        phrase=phrase,
        stat=stat,
        metrics={
            "bid_change": bid_chg,
            "ask_change": ask_chg,
            "bid_notional": b.bid_notional,
            "ask_notional": b.ask_notional,
            "book_imbalance": b.imbalance,
            "spread_bps": b.spread_bps,
        },
    )

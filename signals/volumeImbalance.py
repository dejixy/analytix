"""
Who was the aggressor? Taker buy vs taker sell notional over the window.

Takers pay to cross the spread, so their imbalance is the most direct
measure of urgency. Scaled by activity: a 70/30 split on 3× normal volume
says more than the same split on a dead tape.
"""
from config import IMBALANCE_FULL_STRENGTH
from models.signalModel import Direction, SignalResult
from models.tradeModel import TradeSide
from signals.base import WindowSlice, clip, fmt_usd

NAME, LABEL = "volume_imbalance", "Order flow"


def volume_imbalance(s: WindowSlice) -> SignalResult:
    buy = sell = 0.0
    for t in s.trades:
        if t.side is TradeSide.BUY:
            buy += t.notional
        else:
            sell += t.notional
    total = buy + sell
    if total <= 0:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No trades in this window.", stat="—")

    imbalance = (buy - sell) / total
    elapsed = max(1.0, s.seconds * s.coverage)
    activity = (total / elapsed) / s.baseline.notional_per_s if s.baseline.notional_per_s > 0 else 1.0
    activity_weight = clip(0.5 + 0.5 * activity, 0.5, 1.0)
    strength = clip(abs(imbalance) / IMBALANCE_FULL_STRENGTH, 0.0, 1.0) * activity_weight

    buy_pct = buy / total * 100
    aggressor, pct = ("Buyers", buy_pct) if imbalance >= 0 else ("Sellers", 100 - buy_pct)
    phrase = f"aggressive {'buying' if imbalance >= 0 else 'selling'} ({pct:.0f}% of taker volume)"
    summary = (
        f"{aggressor} took {pct:.0f}% of {fmt_usd(total)} taker volume "
        f"({len(s.trades)} fills, {activity:.1f}× normal pace)."
    )
    return SignalResult(
        NAME, LABEL,
        score=imbalance,
        strength=strength,
        direction=Direction.of(imbalance, eps=0.02),
        summary=summary,
        phrase=phrase,
        stat=f"{pct:.0f}% {'buy' if imbalance >= 0 else 'sell'}",
        metrics={
            "buy_notional": buy,
            "sell_notional": sell,
            "imbalance": imbalance,
            "activity": activity,
            "fills": float(len(s.trades)),
        },
    )

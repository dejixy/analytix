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
    coverage_note = ""
    if s.flow is not None:                      # long window: pre-summed from minute bars
        buy, sell, fills = s.flow.buy, s.flow.sell, s.flow.fills
        elapsed = max(1.0, s.flow.seconds)
    else:
        buy = sell = 0.0
        for t in s.trades:
            if t.side is TradeSide.BUY:
                buy += t.notional
            else:
                sell += t.notional
        fills = len(s.trades)
        elapsed = max(1.0, s.seconds * s.coverage)
    total = buy + sell
    if total <= 0:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No trades in this window.", stat="")

    imbalance = (buy - sell) / total
    activity = (total / elapsed) / s.baseline.notional_per_s if s.baseline.notional_per_s > 0 else 1.0
    activity_weight = clip(0.5 + 0.5 * activity, 0.5, 1.0)
    strength = clip(abs(imbalance) / IMBALANCE_FULL_STRENGTH, 0.0, 1.0) * activity_weight
    if s.flow is not None and s.flow.coverage < 0.95:
        # Flow seen over a slice of the window says less about the whole window.
        strength *= clip(s.flow.coverage * 2, 0.2, 1.0)
        coverage_note = f" Live trade data covers {s.flow.coverage:.0%} of this window."

    buy_pct = buy / total * 100
    aggressor, pct = ("Buyers", buy_pct) if imbalance >= 0 else ("Sellers", 100 - buy_pct)
    phrase = f"heavy {'buying' if imbalance >= 0 else 'selling'} ({pct:.0f}% of market orders)"
    summary = (
        f"{aggressor} made up {pct:.0f}% of {fmt_usd(total)} in market orders "
        f"({fills:,} trades, {activity:.1f}× the usual pace).{coverage_note}"
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
            "fills": float(fills),
        },
    )

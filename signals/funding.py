"""
Positioning: funding rate and open interest.

Price alone can't say *who* moved it. Pairing the price move with the change
in open interest can:

                 OI up                 OI down
  price up       new longs opening     shorts covering (squeeze)
  price down     new shorts opening    longs closing / being flushed

Funding says which side is crowded: positive = longs pay shorts. A crowded
side that starts exiting is fuel for a sharp move.

This signal is descriptive — it explains the character of a move rather than
pushing against it — so its direction follows the price.
"""
import math

from config import CROWDED_FUNDING_APR, OI_REF_PCT_PER_SQRT_MIN
from models.signalModel import Direction, SignalResult
from signals.base import WindowSlice, clip

NAME, LABEL = "funding", "Funding & OI"

QUADRANTS = {
    (Direction.UP, True): "new longs opening",
    (Direction.UP, False): "shorts covering",
    (Direction.DOWN, True): "new shorts opening",
    (Direction.DOWN, False): "longs closing",
    (Direction.NEUTRAL, True): "positions building on both sides",
    (Direction.NEUTRAL, False): "positions unwinding",
}


def funding(s: WindowSlice) -> SignalResult:
    a, b = s.ctx_start, s.ctx_end
    if not b:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No funding / OI data yet.", stat="—")

    apr = b.funding_apr
    oi_chg = (b.open_interest / a.open_interest - 1) * 100 if a and a.open_interest else 0.0
    apr_chg = apr - a.funding_apr if a else 0.0
    price_dir = Direction.of(s.move_bps, eps=1.0)

    elapsed = max(1.0, s.seconds * s.coverage)
    ref = OI_REF_PCT_PER_SQRT_MIN * math.sqrt(elapsed / 60)
    strength = clip(abs(oi_chg) / ref, 0.0, 1.0) * 0.9

    quadrant = QUADRANTS[(price_dir, oi_chg >= 0)] if strength >= 0.2 else "open interest roughly flat"
    crowded = None
    if apr >= CROWDED_FUNDING_APR:
        crowded = "longs"
    elif apr <= -CROWDED_FUNDING_APR:
        crowded = "shorts"
    # A crowded side heading for the exit is extra fuel.
    if crowded and oi_chg < 0 and strength >= 0.2 and (
        (crowded == "longs" and price_dir is Direction.DOWN) or (crowded == "shorts" and price_dir is Direction.UP)
    ):
        strength = clip(strength + 0.1, 0.0, 1.0)
        quadrant = f"crowded {crowded} being flushed"

    payer = "longs pay shorts" if apr >= 0 else "shorts pay longs"
    verb = {"up": "rose", "down": "fell", "neutral": "held"}[price_dir.value]
    summary = (
        f"Open interest {oi_chg:+.2f}% while price {verb}: {quadrant}. "
        f"Funding {apr:+.1f}% APR ({payer}{', crowded' if crowded else ''}"
        f"{f', {apr_chg:+.1f} pts' if abs(apr_chg) >= 0.5 else ''})."
    )
    sign = {Direction.UP: 1.0, Direction.DOWN: -1.0, Direction.NEUTRAL: 0.0}[price_dir]
    return SignalResult(
        NAME, LABEL,
        score=sign * strength,
        strength=strength if price_dir is not Direction.NEUTRAL else strength * 0.5,
        direction=price_dir,
        summary=summary,
        phrase=f"{quadrant} (OI {oi_chg:+.2f}%)",
        stat=f"OI {oi_chg:+.2f}%",
        metrics={
            "oi_change_pct": oi_chg,
            "open_interest": b.open_interest,
            "open_interest_usd": b.open_interest_usd,
            "funding_apr": apr,
            "funding_apr_change": apr_chg,
            "funding_hourly": b.funding,
            "crowded": 1.0 if crowded == "longs" else -1.0 if crowded == "shorts" else 0.0,
            "premium_bps": b.premium_bps,
        },
    )

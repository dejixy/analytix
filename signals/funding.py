"""
Positioning: funding rate and open interest.

Price alone can't say *who* moved it. Pairing the price move with the change
in open interest can:

                 OI up                 OI down
  price up       new longs opening     shorts covering (squeeze)
  price down     new shorts opening    longs closing / being flushed

Funding says which side is crowded: positive = longs pay shorts. A crowded
side that starts exiting is fuel for a sharp move.

This signal is descriptive (it explains the character of a move rather than
pushing against it), so its direction follows the price.
"""
import math

from config import CROWDED_FUNDING_APR, OI_REF_PCT_PER_SQRT_MIN
from models.signalModel import Direction, SignalResult
from engine.positioning import percentile
from signals.base import WindowSlice, clip

NAME, LABEL = "funding", "Funding & open interest"

QUADRANTS = {
    (Direction.UP, True): "new longs opening",
    (Direction.UP, False): "shorts covering",
    (Direction.DOWN, True): "new shorts opening",
    (Direction.DOWN, False): "longs closing",
    (Direction.NEUTRAL, True): "positions building on both sides",
    (Direction.NEUTRAL, False): "positions unwinding",
}


TOP_SHARE = 0.9     # an OI move bigger than 90% of history gets flagged on the card


def _ordinal(p: float) -> str:
    n = min(99, max(1, round(p * 100)))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def funding(s: WindowSlice) -> SignalResult:
    a, b = s.ctx_start, s.ctx_end
    if not b:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No funding or open interest data yet.", stat="")

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
        quadrant = f"crowded {crowded} being forced out"

    funding_pct = percentile(s.funding_history, b.funding)
    oi_pct = percentile(s.oi_history, abs(oi_chg)) if a and a.open_interest else None

    payer = "longs pay shorts" if apr >= 0 else "shorts pay longs"
    verb = {"up": "rose", "down": "fell", "neutral": "held"}[price_dir.value]
    oi_rank = ("" if oi_pct is None else f", the biggest {s.label} change on record" if oi_pct >= 0.995
               else f", bigger than {oi_pct:.0%} of {s.label} changes on record")
    fund_rank = f", higher than {funding_pct:.0%} of the past week" if funding_pct is not None else ""
    was = apr - apr_chg
    fund_was = (f", from {abs(was):.1f}%{'' if was * apr >= 0 else ' the other way'} at the start"
                if abs(apr_chg) >= 0.5 else "")
    summary = (
        f"Open interest {oi_chg:+.2f}% while price {verb}: {quadrant}{oi_rank}. "
        f"Funding {abs(apr):.1f}% a year ({payer}{', crowded' if crowded else ''}{fund_was}{fund_rank})."
    )
    stat = f"open interest {oi_chg:+.2f}%"
    if oi_pct is not None and oi_pct >= TOP_SHARE:
        stat += f" · top {max(1, round((1 - oi_pct) * 100))}%"
    sign = {Direction.UP: 1.0, Direction.DOWN: -1.0, Direction.NEUTRAL: 0.0}[price_dir]
    return SignalResult(
        NAME, LABEL,
        score=sign * strength,
        strength=strength if price_dir is not Direction.NEUTRAL else strength * 0.5,
        direction=price_dir,
        summary=summary,
        phrase=f"{quadrant} (open interest {oi_chg:+.2f}%)",
        stat=stat,
        metrics={
            "oi_change_pct": oi_chg,
            "open_interest": b.open_interest,
            "open_interest_usd": b.open_interest_usd,
            "funding_apr": apr,
            "funding_apr_change": apr_chg,
            "funding_hourly": b.funding,
            "crowded": 1.0 if crowded == "longs" else -1.0 if crowded == "shorts" else 0.0,
            "premium_bps": b.premium_bps,
            "oi_pct": oi_pct,
            "funding_pct": funding_pct,
        },
    )

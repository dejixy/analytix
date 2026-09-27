"""
The thing to be explained: how far did price move, and is that unusual?

"Unusual" is relative. 10 bps in a minute is a big deal on a sleepy Sunday
and noise during a CPI print. So the move is scaled by the market's own
recent volatility: expected move ≈ σ₁ₛ × √seconds (random-walk scaling).
"""
import math

from config import MIN_NOTABLE_BPS_PER_SQRT_MIN, NOTABLE_Z, SIGNIFICANT_Z
from models.explanationModel import PriceMove, Significance
from models.signalModel import Direction
from signals.base import WindowSlice


def price_move(s: WindowSlice) -> PriceMove:
    start, end = s.price_start or 0.0, s.price_end or 0.0
    move = s.move_bps
    elapsed_s = 1.0
    if s.book_start and s.book_end:
        elapsed_s = max(1.0, (s.book_end.timestamp - s.book_start.timestamp) / 1000)
    expected = max(1e-9, s.baseline.sigma_1s_bps * math.sqrt(elapsed_s))
    z = move / expected
    floor_bps = MIN_NOTABLE_BPS_PER_SQRT_MIN * math.sqrt(elapsed_s / 60)

    if abs(move) < floor_bps or abs(z) < NOTABLE_Z:
        sig = Significance.QUIET
    elif abs(z) >= SIGNIFICANT_Z:
        sig = Significance.SIGNIFICANT
    else:
        sig = Significance.NOTABLE

    up = move >= 0
    return PriceMove(
        burst_bps=s.burst_up_bps if up else s.burst_down_bps,
        burst_end_ms=s.burst_up_end_ms if up else s.burst_down_end_ms,
        start_price=start,
        end_price=end,
        high=s.high if s.high is not None else max(start, end),
        low=s.low if s.low is not None else min(start, end),
        move_bps=move,
        z=z,
        expected_bps=expected,
        significance=sig,
        direction=Direction.of(move, eps=0.5),
    )

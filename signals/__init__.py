"""
Signal registry. To add a signal: write a function WindowSlice -> SignalResult
in its own module and append it to DRIVER_SIGNALS. The explainer ranks
whatever is registered here.
"""
from signals.depthDelta import depth_delta
from signals.funding import funding
from signals.liquidations import liquidations
from signals.priceMove import price_move
from signals.volumeImbalance import volume_imbalance

DRIVER_SIGNALS = [volume_imbalance, depth_delta, liquidations, funding]

# How much each signal's strength counts when ranking drivers, by horizon.
# Microstructure shocks (sweeps, pulled liquidity) explain seconds-to-minutes;
# positioning (OI, funding) explains the slower, larger moves. Keyed by horizon
# rather than window label so a new entry in WINDOWS still gets sensible weights.
HORIZON_WEIGHTS = {
    "short": {"liquidations": 1.25, "volume_imbalance": 1.0, "depth_delta": 0.9, "funding": 0.45},
    "medium": {"volume_imbalance": 1.0, "liquidations": 0.9, "funding": 0.85, "depth_delta": 0.65},
    "long": {"funding": 1.2, "volume_imbalance": 1.0, "liquidations": 0.6, "depth_delta": 0.45},
    "very_long": {"funding": 1.3, "volume_imbalance": 1.0, "liquidations": 0.4, "depth_delta": 0.3},
}


def horizon(seconds: int) -> str:
    if seconds <= 120:
        return "short"
    if seconds <= 600:
        return "medium"
    return "long" if seconds <= 3600 else "very_long"


def weights_for(seconds: int) -> dict[str, float]:
    return HORIZON_WEIGHTS[horizon(seconds)]


__all__ = ["DRIVER_SIGNALS", "HORIZON_WEIGHTS", "horizon", "price_move", "weights_for"]

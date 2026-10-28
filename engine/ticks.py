"""
Hyperliquid's price tick: perp prices allow up to 5 significant figures, and whole-number prices are always allowed.

    2,650.1  -> 0.1      104.37 -> 0.01      99.874 -> 0.001      110,000 -> 1 (not 10: integers are always valid)

So the tick changes when a price crosses a power of ten (SOL around 100). Very cheap coins are also capped by
6 - szDecimals decimal places, which this ignores; none of the studied coins get near it.
"""
import numpy as np


def hl_tick(price):
    """The tick at `price` (a number or an array)."""
    p = np.asarray(price, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        tick = np.minimum(10.0 ** (np.floor(np.log10(p)) - 4), 1.0)
    return float(tick) if tick.ndim == 0 else tick

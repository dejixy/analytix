"""
Shared inputs for signals.

Signals are plain functions: WindowSlice (+ Baseline) in, SignalResult out.
No state, no I/O, no clock — which makes each one trivially testable and means
the same function serves every timeframe.
"""
from dataclasses import dataclass

from models.bookModel import BookSummary
from models.contextModel import AssetContext
from models.orderModel import AggressiveOrder
from models.tradeModel import Trade


@dataclass(frozen=True, slots=True)
class Baseline:
    """What 'normal' looks like, computed once per tick over the whole buffer."""
    sigma_1s_bps: float          # typical 1-second mid move (std of 1s log returns)
    notional_per_s: float        # typical taker volume per second (USD)
    sweep_threshold: float       # USD size above which an aggressive order is 'large'
    covered_s: float             # seconds of history the baseline is built on


@dataclass(frozen=True, slots=True)
class WindowSlice:
    label: str                   # "10m"
    seconds: int
    start_ms: int
    end_ms: int
    trades: list[Trade]
    orders: list[AggressiveOrder]   # trades stitched into taker orders
    book_start: BookSummary | None
    book_end: BookSummary | None
    ctx_start: AssetContext | None
    ctx_end: AssetContext | None
    high: float | None
    low: float | None
    coverage: float              # 0..1 — how much of the window we have data for
    baseline: Baseline
    burst_up_bps: float = 0.0    # largest 60s rise inside the window
    burst_down_bps: float = 0.0  # largest 60s fall inside the window (negative)
    burst_up_end_ms: int = 0
    burst_down_end_ms: int = 0

    @property
    def price_start(self) -> float | None:
        return self.book_start.mid if self.book_start else None

    @property
    def price_end(self) -> float | None:
        return self.book_end.mid if self.book_end else None

    @property
    def move_bps(self) -> float:
        a, b = self.price_start, self.price_end
        if not a or not b:
            return 0.0
        return (b / a - 1) * 10_000


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def fmt_usd(x: float) -> str:
    a = abs(x)
    if a >= 1e9:
        s = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.2f}M"
    elif a >= 1e3:
        s = f"${a / 1e3:.0f}K"
    else:
        s = f"${a:.0f}"
    return "-" + s if x < 0 else s


def fmt_pct(x: float, digits: int = 1, sign: bool = True) -> str:
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"

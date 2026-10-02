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
from models.tradeModel import Trade, TradeSide


@dataclass(frozen=True, slots=True)
class Baseline:
    """What 'normal' looks like, computed once per tick over the whole buffer."""
    sigma_1s_bps: float          # typical 1-second mid move (std of 1s log returns)
    notional_per_s: float        # typical taker volume per second (USD)
    sweep_threshold: float       # USD size above which an aggressive order is 'large'
    covered_s: float             # seconds of history the baseline is built on


@dataclass(frozen=True, slots=True)
class FlowAgg:
    """Taker flow summed from minute bars (long windows only)."""
    buy: float
    sell: float
    fills: int
    seconds: float      # seconds of the window that live flow covers
    coverage: float     # seconds / window length


@dataclass(frozen=True, slots=True)
class SweepAgg:
    buy_n: int
    sell_n: int
    buy_notional: float
    sell_notional: float
    total_notional: float


@dataclass(frozen=True, slots=True)
class CascadeInfo:
    """What happened around a tracked cascade (engine/cascades.py): the OI check and the recovery since."""
    side: TradeSide
    start_ms: int
    end_ms: int
    oi_settled: bool                 # the OI check is done (a reading after the cascade, or given up)
    oi_change_usd: float | None      # None until settled, or if OI wasn't available
    confirm_share: float | None      # OI drop as a share of the cascade's notional
    verdict: str | None              # "likely" | "partly" | "unlikely" liquidations
    move_bps: float                  # price before → the cascade's extreme
    recovered: float | None          # share of that move price has won back since (can be < 0 or > 1)
    since_end_s: float


@dataclass(frozen=True, slots=True)
class WallStats:
    """How big resting orders near the price have behaved over the last 30 minutes (engine/walls.py)."""
    pulled_near: dict[str, int]      # side ("bid"/"ask") → walls that vanished, unfilled, as price came within 10 bps
    eaten: dict[str, int]            # → walls that were traded into
    held: dict[str, int]             # → walls standing now that have absorbed ≥ 20% of their size


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
    burst_up_bps: float = 0.0    # largest rise over any burst_span_s stretch inside the window
    burst_down_bps: float = 0.0  # largest fall over any burst_span_s stretch (negative)
    burst_up_end_ms: int = 0
    burst_down_end_ms: int = 0
    burst_span_s: int = 60       # 1 minute for tick windows, window/12 for long ones
    resolution: str = "tick"     # "tick" (raw trades/book) | "bar" (minute bars)
    flow: FlowAgg | None = None  # bar windows: pre-summed flow instead of trades
    sweep_agg: SweepAgg | None = None
    flow_coverage: float = 1.0   # share of the window with live flow/depth/OI data
    cascades: tuple[CascadeInfo, ...] = ()   # tracked cascades with their OI check and recovery
    walls: WallStats | None = None           # how recent big resting orders behaved: real or pulled
    funding_history: tuple[float, ...] = ()  # sorted hourly funding rates, past week (engine/positioning.py)
    oi_history: tuple[float, ...] = ()       # sorted |ΔOI %| over this timeframe, from saved live bars

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


def fmt_px(p: float) -> str:
    """A price at a sensible precision: 2,650.40 · 0.8123 · 0.000123."""
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:.4f}" if p >= 0.01 else f"{p:.6f}"

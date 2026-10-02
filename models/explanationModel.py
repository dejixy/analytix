from dataclasses import dataclass, field
from enum import Enum

from models.signalModel import Direction, SignalResult


class Significance(str, Enum):
    QUIET = "quiet"
    NOTABLE = "notable"
    SIGNIFICANT = "significant"


class Alignment(str, Enum):
    SUPPORTS = "supports"   # pushes the same way price moved
    OPPOSES = "opposes"     # pushed against the move (and lost)
    NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class Driver:
    name: str
    label: str
    alignment: Alignment
    strength: float
    share: float            # 0..1 share of the explanation among supporting drivers
    summary: str


@dataclass(frozen=True, slots=True)
class PriceMove:
    start_price: float
    end_price: float
    high: float
    low: float
    move_bps: float
    z: float                # move ÷ expected move for this window
    expected_bps: float
    significance: Significance
    direction: Direction
    burst_bps: float = 0.0  # biggest move over one burst span inside the window, in the move's direction
    burst_end_ms: int = 0
    burst_span_s: int = 60

    @property
    def burst_share(self) -> float:
        """How much of the net move one minute accounts for (1.0 = all of it)."""
        return self.burst_bps / self.move_bps if self.move_bps else 0.0

    @property
    def move_pct(self) -> float:
        return self.move_bps / 100


@dataclass(frozen=True, slots=True)
class FlowImpact:
    """How far price moved compared with what the window's net taker flow normally does (engine/impact.py)."""
    net_flow: float          # USD, taker buys − taker sells
    expected_bps: float      # the move that much net flow normally produces over this timeframe
    actual_bps: float
    ratio: float             # actual ÷ expected
    verdict: str             # "against" | "absorbed" | "normal" | "outsized"
    lam_bps_per_m: float     # normal impact: bps per $1M of net flow
    source: str              # "measured" | "scaled from 1m"


@dataclass(frozen=True, slots=True)
class DefendedLevel:
    """Where passive orders soaked up the aggression in an absorbed window (engine/levels.py)."""
    side: str                # "bid": held against sellers (support) · "ask": held against buyers (resistance)
    price: float             # notional-weighted price of the aggressive fills it absorbed
    absorbed: float          # USD of aggressive flow that hit it
    share: float             # that, as a share of all the aggressors' volume in the window


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """A discrete thing worth knowing about, for the event feed: a broken level, a cascade."""
    id: str
    kind: str                # "level_break" | "cascade"
    ts: int                  # exchange ms
    direction: Direction     # which way price went / was pushed
    title: str
    detail: str = ""         # a short note shown on the right
    window: str = ""


@dataclass(frozen=True, slots=True)
class Explanation:
    window: str
    seconds: int
    start_ms: int
    end_ms: int
    coverage: float         # fraction of the window we actually have data for
    move: PriceMove
    headline: str
    narrative: list[str]
    drivers: list[Driver]
    confidence: float
    signals: dict[str, SignalResult] = field(default_factory=dict)
    shape: str = ""         # "burst" | "grind" | "mixed" | "" — only for windows longer than a minute
    shape_label: str = ""   # human text for the shape, e.g. "one sharp 30-minute stretch"
    flow_coverage: float = 1.0
    impact: FlowImpact | None = None
    level: DefendedLevel | None = None


@dataclass(slots=True)
class MoveEvent:
    """A significant move, captured at its peak — the answer to 'what happened at 14:32?'"""
    id: int
    window: str
    started_ms: int
    peak_ms: int
    ended_ms: int | None
    explanation: Explanation

    @property
    def active(self) -> bool:
        return self.ended_ms is None

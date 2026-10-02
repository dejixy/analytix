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
    outlook: "Outlook | None" = None


@dataclass(frozen=True, slots=True)
class Outlook:
    """A lean for the next window-length of time — a heuristic estimate, scored against what happens."""
    horizon_s: int
    lean: Direction
    score: float            # -1..+1 combined evidence
    expected_bps: float     # centre of the estimate
    range_bps: float        # typical (1σ) move over the horizon
    p_up: float             # rough probability price is higher at the end of the horizon
    reasons: list[str]
    line: str
    hit_rate: float | None = None   # share of past directional leans that were right (hidden until enough periods)
    scored: int = 0                 # directional leans scored — overlapping samples, several per period
    periods: float = 0.0            # independent window-lengths those leans cover: the honest sample size
    range_rate: float | None = None # share of past outlooks whose outcome landed inside expected ± range
    track: str = ""                 # the track record in words, for the dashboard


@dataclass(frozen=True, slots=True)
class TrackRecord:
    """How past outlooks for one window turned out (see engine/outlookTracker.py)."""
    hit_rate: float | None = None   # share of directional calls that were right; None until enough periods
    called: int = 0                 # directional calls scored (overlapping samples)
    periods: float = 0.0            # independent horizons those calls cover
    range_rate: float | None = None # share of all samples that ended inside expected ± range

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

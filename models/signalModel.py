from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"

    @classmethod
    def of(cls, x: float, eps: float = 0.0) -> "Direction":
        if x > eps:
            return cls.UP
        if x < -eps:
            return cls.DOWN
        return cls.NEUTRAL


@dataclass(frozen=True, slots=True)
class SignalResult:
    """
    The common shape every signal returns, so the explainer can rank them
    without knowing how each one was computed.
    """
    name: str               # machine name, e.g. "volume_imbalance"
    label: str              # human label, e.g. "Order flow"
    score: float            # -1..+1, positive = pushes price up
    strength: float         # 0..1, how loud the signal is
    direction: Direction
    summary: str            # one full sentence for the narrative
    phrase: str = ""        # short clause for headlines: "driven by <phrase>"
    stat: str = ""          # the one number that matters, for compact displays: "84% sell"
    metrics: dict[str, float] = field(default_factory=dict)

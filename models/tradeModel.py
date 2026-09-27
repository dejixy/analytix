from dataclasses import dataclass
from enum import Enum


class TradeSide(str, Enum):
    """Aggressor side. Hyperliquid tags every trade with the *taker's* side."""
    BUY = "B"   # taker bought — lifted the ask
    SELL = "A"  # taker sold — hit the bid


@dataclass(frozen=True, slots=True)
class Trade:
    """One fill. Events are immutable facts, hence frozen."""
    timestamp: int          # ms since epoch, exchange clock
    price: float
    size: float             # in coins
    side: TradeSide
    tid: int
    hash: str = ""          # L1 tx hash — fills from the same taker order share it
    buyer: str | None = None
    seller: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.size

"""The best bid and ask for the same coin on another exchange (Binance, Bybit), on that exchange's clock."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExternalQuote:
    timestamp: int          # the other exchange's own time, ms
    venue: str              # "binance" | "bybit"
    coin: str               # Hyperliquid's name for it
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

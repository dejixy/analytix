from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AssetContext:
    """
    Perp context from Hyperliquid's `activeAssetCtx` channel.

    The feed carries no timestamp, so the parser leaves timestamp=0 and
    MarketState stamps it with the *exchange* clock on arrival, keeping every
    buffer on one clock.
    """
    timestamp: int
    funding: float              # current hourly funding rate, as a fraction (0.0000125 = 0.00125%/h)
    open_interest: float        # in coins
    mark_price: float
    oracle_price: float
    mid_price: float | None = None
    day_volume: float = 0.0     # USD notional, 24h
    prev_day_price: float = 0.0

    @property
    def funding_apr(self) -> float:
        """Annualised funding in percent. Positive = longs pay shorts."""
        return self.funding * 24 * 365 * 100

    @property
    def premium_bps(self) -> float:
        if not self.oracle_price:
            return 0.0
        return (self.mark_price - self.oracle_price) / self.oracle_price * 10_000

    @property
    def open_interest_usd(self) -> float:
        return self.open_interest * self.mark_price

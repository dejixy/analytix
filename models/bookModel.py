from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    size: float
    orders: int

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class BookSummary:
    """
    A compressed snapshot of the book at one moment.

    The OrderBook itself is *state* (we only keep the latest). But to answer
    "how did depth change over the last 5m?" we need history, so every book
    update also emits one of these small *events* into a RingBuffer.
    """
    timestamp: int
    mid: float
    best_bid: float
    best_ask: float
    spread_bps: float
    bid_notional: float     # USD resting within DEPTH_BAND_BPS below mid
    ask_notional: float     # USD resting within DEPTH_BAND_BPS above mid

    @property
    def imbalance(self) -> float:
        """+1 = all bids, -1 = all asks."""
        total = self.bid_notional + self.ask_notional
        return 0.0 if total == 0 else (self.bid_notional - self.ask_notional) / total


@dataclass(frozen=True, slots=True)
class OrderBook:
    timestamp: int
    bids: tuple[BookLevel, ...]   # best (highest) first
    asks: tuple[BookLevel, ...]   # best (lowest) first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread_bps(self) -> float | None:
        mid = self.mid
        if mid is None:
            return None
        return (self.asks[0].price - self.bids[0].price) / mid * 10_000

    def depth_notional(self, band_bps: float) -> tuple[float, float]:
        """USD resting on each side within band_bps of mid."""
        mid = self.mid
        if mid is None:
            return 0.0, 0.0
        lo = mid * (1 - band_bps / 10_000)
        hi = mid * (1 + band_bps / 10_000)
        bid = sum(l.notional for l in self.bids if l.price >= lo)
        ask = sum(l.notional for l in self.asks if l.price <= hi)
        return bid, ask

    def summarize(self, band_bps: float) -> BookSummary | None:
        mid = self.mid
        if mid is None:
            return None
        bid, ask = self.depth_notional(band_bps)
        return BookSummary(
            timestamp=self.timestamp,
            mid=mid,
            best_bid=self.bids[0].price,
            best_ask=self.asks[0].price,
            spread_bps=self.spread_bps or 0.0,
            bid_notional=bid,
            ask_notional=ask,
        )

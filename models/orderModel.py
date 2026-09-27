from dataclasses import dataclass
from typing import Iterable

from config import MIN_SWEEP_NOTIONAL, SWEEP_MIN_LEVELS
from models.tradeModel import Trade, TradeSide


@dataclass(frozen=True, slots=True)
class AggressiveOrder:
    """
    One taker order, reassembled from its fills.

    A market order that walks three price levels shows up on the feed as three
    trades. They share the L1 tx hash, side and timestamp, so we stitch them
    back together — order size and levels walked are what reveal a sweep.
    """
    timestamp: int
    side: TradeSide
    notional: float
    size: float
    fills: int
    levels: int
    first_price: float
    last_price: float
    confirmed_liquidation: bool = False

    @property
    def span_bps(self) -> float:
        if not self.first_price:
            return 0.0
        return abs(self.last_price / self.first_price - 1) * 10_000


def group_orders(trades: Iterable[Trade], liquidators: frozenset[str] | set[str] = frozenset()) -> list[AggressiveOrder]:
    groups: dict[tuple[str, TradeSide, int], list[Trade]] = {}
    for t in trades:
        groups.setdefault((t.hash or f"tid:{t.tid}", t.side, t.timestamp), []).append(t)

    orders: list[AggressiveOrder] = []
    for (_, side, ts), fills in groups.items():
        notional = size = 0.0
        prices = set()
        confirmed = False
        for f in fills:
            notional += f.price * f.size
            size += f.size
            prices.add(f.price)
            if liquidators and (f.buyer in liquidators or f.seller in liquidators):
                confirmed = True
        orders.append(AggressiveOrder(ts, side, notional, size, len(fills), len(prices),
                                      fills[0].price, fills[-1].price, confirmed))
    orders.sort(key=lambda o: o.timestamp)
    return orders


def is_sweep(o: AggressiveOrder, threshold: float) -> bool:
    """Large versus recent orders, or walking several levels with real size, or a known liquidation."""
    return (
        o.confirmed_liquidation
        or o.notional >= threshold
        or (o.levels >= SWEEP_MIN_LEVELS and o.notional >= MIN_SWEEP_NOTIONAL)
    )

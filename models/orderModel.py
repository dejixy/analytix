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
    taker: str | None = None        # the aggressor's wallet
    engine: bool = False            # no tx hash: executed by the engine itself — a TWAP slice, liquidation or ADL

    @property
    def span_bps(self) -> float:
        if not self.first_price:
            return 0.0
        return abs(self.last_price / self.first_price - 1) * 10_000


def group_orders(trades: Iterable[Trade], liquidators: frozenset[str] | set[str] = frozenset()) -> list[AggressiveOrder]:
    """Fills → taker orders. Normally one order = one L1 transaction hash. Engine-executed fills (TWAP
    slices, liquidations) have no hash (all zeros), so several of them in one block would collapse into one
    fake "big order" — those are grouped by the taker's wallet instead."""
    groups: dict[tuple[str, TradeSide, int], list[Trade]] = {}
    for t in trades:
        if t.zero_hash:
            key = f"engine:{t.taker or t.tid}"
        else:
            key = t.hash or f"tid:{t.tid}"
        groups.setdefault((key, t.side, t.timestamp), []).append(t)

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
                                      fills[0].price, fills[-1].price, confirmed,
                                      taker=fills[0].taker, engine=fills[0].zero_hash))
    orders.sort(key=lambda o: o.timestamp)
    return orders


def is_twap_slice(o: AggressiveOrder, twaps: dict | None) -> bool:
    """A slice of a running TWAP: an engine order from a (wallet, side) known to be TWAPing, and slice-sized —
    a much bigger engine order from the same wallet is something else (a liquidation) and isn't excused."""
    if not twaps or not o.engine or not o.taker:
        return False
    known = twaps.get((o.taker, o.side))
    return known is not None and o.notional <= 4.5 * known[1]    # ±20% randomised × 3× catch-up


def is_sweep(o: AggressiveOrder, threshold: float, twaps: dict | None = None) -> bool:
    """Large versus recent orders, or walking several levels with real size, or a known liquidation.
    TWAP slices are never sweeps: they're scheduled pieces of a planned order, not forced flow."""
    if is_twap_slice(o, twaps) and not o.confirmed_liquidation:
        return False
    return (
        o.confirmed_liquidation
        or o.notional >= threshold
        or (o.levels >= SWEEP_MIN_LEVELS and o.notional >= MIN_SWEEP_NOTIONAL)
    )

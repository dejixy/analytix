"""
MarketState — everything the engine knows about the market right now.

Event data (trades, book summaries, funding ticks) goes into RingBuffers.
State data (the order book, the latest context) is replaced on every update.

Derived events are built once, on arrival: each book update also yields a
BookSummary, and each batch of trades is stitched into AggressiveOrders. Signals
read those instead of re-deriving them for every window on every tick.
"""
from collections import deque
from dataclasses import replace
from typing import Iterable

from buffer.ringBuffer import RingBuffer
from config import DEPTH_BAND_BPS, HISTORY_SECONDS, LIQUIDATOR_ADDRESSES
from models.bookModel import BookSummary, OrderBook
from models.contextModel import AssetContext
from models.orderModel import AggressiveOrder, group_orders
from models.tradeModel import Trade

Event = Trade | OrderBook | AssetContext


class MarketState:
    def __init__(self, coin: str, history_seconds: int = HISTORY_SECONDS):
        self.coin = coin
        self.history_seconds = history_seconds
        self.trades: RingBuffer[Trade] = RingBuffer(max_seconds=history_seconds)
        self.orders: RingBuffer[AggressiveOrder] = RingBuffer(max_seconds=history_seconds)
        self.books: RingBuffer[BookSummary] = RingBuffer(max_seconds=history_seconds)
        self.contexts: RingBuffer[AssetContext] = RingBuffer(max_seconds=history_seconds)
        self.book: OrderBook | None = None
        self.context: AssetContext | None = None
        self.now_ms: int = 0            # the exchange clock: newest timestamp seen on any stream
        self.dropped: int = 0           # out-of-order or duplicate events rejected
        # On reconnect Hyperliquid can resend recent trades; remember recent ids to drop repeats.
        self._seen_tids: set[tuple[int, int]] = set()
        self._seen_order: deque[tuple[int, int]] = deque()

    # ── writes ──────────────────────────────────────────────────────────────
    def apply(self, event: Event) -> None:
        self.apply_many([event])

    def apply_many(self, events: Iterable[Event]) -> None:
        """Apply one message's worth of events. A taker order fills within one block,
        so all fills of an order arrive in the same message — group them here."""
        accepted: list[Trade] = []
        for e in events:
            if isinstance(e, Trade):
                if self._apply_trade(e):
                    accepted.append(e)
            elif isinstance(e, OrderBook):
                self._apply_book(e)
            elif isinstance(e, AssetContext):
                self._apply_context(e)
        if accepted:
            for order in group_orders(accepted, LIQUIDATOR_ADDRESSES):
                self.orders.push(order)

    def _advance(self, ts: int) -> None:
        if ts > self.now_ms:
            self.now_ms = ts

    def _apply_trade(self, t: Trade) -> bool:
        key = (t.timestamp, t.tid)
        if key in self._seen_tids or not self.trades.push(t):
            self.dropped += 1
            return False
        self._seen_tids.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 20_000:
            self._seen_tids.discard(self._seen_order.popleft())
        self._advance(t.timestamp)
        return True

    def _apply_book(self, b: OrderBook) -> None:
        if self.book and b.timestamp < self.book.timestamp:
            self.dropped += 1
            return
        self.book = b                                   # state: replace
        summary = b.summarize(DEPTH_BAND_BPS)
        if summary:
            self.books.push(summary)                    # event: buffer
        self._advance(b.timestamp)

    def _apply_context(self, c: AssetContext) -> None:
        # No timestamp on the wire — stamp it with the exchange clock.
        stamped = replace(c, timestamp=self.now_ms)
        self.context = stamped
        if self.now_ms:
            self.contexts.push(stamped)

    def reset(self) -> None:
        self.__init__(self.coin, self.history_seconds)

    # ── reads ───────────────────────────────────────────────────────────────
    @property
    def mid(self) -> float | None:
        return self.book.mid if self.book else None

    def mid_series(self, seconds: int, step_ms: int = 1000) -> list[tuple[int, float]]:
        """Mid price resampled to one point per step (last value in each bucket)."""
        points: dict[int, float] = {}
        for s in self.books.window(min(seconds, self.history_seconds), self.now_ms):
            points[s.timestamp // step_ms * step_ms] = s.mid
        return sorted(points.items())

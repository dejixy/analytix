"""
MarketState — everything the engine knows about the market right now.

Event data (trades, book summaries, funding ticks) goes into RingBuffers.
State data (the order book, the latest context) is replaced on every update.

Derived events are built once, on arrival: each book update also yields a
BookSummary, and each batch of trades is stitched into AggressiveOrders. Signals
read those instead of re-deriving them for every window on every tick.

A second, coarser tier rolls everything into one-minute Bars as it arrives.
Windows longer than an hour (6h … 1w) read those instead of raw ticks.
"""
from collections import deque
from dataclasses import replace
from typing import Callable, Iterable

from buffer.ringBuffer import RingBuffer
from config import (
    BAR_HISTORY_S,
    BAR_SECONDS,
    DEPTH_BAND_BPS,
    HISTORY_SECONDS,
    LIQUIDATOR_ADDRESSES,
    MIN_SWEEP_NOTIONAL,
)
from models.barModel import Bar, BarAccumulator
from models.bookModel import BookSummary, OrderBook
from models.contextModel import AssetContext
from models.orderModel import AggressiveOrder, group_orders, is_sweep
from models.tradeModel import Trade

Event = Trade | OrderBook | AssetContext


class MarketState:
    def __init__(self, coin: str, history_seconds: int = HISTORY_SECONDS,
                 on_bar: Callable[[str, Bar], None] | None = None):
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
        # minute-bar tier
        self.bars: RingBuffer[Bar] = RingBuffer(max_seconds=BAR_HISTORY_S)
        self.on_bar = on_bar                        # e.g. persist each closed bar
        self.sweep_threshold = MIN_SWEEP_NOTIONAL   # the analyzer keeps this in line with its baseline
        self._bar: BarAccumulator | None = None
        self._last_close: float | None = None

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
                self._bar_order(order)

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
        if summary:
            acc = self._bar_at(b.timestamp)
            if acc:
                acc.price(summary.mid)
                acc.bid, acc.ask = summary.bid_notional, summary.ask_notional

    def _apply_context(self, c: AssetContext) -> None:
        # No timestamp on the wire — stamp it with the exchange clock.
        stamped = replace(c, timestamp=self.now_ms)
        self.context = stamped
        if self.now_ms:
            self.contexts.push(stamped)
            acc = self._bar_at(self.now_ms)
            if acc:
                acc.oi, acc.funding, acc.mark = c.open_interest, c.funding, c.mark_price

    # ── minute bars ─────────────────────────────────────────────────────────
    def _bar_at(self, ts: int) -> BarAccumulator | None:
        """The accumulator for the minute containing ts; closes the previous bar when the minute rolls."""
        start = ts // (BAR_SECONDS * 1000) * (BAR_SECONDS * 1000)
        acc = self._bar
        if acc is not None and start > acc.start:
            self._close_bar()
            acc = None
        if acc is None:
            price = self.mid if self.mid is not None else self._last_close
            if price is None:
                return None
            acc = self._bar = BarAccumulator(start, price)
        return acc

    def _close_bar(self) -> None:
        acc, self._bar = self._bar, None
        if acc is None:
            return
        bar = acc.freeze(BAR_SECONDS)
        self._last_close = bar.close
        if self.bars.push(bar) and self.on_bar:
            self.on_bar(self.coin, bar)

    def _bar_order(self, o: AggressiveOrder) -> None:
        acc = self._bar_at(o.timestamp)
        if acc is None:
            return
        buy = o.side.value == "B"
        if buy:
            acc.buy += o.notional
        else:
            acc.sell += o.notional
        acc.fills += o.fills
        if is_sweep(o, self.sweep_threshold):
            if buy:
                acc.sw_b += 1
                acc.swn_b += o.notional
            else:
                acc.sw_s += 1
                acc.swn_s += o.notional

    def merge_bars(self, bars: list[Bar]) -> int:
        """Add history (from the database or candle backfill) without overwriting live bars."""
        covered: set[int] = set()
        for b in self.bars:
            covered.update(b.timestamp + k * 60_000 for k in range(max(1, b.span_s // 60)))
        now = self.now_ms or (max(b.end_ms for b in bars) if bars else 0)

        def overlaps(b: Bar) -> bool:
            minutes = [b.timestamp + k * 60_000 for k in range(max(1, b.span_s // 60))]
            return any(m in covered for m in minutes) or b.timestamp > now

        added = self.bars.merge(bars, overlaps)
        newest = self.bars.newest
        if newest and self._last_close is None:
            self._last_close = newest.close
        return added

    def reset(self) -> None:
        self.__init__(self.coin, self.history_seconds, self.on_bar)

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

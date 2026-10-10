"""
Time-evicting buffer for event data (trades, book summaries, funding ticks).

Despite the name, this is not capacity-bounded like a classic ring buffer: it
holds everything newer than `max_seconds`, measured on the *exchange* clock.

Three decisions worth being able to defend:

1. `T` is bound to `Timestamped`, a Protocol. Anything with a `timestamp`
   qualifies: no inheritance needed (structural typing). `RingBuffer[int]`
   is now a type error instead of a runtime crash in `_evict()`.

2. "Now" is the newest item's timestamp, never `time.time()`. Every window is
   measured on one clock, so laptop clock skew can't distort windows, and a
   recorded session replays to identical results. Detecting a dead feed is
   ingestion's job (it owns the wall clock), not the buffer's.

3. Items must arrive in timestamp order. That's what lets `_evict()` pop from
   the left and `window()` stop early from the right. `push()` enforces it by
   rejecting stragglers instead of silently corrupting the ordering.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Generic, Iterator, Protocol, TypeVar

from config import HISTORY_SECONDS, WINDOWS


class Timestamped(Protocol):
    # Declared as a read-only property so frozen dataclasses satisfy it.
    @property
    def timestamp(self) -> int: ...  # ms since epoch, exchange clock


T = TypeVar("T", bound=Timestamped)


@dataclass
class RingBuffer(Generic[T]):
    max_seconds: int = HISTORY_SECONDS
    _buf: Deque[T] = field(default_factory=deque)

    # ── writes ──────────────────────────────────────────────────────────────
    def push(self, item: T) -> bool:
        """Append an item. Returns False (and drops it) if it's out of order."""
        if self._buf and item.timestamp < self._buf[-1].timestamp:
            return False
        self._buf.append(item)
        self._evict()
        return True

    def _evict(self) -> None:
        cutoff_ms = self._buf[-1].timestamp - self.max_seconds * 1000
        while self._buf and self._buf[0].timestamp < cutoff_ms:
            self._buf.popleft()

    def clear(self) -> None:
        self._buf.clear()

    def merge(self, items: list[T], overlaps=None) -> int:
        """
        Merge older or gap-filling items into the buffer (e.g. backfilled history
        that arrives after live data has started). Items for which
        `overlaps(item)` is true are skipped. Rebuilds the deque in time order,
        so it's O(n), meant for occasional backfills, not the hot path.
        """
        keep = [i for i in items if overlaps is None or not overlaps(i)]
        if not keep:
            return 0
        merged = sorted([*self._buf, *keep], key=lambda i: i.timestamp)
        self._buf = deque(merged)
        self._evict()
        return len(keep)

    # ── reads ───────────────────────────────────────────────────────────────
    @property
    def newest(self) -> T | None:
        return self._buf[-1] if self._buf else None

    @property
    def oldest(self) -> T | None:
        return self._buf[0] if self._buf else None

    def window(self, seconds: int, now_ms: int | None = None) -> list[T]:
        """
        Items in (now - seconds, now], oldest first.

        `now_ms` defaults to this buffer's newest item. MarketState passes its
        shared exchange clock instead, so a quiet buffer (no book update for 2s)
        is still measured against the same "now" as the busy trade buffer.

        Walks from the newest end and stops at the first item older than the
        cutoff, so a 1m query on a 60m buffer touches ~1/60th of it.
        """
        if seconds > self.max_seconds:
            raise ValueError(f"Buffer only holds {self.max_seconds}s, asked for {seconds}s")
        if not self._buf:
            return []
        now = self._buf[-1].timestamp if now_ms is None else now_ms
        cutoff_ms = now - seconds * 1000
        out: list[T] = []
        for item in reversed(self._buf):
            if item.timestamp <= cutoff_ms:
                break
            if item.timestamp <= now:
                out.append(item)
        out.reverse()
        return out

    def window_named(self, name: str, now_ms: int | None = None) -> list[T]:
        if name not in WINDOWS:
            raise ValueError(f"Unknown window '{name}'. Choose from {list(WINDOWS)}")
        return self.window(WINDOWS[name], now_ms)

    def latest_at(self, ts_ms: int) -> T | None:
        """The newest item at or before ts_ms, i.e. the state 'as of' that moment."""
        for item in reversed(self._buf):
            if item.timestamp <= ts_ms:
                return item
        return None

    def covered_seconds(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        return (self._buf[-1].timestamp - self._buf[0].timestamp) / 1000

    def __iter__(self) -> Iterator[T]:
        return iter(self._buf)

    def __reversed__(self) -> Iterator[T]:
        return reversed(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

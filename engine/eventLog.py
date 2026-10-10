"""
Remembers significant moves so you can ask "what happened at 14:32?" after
the 15-minute buffer has moved on.

A move becomes an *episode*:
  • opens when a window turns SIGNIFICANT,
  • stays open while the move is at least NOTABLE (hysteresis: a move hovering
    around the threshold doesn't flicker into five separate events),
  • keeps the explanation from its peak (largest |z|),
  • closes when the window goes quiet or the move reverses,
  • re-opens instead of duplicating if the same move re-ignites within half a
    window of closing.
"""
from collections import deque

from config import EVENT_LOG_SIZE
from models.explanationModel import Explanation, MoveEvent, Significance
from models.signalModel import Direction


class EventLog:
    def __init__(self, maxlen: int = EVENT_LOG_SIZE):
        self._events: deque[MoveEvent] = deque(maxlen=maxlen)
        self._active: dict[str, MoveEvent] = {}
        self._last_closed: dict[str, MoveEvent] = {}
        self._next_id = 1

    def update(self, latest: dict[str, Explanation], now_ms: int) -> None:
        for window, exp in latest.items():
            move = exp.move
            active = self._active.get(window)
            if active:
                reversed_ = move.direction is not Direction.NEUTRAL and move.direction is not active.explanation.move.direction
                if move.significance is Significance.QUIET or reversed_:
                    self._close(window, active, now_ms)
                else:
                    self._maybe_peak(active, exp, now_ms)
                    continue
            if move.significance is not Significance.SIGNIFICANT:
                continue
            last = self._last_closed.get(window)
            if (last and last.ended_ms is not None
                    and last.explanation.move.direction is move.direction
                    and now_ms - last.ended_ms <= exp.seconds * 500):
                last.ended_ms = None                    # same move re-igniting
                self._active[window] = last
                self._maybe_peak(last, exp, now_ms)
                continue
            ev = MoveEvent(self._next_id, window, now_ms, now_ms, None, exp)
            self._next_id += 1
            self._events.append(ev)
            self._active[window] = ev

    @staticmethod
    def _maybe_peak(ev: MoveEvent, exp: Explanation, now_ms: int) -> None:
        if abs(exp.move.z) >= abs(ev.explanation.move.z):
            ev.explanation = exp
            ev.peak_ms = now_ms

    def _close(self, window: str, ev: MoveEvent, now_ms: int) -> None:
        ev.ended_ms = now_ms
        del self._active[window]
        self._last_closed[window] = ev

    def recent(self, n: int = 50, window: str | None = None) -> list[MoveEvent]:
        evs = [e for e in self._events if window is None or e.window == window]
        evs.sort(key=lambda e: e.peak_ms, reverse=True)   # newest peak first
        return evs[:n]

    def __len__(self) -> int:
        return len(self._events)

    def reset(self) -> None:
        self.__init__(self._events.maxlen or EVENT_LOG_SIZE)

"""
How busy the server is, in plain numbers, for the 5-minute log line and /api/health.

Everything (the feeds, the engine, the scanner, pushing updates to browsers) shares one thread, so the number
that matters most is the *screen delay*: how late a task that should run every 250ms actually runs. If it's
small, nothing is waiting; if it grows, the other numbers say which part is holding things up.
"""
import asyncio
import logging
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field

log = logging.getLogger("analytix.speed")

TICK_S = 0.25
FREEZE_S = 1.0                    # the main thread not ticking for this long counts as a freeze


@dataclass(slots=True)
class Timer:
    """Running average and worst case of one kind of work, reset after each report."""
    n: int = 0
    total_ms: float = 0.0
    worst_ms: float = 0.0

    def add(self, ms: float) -> None:
        self.n += 1
        self.total_ms += ms
        self.worst_ms = max(self.worst_ms, ms)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.n if self.n else 0.0

    def reset(self) -> None:
        self.n, self.total_ms, self.worst_ms = 0, 0.0, 0.0


@dataclass(slots=True)
class Speed:
    delay: Timer = field(default_factory=Timer)      # how late the 250ms tick ran
    engine: Timer = field(default_factory=Timer)     # one engine run, one coin
    scanner: Timer = field(default_factory=Timer)    # one scanner poll (all coins)
    push: Timer = field(default_factory=Timer)       # building and sending one round of browser updates

    last_tick: float = 0.0
    freezes: int = 0
    stopped: bool = False

    async def watch(self) -> None:
        self.last_tick, self.stopped = time.perf_counter(), False
        FreezeCatcher(self, threading.get_ident()).start()
        try:
            while True:
                t = time.perf_counter()
                await asyncio.sleep(TICK_S)
                now = time.perf_counter()
                self.last_tick = now
                self.delay.add(max(0.0, (now - t - TICK_S) * 1000))
        finally:
            self.stopped = True

    def report(self, reset: bool = True) -> dict:
        out = {name: {"avg_ms": round(t.avg_ms, 1), "worst_ms": round(t.worst_ms, 1), "count": t.n}
               for name, t in (("screen_delay", self.delay), ("engine_run", self.engine),
                               ("scanner_poll", self.scanner), ("browser_push", self.push))}
        out["freezes_since_start"] = self.freezes
        if reset:
            for t in (self.delay, self.engine, self.scanner, self.push):
                t.reset()
        return out

    def line(self) -> str:
        r = self.report()
        d, e, s, p = r["screen_delay"], r["engine_run"], r["scanner_poll"], r["browser_push"]
        parts = [f"screen delay avg {d['avg_ms']:.0f}ms, worst {d['worst_ms']:.0f}ms",
                 f"engine {e['avg_ms']:.1f}ms a run (worst {e['worst_ms']:.0f}ms)"]
        if s["count"]:
            parts.append(f"scanner {s['avg_ms']:.0f}ms a poll (worst {s['worst_ms']:.0f}ms)")
        if p["count"]:
            parts.append(f"browser push {p['avg_ms']:.0f}ms (worst {p['worst_ms']:.0f}ms)")
        if self.freezes:
            parts.append(f"{self.freezes} freeze{'s' if self.freezes != 1 else ''} over 1s so far (FREEZE lines "
                         f"in the log show where)")
        return " · ".join(parts)


class FreezeCatcher(threading.Thread):
    """Watches the main thread from the side. When it hasn't ticked for FREEZE_S, logs where it is stuck: the
    exact lines of code running at that moment (once per freeze), so a stall can be fixed at its source."""

    def __init__(self, speed: Speed, main_id: int):
        super().__init__(name="freeze-catcher", daemon=True)
        self.speed, self.main_id = speed, main_id

    def run(self) -> None:
        reported_for = None
        while not self.speed.stopped:
            time.sleep(TICK_S)
            stuck_s = time.perf_counter() - self.speed.last_tick
            if stuck_s < FREEZE_S or reported_for == self.speed.last_tick:   # fine, or this freeze is logged
                continue
            reported_for = self.speed.last_tick
            frame = sys._current_frames().get(self.main_id)
            if frame is None:
                continue
            self.speed.freezes += 1
            stack = "".join(traceback.format_stack(frame, limit=14))      # the innermost 14 calls
            others = []                                               # a busy worker thread can hold Python's lock
            for t in threading.enumerate():
                f = sys._current_frames().get(t.ident)
                if t.ident in (self.main_id, self.ident) or f is None:
                    continue
                top = traceback.extract_stack(f, limit=2)
                others.append(f"  {t.name}: " + " <- ".join(f"{fr.name} ({fr.filename.rsplit('/', 1)[-1]}:{fr.lineno})"
                                                           for fr in reversed(top)))
            log.warning("FREEZE: the server has been stuck for %.1fs. It is running:\n%s%s", stuck_s, stack,
                        ("Other threads:\n" + "\n".join(others)) if others else "")

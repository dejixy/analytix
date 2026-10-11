"""
How busy the server is, in plain numbers, for the 5-minute log line and /api/health.

Everything (the feeds, the engine, the scanner, pushing updates to browsers) shares one thread, so the number
that matters most is the *screen delay*: how late a task that should run every 250ms actually runs. If it's
small, nothing is waiting; if it grows, the other numbers say which part is holding things up.
"""
import asyncio
import time
from dataclasses import dataclass, field

TICK_S = 0.25


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

    async def watch(self) -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(TICK_S)
            self.delay.add(max(0.0, (time.perf_counter() - t - TICK_S) * 1000))

    def report(self, reset: bool = True) -> dict:
        out = {name: {"avg_ms": round(t.avg_ms, 1), "worst_ms": round(t.worst_ms, 1), "count": t.n}
               for name, t in (("screen_delay", self.delay), ("engine_run", self.engine),
                               ("scanner_poll", self.scanner), ("browser_push", self.push))}
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
        return " · ".join(parts)

"""
Loud-market gauge, live: how big the next 30 minutes are likely to be for each coin (not which way), and whether
that's quiet, middle or loud for that coin. It's the move-size forecast (engine/volforecast.py) that beat "same as
the last 30 minutes" on every coin out of sample, refitted every minute on this server's own minute bars.

    data      the live minute bars (close and fill count), seeded from the bar history on disk (BAR_KEEP_DAYS) and
              topped up from each coin's live bars
    warm-up   the forecast reads the last 24 hours and fits on at least two days after that, so it shows nothing
              until about three days of bars exist (and gets steadier as the week of bars on disk fills up)
    fit       every REFRESH_S, in a worker thread: least squares on the minutes whose next 30 minutes are already
              known; the minute that just ended is then forecast. Quiet / middle / loud are the fitted minutes'
              thirds for that coin
    used by   the dashboard header ("Next 30 min") and Degen, which trades smaller in loud windows
"""
import asyncio
import logging
import time

from config import BAR_KEEP_DAYS
from engine import volforecast

log = logging.getLogger("analytix.regime")

REFRESH_S = 60
MIN_ROWS = 2 * 1440             # fitted minutes needed: two days after the 24-hour warm-up
DAY_MS = 86_400_000


class RegimeService:
    def __init__(self, runtime, keep_days: float = BAR_KEEP_DAYS):
        self.rt = runtime
        self.keep_ms = int(keep_days * DAY_MS)
        self.bars: dict[str, dict[int, object]] = {}        # coin -> bar open ms -> Bar
        self.now: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.rt.mode == "live":
            self._task = asyncio.create_task(self._loop(), name="regime")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _seed(self) -> None:
        since = int(time.time() * 1000) - self.keep_ms
        store = getattr(self.rt, "store", None)
        for coin in self.rt.coins:
            have = self.bars.setdefault(coin, {})
            if store is not None:
                try:
                    for b in store.load(coin, since):
                        have[b.timestamp] = b
                except Exception:
                    log.exception("couldn't read saved bars for %s", coin)

    def _merge(self, coin: str) -> None:
        have = self.bars.setdefault(coin, {})
        pipe = self.rt.pipelines.get(coin)
        if pipe is not None:
            for b in pipe.state.bars:
                have[b.timestamp] = b
        cut = int(time.time() * 1000) - self.keep_ms
        for t in [t for t in have if t < cut]:
            del have[t]

    async def refresh(self) -> None:
        now = int(time.time() * 1000)
        for coin in self.rt.coins:
            self._merge(coin)
            g = volforecast.minutes_from_bars(sorted(self.bars[coin].values(), key=lambda b: b.timestamp))
            fc = await asyncio.to_thread(volforecast.fit_minutes, coin, g, now, MIN_ROWS) if g else None
            if fc is None:
                self.now[coin] = {"regime": None, "move_bps": None, "minutes": len(self.bars[coin]), "as_of": now}
                continue
            move, reg = fc.at(now)
            self.now[coin] = {"regime": reg, "move_bps": None if move != move else round(move, 1),
                              "cuts": [round(c, 1) for c in fc.cuts], "minutes": len(self.bars[coin]), "as_of": now}

    async def _loop(self) -> None:
        self._seed()
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loud-market gauge refresh failed")
            await asyncio.sleep(REFRESH_S)

    def of(self, coin: str) -> dict:
        """{"regime": quiet|middle|loud|None, "move_bps": the expected 30-minute move, ...}"""
        return self.now.get(coin) or {"regime": None, "move_bps": None}

    def public(self) -> dict:
        return {c: self.of(c) for c in self.rt.coins}

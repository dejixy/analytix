"""
Long-tail recorder: the market data of mid-volume Hyperliquid perps, recorded for studies and shown nowhere. Thin
books, bigger moves and fewer fast competitors are where small size might still have an edge; testing that needs
a week or two of their data first.

    which coins   by 24h volume from the scanner's market poll (all perps, every 10 s): the main coins and the
                  LONGTAIL_SKIP busiest others are left out, then the next LONGTAIL_N with at least
                  LONGTAIL_MIN_USD a day (or LONGTAIL_COINS, by hand)
    re-picked     once a day; a coin already recorded stays while it's still in the top LONGTAIL_N + 10, so
                  each coin's record is as continuous as possible
    what          trades, the 20-level book, best bid and offer, and the coin's context (funding, open interest),
                  on its own socket, to hourly files in LONGTAIL_DIR (prefix "longtail"), separate from the main
                  recordings so their replays stay in time order
    store         python -m scripts.buildStore <LONGTAIL_DIR> adds them to the fast store like any other coin

Live mode, and only while the main feed is recorded.
"""
import asyncio
import logging
import time

from config import (LONGTAIL, LONGTAIL_COINS, LONGTAIL_DIR, LONGTAIL_MIN_USD, LONGTAIL_N, LONGTAIL_SKIP,
                    RECORD_MIN_FREE_GB)
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import subscriptions
from ingestion.recorder import HourlyRecorder

log = logging.getLogger("analytix.longtail")

REPICK_S = 24 * 3600
WAIT_S = 10
KEEP_MARGIN = 10


def pick(volumes: dict[str, float], exclude: set[str], current: list[str], n: int, skip: int,
         min_usd: float) -> list[str]:
    """The coins to record: current ones still near the top stay, then the busiest new ones fill up to n."""
    ranked = [c for c, v in sorted(volumes.items(), key=lambda kv: -kv[1]) if c not in exclude and v >= min_usd]
    pool = ranked[skip:]
    keep = [c for c in current if c in pool[:n + KEEP_MARGIN]][:n]
    for c in pool:
        if len(keep) >= n:
            break
        if c not in keep:
            keep.append(c)
    return keep


class LongTailRecorder:
    def __init__(self, runtime, folder=LONGTAIL_DIR, enabled: bool = LONGTAIL):
        self.rt = runtime
        self.folder = folder
        self.on = enabled
        self.coins: list[str] = []
        self.status = FeedStatus(mode="live")
        self.client: HyperliquidClient | None = None
        self.recorder: HourlyRecorder | None = None
        self.picked_at = 0.0
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.on and self.rt.mode == "live" and bool(getattr(self.rt, "record", False))

    async def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._run(), name="longtail")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.recorder:
            self.recorder.close()

    def _volumes(self) -> dict[str, float]:
        ctx = getattr(getattr(self.rt, "scanner", None), "contexts", None) or {}
        return {c: float(v.get("day_volume") or 0.0) for c, v in ctx.items()}

    def repick(self) -> list[str]:
        if LONGTAIL_COINS:
            return list(LONGTAIL_COINS)
        return pick(self._volumes(), set(self.rt.coins), self.coins, LONGTAIL_N, LONGTAIL_SKIP, LONGTAIL_MIN_USD)

    async def _run(self) -> None:
        while not LONGTAIL_COINS and not self._volumes():          # the scanner's first market poll
            await asyncio.sleep(WAIT_S)
        self.coins = self.repick()
        self.picked_at = time.time()
        if not self.coins:
            log.warning("long-tail recorder: no coins match, nothing recorded")
            return
        self.recorder = HourlyRecorder(self.folder, "longtail", min_free_gb=RECORD_MIN_FREE_GB)
        self.client = HyperliquidClient(self.coins, lambda msg: None, self.status, recorder=self.recorder)
        log.info("long-tail recorder: %d coins (%s) to %s", len(self.coins), ", ".join(self.coins), self.folder)
        feed = asyncio.create_task(self.client.run(), name="longtail-feed")
        try:
            while True:
                await asyncio.sleep(REPICK_S)
                new = self.repick()
                if new and new != self.coins:
                    gone, added = set(self.coins) - set(new), [c for c in new if c not in self.coins]
                    self.coins = new
                    self.client.set_subscriptions([s for c in new for s in subscriptions(c)])
                    log.info("long-tail recorder: +%s −%s", ",".join(added) or "none", ",".join(sorted(gone)) or "none")
                self.picked_at = time.time()
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    def summary(self) -> str:
        if not self.client:
            return ""
        return (f"long tail: {len(self.coins)} coins, feed {'up' if self.status.connected else 'down'} "
                f"({self.status.messages:,} messages)")

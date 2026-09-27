"""
Replays a recorded JSONL session through the same pipeline as the live feed.

Pacing uses the recorded receive times divided by `speed`. speed=inf replays
as fast as possible (used by tests and scripts/replayReport.py).
"""
import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Iterator

from ingestion.feedStatus import FeedStatus

log = logging.getLogger("analytix.replay")


def read_session(path: str | Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            yield int(row["recv_ms"]), row["msg"]


class ReplaySource:
    def __init__(
        self,
        path: str | Path,
        on_message: Callable[[dict[str, Any]], None],
        status: FeedStatus,
        speed: float = 4.0,
        loop: bool = True,
        on_reset: Callable[[], None] | None = None,
    ):
        self.path = Path(path)
        self.on_message = on_message
        self.status = status
        self.speed = speed
        self.loop = loop
        self.on_reset = on_reset
        self.status.source = str(self.path)

    async def run(self) -> None:
        self.status.connected = True
        while True:
            await self._play_once()
            if not self.loop:
                self.status.finished = True
                log.info("replay finished")
                return
            self.status.loops += 1
            # Exchange time would jump backwards on a loop — start from a clean state.
            if self.on_reset:
                self.on_reset()
            await asyncio.sleep(1.0)

    async def _play_once(self) -> None:
        first_recv: int | None = None
        loop = asyncio.get_running_loop()
        wall_start = loop.time()
        n = 0
        for recv_ms, msg in read_session(self.path):
            if first_recv is None:
                first_recv = recv_ms
            if not math.isinf(self.speed):
                due = (recv_ms - first_recv) / 1000 / self.speed
                delay = due - (loop.time() - wall_start)
                if delay > 0:
                    await asyncio.sleep(delay)
            self.status.touch()
            try:
                self.on_message(msg)
            except Exception:
                log.exception("failed to process replayed message")
            n += 1
            if math.isinf(self.speed) and n % 500 == 0:
                await asyncio.sleep(0)  # let the event loop breathe


def replay_sync(path: str | Path, on_message: Callable[[dict[str, Any]], None]) -> int:
    """Blocking, as-fast-as-possible replay. Returns the number of messages."""
    n = 0
    for _, msg in read_session(path):
        on_message(msg)
        n += 1
    return n

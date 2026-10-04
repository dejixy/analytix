"""
Replays a recorded JSONL session through the same pipeline as the live feed.

Pacing uses the recorded receive times divided by `speed`. speed=inf replays
as fast as possible (used by tests and scripts/replayReport.py).
"""
import asyncio
import gzip
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ingestion.feedStatus import FeedStatus
from ingestion.parsers import coin_of

log = logging.getLogger("analytix.replay")


STITCH_GAP_MS = 600_000      # recordings closer than this are one continuous session


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def read_session(path: str | Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """(recv_ms, raw message) for each line of a .jsonl or .jsonl.gz recording.

    A line cut off by a crash or power loss (always the last one written) is skipped, not fatal."""
    path = Path(path)
    try:
        with _open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    log.warning("skipping a damaged line in %s", path.name)
                    continue
                yield int(row["recv_ms"]), row["msg"]
    except (EOFError, gzip.BadGzipFile):
        log.warning("%s ends early (cut off mid-write); using what's there", path.name)


def coins_in(path: str | Path, max_lines: int = 20_000) -> list[str]:
    """Coins present in a recording, in order of first appearance."""
    path = Path(path)
    if not path.exists():
        return []
    seen: dict[str, None] = {}
    for i, (_, msg) in enumerate(read_session(path)):
        if i >= max_lines:
            break
        coin = coin_of(msg) if isinstance(msg, dict) else None
        if coin:
            seen.setdefault(coin)
    return list(seen)


def recording_files(paths: Iterable[str | Path]) -> list[Path]:
    """Expand folders to the recordings inside them (.jsonl and .jsonl.gz), oldest first."""
    out: dict[Path, None] = {}
    for p in map(Path, paths):
        if p.is_dir():
            for f in sorted([*p.glob("*.jsonl"), *p.glob("*.jsonl.gz")]):
                out.setdefault(f)
        elif p.exists():
            out.setdefault(p)
    return sorted(out, key=lambda f: (_first_recv(f) or 0, f.name))


def _first_recv(path: Path) -> int | None:
    return next((recv for recv, _ in read_session(path)), None)


def read_recordings(paths: Iterable[str | Path], max_gap_ms: int = STITCH_GAP_MS
                    ) -> Iterator[tuple[bool, int, dict[str, Any]]]:
    """(starts_new_session, recv_ms, msg) across many recordings in time order.

    Hourly files from one recorder run join into one continuous session, so the 60m window
    doesn't restart its warm-up every hour. A silence longer than max_gap_ms — the recorder
    was off — starts a fresh session."""
    last: int | None = None
    for path in recording_files(paths):
        for recv, msg in read_session(path):
            fresh = last is None or recv - last > max_gap_ms or recv < last - max_gap_ms
            last = recv
            yield fresh, recv, msg


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

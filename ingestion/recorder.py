"""
Records raw WebSocket messages to JSON Lines so any session can be replayed.

Line format: {"recv_ms": <wall-clock ms when received>, "msg": <raw message>}
recv_ms only paces the replay; all analysis runs on the exchange timestamps
inside the messages.

Two recorders:

  JsonlRecorder     one file, appended to (short captures, tests)
  HourlyRecorder    a folder of hourly files for always-on recording. The hour being
                    written is plain JSONL — a crash or power cut loses at most the
                    last few hundred lines, never the file. When the hour ends it is
                    gzipped in the background (~8–10× smaller). A restart inside the
                    same hour appends to that hour's file; leftovers from earlier hours
                    are compressed on start. Recording pauses (and says so) when free
                    disk drops under a floor, so it can't fill a server.
"""
import gzip
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger("analytix.recorder")

HOUR_MS = 3_600_000


class JsonlRecorder:
    def __init__(self, path: str | Path, flush_every: int = 200):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO = self.path.open("a", encoding="utf-8")
        self._flush_every = flush_every
        self._n = 0

    def write(self, msg: dict[str, Any], recv_ms: int | None = None) -> None:
        line = {"recv_ms": recv_ms if recv_ms is not None else int(time.time() * 1000), "msg": msg}
        self._fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._n += 1
        if self._n % self._flush_every == 0:
            self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


def hour_stamp(hour: int) -> str:
    return datetime.fromtimestamp(hour * 3600, tz=timezone.utc).strftime("%Y%m%d_%H00")


def compress(path: Path) -> Path:
    """Gzip a finished hour (written to .tmp first, so a half-written .gz never exists), then delete the original."""
    target = path.with_name(path.name + ".gz")
    n = 2
    while target.exists():                                   # never overwrite an earlier recording of the same hour
        target = path.with_name(f"{path.stem}_{n}.jsonl.gz")
        n += 1
    tmp = target.with_name(target.name + ".tmp")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    os.replace(tmp, target)
    path.unlink()
    return target


class HourlyRecorder:
    def __init__(self, folder: str | Path, prefix: str, flush_every: int = 200, min_free_gb: float = 2.0,
                 background: bool = True):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.flush_every = flush_every
        self.min_free_bytes = int(min_free_gb * 1e9)
        self.background = background
        self.lines = 0
        self.hour_bytes = 0                     # written to the current hour's file this run
        self.paused = False
        self._hour: int | None = None
        self._path: Path | None = None
        self._fh: TextIO | None = None
        self._threads: list[threading.Thread] = []
        self._compress_leftovers()

    @property
    def current_path(self) -> Path | None:
        return self._path

    def write(self, msg: dict[str, Any], recv_ms: int | None = None) -> None:
        recv_ms = recv_ms if recv_ms is not None else int(time.time() * 1000)
        hour = recv_ms // HOUR_MS
        if hour != self._hour:
            self._roll(hour)
        if self.paused or self._fh is None:
            return
        line = json.dumps({"recv_ms": recv_ms, "msg": msg}, separators=(",", ":")) + "\n"
        self._fh.write(line)
        self.lines += 1
        self.hour_bytes += len(line)
        if self.lines % self.flush_every == 0:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None
        for t in self._threads:
            t.join()

    # ── internals ─────────────────────────────────────────────────────────
    def _roll(self, hour: int) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
            if self._path is not None and self._path.exists():
                self._compress(self._path)
        self._hour = hour
        self._path = self.folder / f"{self.prefix}_{hour_stamp(hour)}.jsonl"
        self.hour_bytes = 0
        free = shutil.disk_usage(self.folder).free
        if free < self.min_free_bytes:
            if not self.paused:
                log.error("recording paused: only %.1f GB free in %s", free / 1e9, self.folder)
            self.paused = True
            return
        if self.paused:
            log.info("recording resumed: %.1f GB free", free / 1e9)
        self.paused = False
        self._fh = self._path.open("a", encoding="utf-8")

    def _compress(self, path: Path) -> None:
        def job() -> None:
            try:
                out = compress(path)
                log.info("compressed %s → %s", path.name, out.name)
            except Exception:
                log.exception("could not compress %s (kept as plain JSONL)", path)
        self._threads = [t for t in self._threads if t.is_alive()]
        if self.background:
            t = threading.Thread(target=job, name=f"compress-{path.name}", daemon=False)
            t.start()
            self._threads.append(t)
        else:
            job()

    def _compress_leftovers(self) -> None:
        """Plain files from earlier hours: a run that was stopped, or crashed, before compressing them."""
        now_stamp = hour_stamp(int(time.time() * 1000) // HOUR_MS)
        for p in sorted(self.folder.glob(f"{self.prefix}_*.jsonl")):
            if not p.stem.endswith(now_stamp):
                self._compress(p)

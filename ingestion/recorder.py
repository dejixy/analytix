"""
Records raw WebSocket messages to JSON Lines so any session can be replayed.

Line format: {"recv_ms": <wall-clock ms when received>, "msg": <raw message>}
recv_ms only paces the replay; all analysis runs on the exchange timestamps
inside the messages.
"""
import json
import time
from pathlib import Path
from typing import Any, TextIO


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

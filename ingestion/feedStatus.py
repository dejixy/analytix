"""
Feed health, measured on the *wall* clock.

This is the one place wall time belongs: the buffers can't tell a quiet market
from a dead socket (exchange time just stops), but ingestion knows when the
last message physically arrived.
"""
import time
from dataclasses import dataclass

from config import STALE_AFTER_S


@dataclass
class FeedStatus:
    mode: str                       # "live" | "replay"
    source: str = ""                # URL or file path
    connected: bool = False
    messages: int = 0
    reconnects: int = 0
    last_message_wall: float = 0.0
    last_error: str | None = None
    finished: bool = False          # replay reached the end (and isn't looping)
    loops: int = 0

    def touch(self) -> None:
        self.messages += 1
        self.last_message_wall = time.time()

    @property
    def silence_s(self) -> float | None:
        if not self.last_message_wall:
            return None
        return time.time() - self.last_message_wall

    @property
    def stale(self) -> bool:
        s = self.silence_s
        return self.connected and not self.finished and s is not None and s > STALE_AFTER_S

    def to_dict(self) -> dict:
        s = self.silence_s
        return {
            "mode": self.mode,
            "source": self.source,
            "connected": self.connected,
            "stale": self.stale,
            "finished": self.finished,
            "messages": self.messages,
            "reconnects": self.reconnects,
            "loops": self.loops,
            "silence_s": round(s, 2) if s is not None else None,
            "last_error": self.last_error,
        }

"""
Keeps the event log (big moves, broken levels, cascades) across restarts, in the same SQLite file as the
minute bars. Live mode only: a replay never writes here.

Each event is stored as the JSON the dashboard already receives, under a key that stays unique across
restarts (the engine's own ids start again from 1 in every run). On start-up the last week is loaded back
and shown below the new events, so an update or a restart no longer empties the log.
"""
import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("analytix.storage")

KEEP_MS = 7 * 86_400_000


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS events (coin TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, "
                         "ts INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (coin, key))")
        self._db.commit()
        self._saved: dict[tuple[str, str], str] = {}        # what's on disk already, so unchanged events are skipped

    def save(self, coin: str, rows: list[tuple[str, str, int, dict]]) -> int:
        """rows: (key, kind, ts, data). Writes the new and changed ones; returns how many."""
        n = 0
        try:
            for key, kind, ts, data in rows:
                blob = json.dumps(data, separators=(",", ":"))
                if self._saved.get((coin, key)) == blob:
                    continue
                self._db.execute("INSERT OR REPLACE INTO events (coin, key, kind, ts, data) VALUES (?, ?, ?, ?, ?)",
                                 (coin, key, kind, ts, blob))
                self._saved[(coin, key)] = blob
                n += 1
            if n:
                self._db.commit()
        except sqlite3.Error:
            log.exception("could not save events")
        return n

    def load(self, coin: str, kind: str, since_ms: int, limit: int) -> list[dict]:
        """The newest `limit` events of one kind, newest first."""
        rows = self._db.execute("SELECT key, data FROM events WHERE coin = ? AND kind = ? AND ts >= ? "
                                "ORDER BY ts DESC LIMIT ?", (coin, kind, since_ms, limit)).fetchall()
        out = []
        for key, blob in rows:
            try:
                d = json.loads(blob)
            except ValueError:
                continue
            self._saved[(coin, key)] = blob
            out.append(d)
        return out

    def prune(self, before_ms: int) -> None:
        self._db.execute("DELETE FROM events WHERE ts < ?", (before_ms,))
        self._db.commit()

    def close(self) -> None:
        self._db.commit()
        self._db.close()

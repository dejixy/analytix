"""
Persists live minute bars to SQLite so long windows survive restarts.

Only bars built from the live feed are stored: they are the ones that carry
order flow, depth and open interest, which Hyperliquid can't give us later.
Price-only candle history is cheap to re-download on every start.
"""
import logging
import sqlite3
from dataclasses import astuple, fields
from pathlib import Path

from models.barModel import Bar

log = logging.getLogger("analytix.storage")

_FIELDS = [f.name for f in fields(Bar)]


class BarStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        cols = ", ".join(f"{name}" for name in _FIELDS)
        self._db.execute(f"CREATE TABLE IF NOT EXISTS bars (coin TEXT NOT NULL, {cols}, PRIMARY KEY (coin, timestamp))")
        self._db.commit()
        self._pending = 0

    def save(self, coin: str, bar: Bar) -> None:
        marks = ", ".join("?" for _ in range(len(_FIELDS) + 1))
        try:
            self._db.execute(f"INSERT OR REPLACE INTO bars (coin, {', '.join(_FIELDS)}) VALUES ({marks})",
                             (coin, *astuple(bar)))
            self._pending += 1
            if self._pending >= 4:          # one commit per few bars keeps disk writes cheap
                self._db.commit()
                self._pending = 0
        except sqlite3.Error:
            log.exception("could not save bar")

    def load(self, coin: str, since_ms: int) -> list[Bar]:
        rows = self._db.execute(
            f"SELECT {', '.join(_FIELDS)} FROM bars WHERE coin = ? AND timestamp >= ? ORDER BY timestamp",
            (coin, since_ms),
        ).fetchall()
        return [Bar(*row) for row in rows]

    def prune(self, before_ms: int) -> None:
        self._db.execute("DELETE FROM bars WHERE timestamp < ?", (before_ms,))
        self._db.commit()

    def close(self) -> None:
        self._db.commit()
        self._db.close()

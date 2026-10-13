"""
Persists live minute bars to SQLite so long windows survive restarts.

Only bars built from the live feed are stored: they are the ones that carry
order flow, depth and open interest, which Hyperliquid can't give us later.
Price-only candle history is cheap to re-download on every start.
"""
import logging
import sqlite3

from storage.sqlite import connect, transaction
from dataclasses import astuple, fields
from pathlib import Path

from models.barModel import Bar

log = logging.getLogger("analytix.storage")

_FIELDS = [f.name for f in fields(Bar)]


class BarStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = connect(self.path)
        cols = ", ".join(f"{name}" for name in _FIELDS)
        self._db.execute(f"CREATE TABLE IF NOT EXISTS bars (coin TEXT NOT NULL, {cols}, PRIMARY KEY (coin, timestamp))")
        self._db.commit()
        self._pending: list[tuple] = []

    def save(self, coin: str, bar: Bar) -> None:
        """Bars are written a few at a time in one short transaction. They wait in memory, not in an open
        transaction: an open one holds the file's write lock and blocks every other writer until it commits."""
        self._pending.append((coin, *astuple(bar)))
        if len(self._pending) >= 4:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        marks = ", ".join("?" for _ in range(len(_FIELDS) + 1))
        try:
            with transaction(self._db):
                self._db.executemany(f"INSERT OR REPLACE INTO bars (coin, {', '.join(_FIELDS)}) VALUES ({marks})",
                                     self._pending)
            self._pending = []
        except sqlite3.Error as exc:
            log.warning("could not save bars (will retry): %s", exc)
            if len(self._pending) > 2000:
                self._pending = self._pending[-2000:]

    def load(self, coin: str, since_ms: int) -> list[Bar]:
        self.flush()                        # include bars still waiting to be written
        rows = self._db.execute(
            f"SELECT {', '.join(_FIELDS)} FROM bars WHERE coin = ? AND timestamp >= ? ORDER BY timestamp",
            (coin, since_ms),
        ).fetchall()
        return [Bar(*row) for row in rows]

    def prune(self, before_ms: int) -> None:
        self.flush()
        try:
            with transaction(self._db):
                self._db.execute("DELETE FROM bars WHERE timestamp < ?", (before_ms,))
        except sqlite3.Error as exc:
            log.warning("could not prune bars: %s", exc)

    def close(self) -> None:
        self.flush()
        self._db.commit()
        self._db.close()

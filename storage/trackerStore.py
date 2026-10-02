"""
Persists the outlook's track record to SQLite so long-window hit rates survive restarts.

A 24h lean needs 30 separate days before its hit rate is shown; without this,
every restart would start that count again from zero. Two tables:

    outlook_pending   samples taken but not yet due
    outlook_scored    samples graded against what the price actually did

Writes are batched: the runtime calls commit() every few seconds and on shutdown.
Live mode only — a replay never touches the saved record.
"""
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

from engine.outlookTracker import Pending, Scored
from models.signalModel import Direction

log = logging.getLogger("analytix.storage")


class TrackerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS outlook_pending (
                coin TEXT NOT NULL, win TEXT NOT NULL, start_ms INTEGER NOT NULL, due_ms INTEGER NOT NULL,
                price REAL NOT NULL, lean TEXT NOT NULL, expected_bps REAL NOT NULL, range_bps REAL NOT NULL,
                PRIMARY KEY (coin, win, start_ms));
            CREATE TABLE IF NOT EXISTS outlook_scored (
                coin TEXT NOT NULL, win TEXT NOT NULL, start_ms INTEGER NOT NULL, horizon_ms INTEGER NOT NULL,
                hit INTEGER, in_range INTEGER NOT NULL,
                PRIMARY KEY (coin, win, start_ms));
        """)
        self._db.commit()

    def add_pending(self, coin: str, window: str, p: Pending) -> None:
        self._db.execute("INSERT OR REPLACE INTO outlook_pending VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (coin, window, p.start_ms, p.due_ms, p.price, p.lean.value, p.expected_bps, p.range_bps))

    def settle(self, coin: str, window: str, start_ms: int, scored: Scored | None) -> None:
        """A sample came due: drop it from pending and, if it could be graded, keep the grade."""
        self._db.execute("DELETE FROM outlook_pending WHERE coin = ? AND win = ? AND start_ms = ?",
                         (coin, window, start_ms))
        if scored is not None:
            hit = None if scored.hit is None else int(scored.hit)
            self._db.execute("INSERT OR REPLACE INTO outlook_scored VALUES (?, ?, ?, ?, ?, ?)",
                             (coin, window, scored.start_ms, scored.horizon_ms, hit, int(scored.in_range)))

    def load(self, coin: str, keep: int) -> tuple[dict[str, list[Pending]], dict[str, list[Scored]]]:
        pending: dict[str, list[Pending]] = defaultdict(list)
        for win, start, due, price, lean, exp, rng in self._db.execute(
                "SELECT win, start_ms, due_ms, price, lean, expected_bps, range_bps FROM outlook_pending "
                "WHERE coin = ? ORDER BY win, start_ms", (coin,)):
            pending[win].append(Pending(start, due, price, Direction(lean), exp, rng))
        scored: dict[str, list[Scored]] = {}
        wins = [w for (w,) in self._db.execute("SELECT DISTINCT win FROM outlook_scored WHERE coin = ?", (coin,))]
        for win in wins:
            rows = self._db.execute(
                "SELECT start_ms, horizon_ms, hit, in_range FROM outlook_scored WHERE coin = ? AND win = ? "
                "ORDER BY start_ms DESC LIMIT ?", (coin, win, keep)).fetchall()
            scored[win] = [Scored(s, h, None if hit is None else bool(hit), bool(r)) for s, h, hit, r in reversed(rows)]
        return dict(pending), scored

    def trim(self, coin: str, window: str, keep: int) -> None:
        """Keep only the newest `keep` grades for a window — the same number the tracker holds in memory."""
        self._db.execute(
            "DELETE FROM outlook_scored WHERE coin = ? AND win = ? AND start_ms < ("
            "  SELECT start_ms FROM outlook_scored WHERE coin = ? AND win = ? "
            "  ORDER BY start_ms DESC LIMIT 1 OFFSET ?)",
            (coin, window, coin, window, keep - 1))

    def commit(self) -> None:
        try:
            self._db.commit()
        except sqlite3.Error:
            log.exception("could not save the outlook track record")

    def close(self) -> None:
        self.commit()
        self._db.close()

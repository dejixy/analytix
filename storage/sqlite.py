"""
One way to open the shared SQLite file (data/bars.sqlite) and one way to write to it.

Several parts of the app write to the same file: minute bars, the event log, the hold-clock grid, the
prediction-market records and the bot's trades. SQLite allows one writer at a time, so:

    WAL mode        readers never block the writer and the writer never blocks readers; only writers queue
    short waits     a writer that finds the file busy waits at most BUSY_TIMEOUT_S, so a clash can't freeze the
                    server (every write runs on the event loop)
    transactions    every write is a short transaction that either commits or rolls back. A failed write must
                    roll back: an open transaction keeps its lock, and every later write then fails too
"""
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("analytix.storage")

BUSY_TIMEOUT_S = 1.0


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, timeout=BUSY_TIMEOUT_S)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as exc:                 # another process holds it right now: plain mode still works
        log.warning("could not switch %s to WAL: %s", Path(path).name, exc)
    return db


@contextmanager
def transaction(db: sqlite3.Connection):
    """Commit what the block wrote, or roll it all back if anything failed (then re-raise)."""
    try:
        yield db
        db.commit()
    except BaseException:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        raise

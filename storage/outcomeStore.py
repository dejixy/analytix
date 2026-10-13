"""
Keeps what the prediction-market reads learn slowly across restarts, in the same SQLite file as the minute bars:
each wallet's markout record (sharp wallets) and the odds-versus-outcome history (calibration). Live mode only.
"""
import json
import logging
import sqlite3

from storage.sqlite import connect, transaction
import time
from pathlib import Path

log = logging.getLogger("analytix.storage")


class OutcomeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = connect(self.path)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS pm_wallets (wallet TEXT NOT NULL, horizon TEXT NOT NULL, n INTEGER, w REAL, "
            "s REAL, s2 REAL, updated INTEGER, PRIMARY KEY (wallet, horizon));"
            "CREATE TABLE IF NOT EXISTS pm_calib_snaps (outcome INTEGER PRIMARY KEY, snaps TEXT, updated INTEGER);"
            "CREATE TABLE IF NOT EXISTS pm_calib_results (outcome INTEGER PRIMARY KEY, snaps TEXT, result INTEGER, "
            "settled INTEGER);")
        self._db.commit()

    def save_wallets(self, rows: list[tuple[str, str, int, float, float, float]]) -> None:
        if not rows:
            return
        now = int(time.time() * 1000)
        try:
            with transaction(self._db):
                self._db.executemany("INSERT OR REPLACE INTO pm_wallets (wallet, horizon, n, w, s, s2, updated) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", [(*r, now) for r in rows])
        except sqlite3.Error:
            log.exception("could not save wallet records")

    def load_wallets(self) -> list[tuple[str, str, int, float, float, float]]:
        return self._db.execute("SELECT wallet, horizon, n, w, s, s2 FROM pm_wallets").fetchall()

    def save_snaps(self, outcome: int, snaps: dict) -> None:
        try:
            with transaction(self._db):
                self._db.execute("INSERT OR REPLACE INTO pm_calib_snaps (outcome, snaps, updated) VALUES (?, ?, ?)",
                                 (outcome, json.dumps(snaps), int(time.time() * 1000)))
        except sqlite3.Error:
            log.exception("could not save calibration odds")

    def save_result(self, outcome: int, snaps: dict, result: int) -> None:
        try:
            with transaction(self._db):
                self._db.execute("INSERT OR REPLACE INTO pm_calib_results (outcome, snaps, result, settled) "
                                 "VALUES (?, ?, ?, ?)", (outcome, json.dumps(snaps), result, int(time.time() * 1000)))
                self._db.execute("DELETE FROM pm_calib_snaps WHERE outcome = ?", (outcome,))
        except sqlite3.Error:
            log.exception("could not save a settled market")

    def load_calibration(self) -> tuple[dict[int, dict], list[tuple[int, dict, int]]]:
        snaps = {o: json.loads(s) for o, s in self._db.execute("SELECT outcome, snaps FROM pm_calib_snaps")}
        results = [(o, json.loads(s), r) for o, s, r in self._db.execute("SELECT outcome, snaps, result FROM pm_calib_results")]
        return snaps, results

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

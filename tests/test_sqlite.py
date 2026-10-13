import sqlite3
import time

import pytest

from models.barModel import Bar
from storage.barStore import BarStore
from storage.sqlite import connect, transaction


def test_a_failed_write_rolls_back_and_leaves_no_lock(tmp_path):
    f = tmp_path / "shared.sqlite"
    a, b = connect(f), connect(f)
    assert a.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with transaction(a):
        a.execute("CREATE TABLE t (x)")
    with pytest.raises(sqlite3.OperationalError):
        with transaction(a):
            a.execute("INSERT INTO t VALUES (1)")
            a.execute("INSERT INTO missing VALUES (1)")          # fails halfway through
    t0 = time.time()
    with transaction(b):                                         # the other writer isn't kept waiting
        b.execute("INSERT INTO t VALUES (2)")
    assert time.time() - t0 < 0.5
    assert a.execute("SELECT x FROM t").fetchall() == [(2,)]     # the half-done write was undone


def test_the_bar_store_never_holds_the_write_lock_between_bars(tmp_path):
    f = tmp_path / "bars.sqlite"
    store = BarStore(f)
    other = connect(f)
    with transaction(other):
        other.execute("CREATE TABLE IF NOT EXISTS events (x)")
    store.save("ETH", Bar(1_000, 60, 1, 2, 0.5, 1.5))           # one bar waits in memory, not in an open transaction
    t0 = time.time()
    with transaction(other):
        other.execute("INSERT INTO events VALUES (1)")
    assert time.time() - t0 < 0.5
    assert len(store.load("ETH", 0)) == 1                        # and it's there when read
    store.close()

"""The outlook track record across restarts: late grading from bar history, and the SQLite store."""
from dataclasses import replace

from engine.analyzer import Analyzer
from engine.outlookTracker import HISTORY_GRACE_MS, MIN_PERIODS, OutlookTracker
from models.barModel import Bar
from models.signalModel import Direction
from state import MarketState
from storage.trackerStore import TrackerStore
from tests.test_outlook import _up_lean

H6 = 6 * 3600 * 1000


def _six_hour_lean():
    return replace(_up_lean(), horizon_s=6 * 3600)


def test_a_short_lean_that_came_due_unobserved_is_dropped_not_graded_late():
    tr = OutlookTracker()
    tr.record("1m", 0, 3000.0, _up_lean())
    tr.evaluate(10 * 60_000, 3010.0, price_at=lambda ts: (3010.0, ts))   # app was off for ten minutes
    assert tr.stats("1m").called == 0 and not tr._pending["1m"]


def test_a_long_lean_that_came_due_while_off_is_graded_from_bar_history():
    tr = OutlookTracker()
    tr.record("6h", 0, 3000.0, _six_hour_lean())
    # Back 2h after it came due. The price *now* is lower — the bar at the due moment was higher.
    tr.evaluate(H6 + 2 * 3600_000, 2990.0, price_at=lambda ts: (3010.0, ts + 60_000))
    (s,) = tr._results["6h"]
    assert s.hit is True and tr.stats("6h").called == 1


def test_a_bar_price_too_far_from_the_due_moment_is_not_used():
    tr = OutlookTracker()
    tr.record("6h", 0, 3000.0, _six_hour_lean())
    far = lambda ts: (3010.0, ts - 3600_000)                 # nearest bar is an hour off (> 6h/30)
    tr.evaluate(H6 + 3600_000, 2990.0, price_at=far)
    assert tr._pending["6h"]                                 # held while the backfill may still arrive…
    tr.evaluate(H6 + 3600_000 + HISTORY_GRACE_MS, 2990.0, price_at=far)
    assert not tr._pending["6h"] and tr.stats("6h").called == 0   # …then dropped, never misgraded


def test_a_late_sample_does_not_hold_up_on_time_ones_behind_it():
    tr = OutlookTracker()
    tr.record("6h", 0, 3000.0, _six_hour_lean())
    tr.record("6h", 3 * 3600_000, 3000.0, _six_hour_lean())
    tr.evaluate(3 * 3600_000 + H6, 3010.0, price_at=lambda ts: None)   # first is late, second due right now
    assert [s.start_ms for s in tr._results["6h"]] == [3 * 3600_000]
    assert [p.start_ms for p in tr._pending["6h"]] == [0]                # still waiting for history


def test_bar_history_gives_the_nearer_of_open_and_close():
    st = MarketState("ETH")
    st.merge_bars([Bar(600_000, 300, 3000, 3005, 2999, 3004, source="candle")])
    an = Analyzer(st)
    assert an.price_near(600_000 + 60_000) == (3000, 600_000)     # 1 min in → the open
    assert an.price_near(600_000 + 240_000) == (3004, 900_000)    # 4 min in → the close
    assert an.price_near(0) is None


def test_track_record_survives_a_restart(tmp_path):
    db = tmp_path / "outlook.sqlite"
    store = TrackerStore(db)
    tr, o = OutlookTracker(), _up_lean()
    tr.attach(store, "ETH")
    for i in range(MIN_PERIODS):
        t = i * 60_000
        tr.record("1m", t, 3000.0, o)
        tr.evaluate(t + 60_000, 3003.0 if i % 3 else 2997.0)
    tr.record("6h", 0, 3000.0, _six_hour_lean())          # still pending when the app closes
    before = tr.stats("1m")
    store.close()

    again = OutlookTracker()
    n = again.attach(TrackerStore(db), "ETH")
    assert n == MIN_PERIODS + 1
    assert again.stats("1m") == before and before.hit_rate == 20 / 30
    assert [p.start_ms for p in again._pending["6h"]] == [0]
    assert again._pending["6h"][0].lean is Direction.UP
    # the other coin's record is separate
    assert OutlookTracker().attach(TrackerStore(db), "BTC") == 0


def test_store_keeps_only_the_newest_grades(tmp_path):
    store = TrackerStore(tmp_path / "outlook.sqlite")
    tr, o = OutlookTracker(maxlen=5), _up_lean()
    tr.attach(store, "ETH")
    for i in range(12):
        tr.record("1m", i * 60_000, 3000.0, o)
        tr.evaluate(i * 60_000 + 60_000, 3003.0)
    store.trim("ETH", "1m", 5)
    _, scored = store.load("ETH", keep=100)
    assert [s.start_ms for s in scored["1m"]] == [i * 60_000 for i in range(7, 12)]

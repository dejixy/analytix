import numpy as np

from engine.holdclock import Grid
from engine.twap import Slices, ride, runs

T0 = 1_790_000_000


def test_a_30s_beat_is_a_native_run_and_fills_at_once_are_one_slice():
    sl = Slices()
    for k in range(6):
        t = (T0 + 100 + 30 * k) * 1000
        sl.add("0xtwap", "ETH", 1, t, 3000)
        sl.add("0xtwap", "ETH", 1, t + 300, 1000)         # the same market order taking a second level
    sl.add("0xtwap", "ETH", 1, (T0 + 100 + 30 * 6 + 90) * 1000, 4000)   # 90s later: the beat stopped
    for k in range(5):                                   # someone else at a steady 10s beat
        sl.add("0xbot", "BTC", -1, (T0 + 500 + 10 * k) * 1000, 2000)
    for k in range(5):                                   # random gaps: no run
        sl.add("0xrandom", "ETH", 1, (T0 + 1000 + [0, 7, 60, 64, 200][k]) * 1000, 5000)
    got = runs(sl, min_slices=4, min_usd=1000)
    kinds = {(r.wallet, r.kind): r for r in got}
    assert set(kinds) == {("0xtwap", "native"), ("0xbot", "regular")}
    tw = kinds[("0xtwap", "native")]
    assert len(tw.times) == 6 and tw.usd == [4000] * 6 and tw.beat_s == 30
    assert kinds[("0xbot", "regular")].side == -1


def test_riding_a_run_measures_from_the_fill_to_the_end_of_the_beat():
    n = 2000
    mid = 100 * (1 + 1e-5 * np.arange(n))               # steady rise: 0.1 bps a second
    g = Grid(np.arange(n) + T0, mid * (1 - 1e-6), mid * (1 + 1e-6), mid, np.ones(n, bool))
    sl = Slices()
    for k in range(8):
        sl.add("0xtwap", "ETH", 1, (T0 + 100 + 30 * k) * 1000, 3000)
    run = runs(sl, 4, 1000)[0]
    r = ride(g, run, k=4, lag=6, hold_s=None, leverages=(100,), coin="ETH")
    assert r["hold_s"] == (100 + 30 * 7 + 35) - (100 + 30 * 3 + 6)
    assert r["bps"] > 0 and r["adv"] == 0
    assert r["before"] > 0 and r["pt"][100] > 0
    short = runs(sl, 4, 1000)[0]
    short.side = -1
    assert ride(g, short, 4, 6, 300, (100,), "ETH")["bps"] < 0


def test_the_live_tracker_spots_big_twaps_at_the_4th_slice_and_scores_every_hold():
    from api.twaptrack import HOLDS_S, TwapTracker
    tr = TwapTracker()
    rows = {s: [100 + 0.001 * (s - T0) - 0.01, 100 + 0.001 * (s - T0) + 0.01, 100 + 0.001 * (s - T0)]
            for s in range(T0, T0 + 4000)}
    for k in range(10):
        t = (T0 + 100 + 30 * k) * 1000
        tr.on_fill("0xbig", "ETH", 1, t, 15_000)
        tr.on_fill("0xbig", "ETH", 1, t + 500, 10_000)          # one slice of $25k
        tr.on_fill("0xsmall", "ETH", -1, t, 5_000)              # a small TWAP: ignored
        if k == 2:
            assert not tr.records                                # not yet: 3 slices
        if k == 3:
            assert len(tr.records) == 1
    rec = tr.records[0]
    assert rec["wallet"] == "0xbig" and rec["slices"] == 10 and rec["usd"] == 250_000
    for s in range(T0 + 100, T0 + 4000, 5):
        tr.tick(s * 1000, lambda c: rows)
    assert rec["end_ms"] == (T0 + 100 + 30 * 9) * 1000
    assert set(rec["results"]) == {*map(str, HOLDS_S), "end"}
    assert all(r["bps"] > 0 for r in rec["results"].values())    # price rose all along
    assert rec["hold_end_s"] == (100 + 30 * 9 + 35) - (100 + 30 * 3 + 6)
    assert tr.unsaved == [rec] and not tr.keys

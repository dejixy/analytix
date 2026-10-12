import asyncio
import math
from types import SimpleNamespace

import numpy as np
import pytest

from api.holdclock import HoldClock
from engine.holdclock import fill_drift, make_grid, papertrade_net, race, survival, time_at
from models.bookModel import BookSummary
from state import MarketState


def _walk(n=4 * 3600, sigma_bps=1.0, seed=7, half_spread_bps=0.0, start=1_800_000_000):
    rng = np.random.default_rng(seed)
    mid = 100_000 * np.exp(np.cumsum(rng.normal(0, sigma_bps / 1e4, n)))
    hs = mid * half_spread_bps / 1e4
    return [(start + i, mid[i] - hs[i], mid[i] + hs[i], mid[i], True) for i in range(n)]


def test_the_grid_fills_gaps_and_marks_them():
    g = make_grid([(10, 99, 101, 100, True), (13, 98, 102, 100, True)])
    assert list(g.t) == [10, 11, 12, 13] and list(g.bid) == [99, 99, 99, 98]
    assert list(g.ok) == [True, False, False, True]


def test_survival_matches_a_random_walk_and_falls_with_time():
    g = make_grid(_walk())
    curve = survival(g, +1, 10 / 1e4)
    by = {c["horizon"]: c["survive"] for c in curve}
    expect_60 = 1 - 2 * (0.5 * math.erfc(10 / math.sqrt(60) / math.sqrt(2)))
    assert by[60] == pytest.approx(expect_60, abs=0.06)                  # discrete seconds: a touch more survival
    assert by[10] > by[60] > by[300] > by[900]
    assert time_at(curve, 0.5) is not None and time_at(curve, 0.5) > 60


def test_a_wider_spread_kills_sooner_because_the_trigger_is_the_best_bid():
    tight = survival(make_grid(_walk()), +1, 10 / 1e4)
    wide = survival(make_grid(_walk(half_spread_bps=3.0)), +1, 10 / 1e4)
    assert wide[2]["survive"] < tight[2]["survive"]


def test_like_now_uses_the_starts_that_moved_like_the_last_five_minutes():
    quiet = _walk(n=3 * 3600, sigma_bps=0.5, seed=1)
    busy = _walk(n=3600, sigma_bps=2.0, seed=2, start=quiet[-1][0] + 1)
    k = quiet[-1][3] / busy[0][3]
    rows = quiet + [(t, b * k, a * k, m * k, ok) for t, b, a, m, ok in busy]
    g = make_grid(rows)
    every = {c["horizon"]: c["survive"] for c in survival(g, +1, 10 / 1e4)}
    now = {c["horizon"]: c["survive"] for c in survival(g, +1, 10 / 1e4, like_now=True)}
    assert now[60] < every[60] - 0.1                                     # it's busy now: the clock runs faster


def test_a_target_as_far_as_the_bust_is_about_a_coin_flip():
    r = race(make_grid(_walk()), +1, 8 / 1e4, 8 / 1e4)
    assert r["target_first"] == pytest.approx(0.5, abs=0.07) and r["target_first"] + r["bust_first"] + r["neither"] == pytest.approx(1)
    closer = race(make_grid(_walk()), +1, 8 / 1e4, 3 / 1e4)
    assert closer["target_first"] > 0.6 and closer["median_s_to_target"] > 0


def test_fill_drift_and_papertrade_net():
    d = fill_drift(make_grid(_walk()))
    assert 0.5 < d["1s"]["p50"] < 1.0 and d["2s"]["p90"] > d["1s"]["p90"]
    assert papertrade_net(0.50, 0.2, 800) == pytest.approx((0.50 - 0.016) * 0.98)
    assert papertrade_net(0.01, 0.2, 800) == 0.0                         # inside the deadband: nothing


def _rt(coin="BTC"):
    st = MarketState(coin)
    pipe = SimpleNamespace(state=st)
    return SimpleNamespace(mode="live", coins=[coin], pipelines={coin: pipe}), st


def test_the_service_builds_seconds_from_the_books_and_answers():
    rt, st = _rt()
    hold = HoldClock(rt, None)
    t0 = 1_800_000_000_000
    rng = np.random.default_rng(3)
    mid = 100_000.0
    for i in range(1500):
        mid *= math.exp(rng.normal(0, 1e-4))
        for k in range(2):                                               # two book updates a second
            ts = t0 + i * 1000 + k * 400
            st.books.push(BookSummary(ts, mid, mid - 0.5, mid + 0.5, 0.1, 1e6, 1e6))
            st.now_ms = ts
        if i % 60 == 59:
            hold.sample()
    hold.sample()
    assert 1300 < len(hold.rows["BTC"]) <= 1500
    out = asyncio.run(hold.read("BTC", +1, 800, target_pct=50, entry=mid))
    assert out["dist_bps"] == pytest.approx(12.5) and out["survival"][0]["horizon"] == 10
    assert "race" in out and out["race"]["target_bps"] == pytest.approx(6.25)
    assert out["position"]["dist_now_bps"] == pytest.approx(12.5, abs=0.1) and out["live"]["mid"] == mid

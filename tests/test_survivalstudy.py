import json

import numpy as np

from engine.holdclock import Grid, survival, time_at
from engine.survivalstudy import hold_times, moments, payoff, summarize
from scripts.survivalStudy import book_of

DIST = 1 / 841


def grid(hours=10, seed=1, drift=0.0):
    rng = np.random.default_rng(seed)
    n = hours * 3600
    sig = np.where((np.arange(n) // 1800) % 3 == 0, 1.5e-4, 0.6e-4)
    mid = 2500 * np.exp(np.cumsum(rng.normal(drift, sig)))
    return Grid(np.arange(n) + 1_700_000_000, mid * (1 - 1e-5), mid * (1 + 1e-5), mid, np.ones(n, bool))


def panel(g, i):
    lo = max(0, i - 86_400)
    sub = Grid(g.t[lo:i + 1], g.bid[lo:i + 1], g.ask[lo:i + 1], g.mid[lo:i + 1], g.ok[lo:i + 1])
    out = []
    for side in (1, -1):
        c, cn = survival(sub, side, DIST), survival(sub, side, DIST, like_now=True)
        t = time_at(cn if any(x["survive"] is not None for x in cn) else c, 0.9)
        out.append(1200 if t is None else max(t, 1))
    return out


def test_the_study_shows_what_the_panel_showed():
    g = grid()
    dec, tl, ts = hold_times(g, DIST, min_history=2 * 3600)
    for k in (0, len(dec) // 2, len(dec) - 1):
        want = panel(g, dec[k])
        assert abs(tl[k] - want[0]) <= max(3, 0.03 * want[0])
        assert abs(ts[k] - want[1]) <= max(3, 0.03 * want[1])


def test_a_falling_market_favours_shorts_and_they_win_more():
    g = grid(drift=-1.5e-6)
    ms = moments("ETH", g, 841, [3e-4], min_history=2 * 3600)
    assert np.mean([m.side == -1 for m in ms]) > 0.7
    split = np.median([m.t for m in ms])
    short = summarize(ms, lambda m: [-1], 0, 3e-4, 841, split)
    long = summarize(ms, lambda m: [1], 0, 3e-4, 841, split)
    assert short["win"] > long["win"] and short["avg"] > long["avg"]


def test_payoffs_use_papertrades_cut_and_a_bust_loses_the_margin():
    assert payoff("bust", 3e-4, 841, "ETH") == -1.0
    assert 0.09 < payoff("win", 3e-4, 841, "ETH") < 0.12          # +25% before the cut pays about 10%
    assert payoff(-2e-3, 3e-4, 841, "ETH") == -1.0                  # a loss past the bust is capped at the margin
    assert payoff(-1e-4, 3e-4, 841, "ETH") == -1e-4 * 841


def test_the_fast_book_reader_matches_full_parsing():
    msg = {"channel": "l2Book", "data": {"coin": "ETH", "time": 1790259600000, "levels": [
        [{"px": "2650.0", "sz": "1", "n": 1}, {"px": "2649.9", "sz": "2", "n": 1}],
        [{"px": "2650.1", "sz": "1", "n": 1}]]}}
    line = json.dumps({"recv_ms": 1, "msg": msg}, separators=(",", ":"))
    assert book_of(line) == ("ETH", 1790259600000, 2650.0, 2650.1)
    assert book_of(json.dumps({"recv_ms": 1, "msg": msg})) == ("ETH", 1790259600000, 2650.0, 2650.1)   # spaced: slow path
    sub = {"channel": "subscriptionResponse", "data": {"subscription": {"type": "l2Book", "coin": "ETH"}}}
    assert book_of(json.dumps({"recv_ms": 1, "msg": sub})) is None

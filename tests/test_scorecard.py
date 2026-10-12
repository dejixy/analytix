import math
import random
import time

import pytest

from engine.outcomes import HOUR_MS, profile_var, vol_profile
from engine.scorecard import MIN_MARKETS, gate, score
from engine.sharp import Calibration
from ingestion.intel import parse_vol_index

MONDAY = 1760918400000          # Mon 20 Oct 2025 00:00 UTC


def _snap(p, m, bid=None, ask=None, src="moves", h="6h"):
    return {f"ref:{h}": {"p": p, "src": src, "m": m, "bid": bid, "ask": ask}}


def test_a_source_closer_to_what_happened_beats_the_market_and_its_edges_pay():
    random.seed(3)
    results = []
    for i in range(40):
        y = 1 if random.random() < 0.7 else 0
        good = 0.8 if y else 0.3                    # leans the right way more than the market's 50/50
        results.append((i, _snap(good, 0.5, bid=0.48, ask=0.52, src="options"), y))
        results.append((100 + i, _snap(1 - good, 0.5, bid=0.48, ask=0.52, src="moves"), y))
    card = score(results)
    assert card["options"]["verdict"] == "beats" and card["options"]["skill"] > 0.2
    assert card["moves"]["verdict"] == "worse" and card["moves"]["skill"] < 0
    assert card["options"]["edges"] == 40 and card["options"]["edge_return"] > 0.3
    assert card["moves"]["edge_return"] < 0
    assert gate("options", card) == "show" and gate("moves", card) == "hide"


def test_unproven_sources_are_shown_but_our_own_models_dont_alert_until_proven():
    card = score([(i, _snap(0.6, 0.5, src="moves"), 1) for i in range(MIN_MARKETS - 1)])
    assert card["moves"]["verdict"] == "unproven"
    assert gate("moves", card) == "quiet" and gate("book", card) == "show" and gate("goals", {}) == "quiet"
    assert gate("final", {}) == "show"
    assert "final" not in score([(1, _snap(1.0, 0.9, src="final"), 1)])        # a final score isn't a forecast


def test_checkpoints_are_saved_once_and_the_calibration_table_ignores_them():
    cal = Calibration()
    exp = 10 * HOUR_MS
    assert cal.ref_due(7, exp, exp - 30 * 60_000) == "1h"       # first seen inside the last hour
    assert cal.snaps[7]["ref:24h"] is None and cal.snaps[7]["ref:6h"] is None
    cal.note_ref(7, "1h", {"p": 0.6, "src": "moves", "m": 0.5, "bid": 0.49, "ask": 0.51})
    assert cal.ref_due(7, exp, exp - 20 * 60_000) is None
    cal.note(7, exp, 0.5, exp - 30 * 60_000)
    cal.resolve(7, 1)
    assert cal.table("6h")["settled"] == 1 and score(cal.results)["moves"]["checks"] == 1


def _index_rows(weeks=4, session=0.004, quiet=0.0002, seed=1):
    random.seed(seed)
    rows, c = [], 100.0
    for h in range(24 * 7 * weeks):
        t = MONDAY + h * HOUR_MS
        g = time.gmtime(t / 1000)
        sd = session if (g.tm_wday < 5 and 13 <= g.tm_hour < 20) else quiet
        c *= math.exp(random.gauss(0, sd))
        rows.append({"t": t, "c": c})
    return rows


def test_the_hour_of_week_profile_expects_the_session_ahead():
    p = vol_profile(_index_rows())
    mon_8 = MONDAY + 8 * HOUR_MS
    mon_20 = MONDAY + 20 * HOUR_MS
    session = math.sqrt(profile_var(p, mon_8, mon_20))
    flat = math.sqrt(p.week / 168 * 12)
    assert session == pytest.approx(math.sqrt(7) * 0.004, rel=0.2)       # the 7 session hours, not the quiet ones
    assert session > 1.5 * flat                                           # what one average over the clock misses
    sat = MONDAY + 5 * 24 * HOUR_MS
    assert math.sqrt(profile_var(p, sat, sat + 24 * HOUR_MS)) < 0.003     # a weekend day barely moves
    assert profile_var(p, mon_20, mon_8) == 0.0
    assert vol_profile(_index_rows()[:20]) is None


def test_cboe_quotes_parse():
    assert parse_vol_index({"data": {"symbol": "^VIX", "current_price": 16.4}}) == 16.4
    assert parse_vol_index({"data": {"close": 21.0}}) == 21.0
    assert parse_vol_index({"data": {}}) is None and parse_vol_index({}) is None

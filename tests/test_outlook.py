import math

from engine.outlook import SKILL, make_outlook
from engine.outlookTracker import OutlookTracker
from models.explanationModel import Explanation, PriceMove, Significance
from models.signalModel import Direction, SignalResult

FLOW_UP = SignalResult("volume_imbalance", "Order flow", 0.6, 1.0, Direction.UP, "", "", "80% buy",
                       {"imbalance": 0.6, "buy_notional": 8e5, "sell_notional": 2e5})


def ex(window="1m", seconds=60, signals=None, z=0.5):
    move = PriceMove(3000, 3001, 3002, 2999, 3.3, z, 7.7, Significance.QUIET if abs(z) < 1.5 else Significance.SIGNIFICANT,
                     Direction.UP)
    return Explanation(window, seconds, 0, seconds * 1000, 1.0, move, "", [], [], 0.0, signals or {})


def test_bid_heavy_book_and_buyers_lean_up_but_stay_humble():
    o = make_outlook(ex(signals={"volume_imbalance": FLOW_UP}), book_imbalance=0.4, sigma_1s_bps=1.0,
                     horizon_key="short")
    sigma = math.sqrt(60)
    assert o.lean is Direction.UP
    assert 0 < o.expected_bps <= SKILL * sigma + 1e-9
    assert 0.5 < o.p_up <= 0.6
    assert abs(o.range_bps - sigma) < 1e-9
    assert o.line.startswith("Next 1m: lean up") and "bid-heavy" in o.line


def test_no_evidence_means_no_lean():
    o = make_outlook(ex(), book_imbalance=0.0, sigma_1s_bps=1.0, horizon_key="short")
    assert o.lean is Direction.NEUTRAL and "coin flip" in o.line


def test_long_horizons_can_lean_on_flow_and_book_alone():
    # Normal funding used to silence the 6h+ outlook entirely; flow + book must still count.
    o = make_outlook(ex("12h", 43200, {"volume_imbalance": FLOW_UP}, z=2.0), book_imbalance=0.3, sigma_1s_bps=1.0,
                     horizon_key="very_long")
    assert o.lean is Direction.UP and abs(o.score) >= 0.15


def test_conflicting_evidence_is_spelled_out():
    flow_down = SignalResult("volume_imbalance", "Order flow", -0.34, 0.8, Direction.DOWN, "", "", "",
                             {"imbalance": -0.34, "buy_notional": 3.3e5, "sell_notional": 6.7e5})
    o = make_outlook(ex("12h", 43200, {"volume_imbalance": flow_down}), 0.24, 1.0, "very_long")
    assert " — but " in o.line


def test_crowded_funding_matters_more_on_long_horizons():
    fund = SignalResult("funding", "Funding & OI", 0, 0.1, Direction.NEUTRAL, "", "", "", {"funding_apr": 60.0})
    short = make_outlook(ex(signals={"funding": fund}), 0.0, 1.0, "short")
    long_ = make_outlook(ex("24h", 86400, {"funding": fund}), 0.0, 1.0, "very_long")
    assert short.score == 0
    assert long_.score < 0 and "crowded longs" in long_.line


def test_tracker_scores_leans_after_the_horizon():
    tr = OutlookTracker()
    o = make_outlook(ex(signals={"volume_imbalance": FLOW_UP}), 0.4, 1.0, "short")
    for i in range(12):                            # 12 up-leans, 6s apart, each followed by a rise
        t = i * 6_000
        tr.record("1m", t, 3000.0, o)
        tr.evaluate(t + 60_000, 3003.0)
    rate, n = tr.stats("1m")
    assert n == 12 and rate == 1.0

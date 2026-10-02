import math

from engine.outlook import SKILL, STRONG_SCORE, make_outlook, track_line
from engine.outlookTracker import MIN_PERIODS, OutlookTracker, independent_periods
from models.explanationModel import Explanation, PriceMove, Significance, TrackRecord
from models.signalModel import Direction, SignalResult

FLOW_UP = SignalResult("volume_imbalance", "Order flow", 0.6, 1.0, Direction.UP, "", "", "80% buy",
                       {"imbalance": 0.6, "buy_notional": 8e5, "sell_notional": 2e5})


def ex(window="1m", seconds=60, signals=None, z=0.5):
    move = PriceMove(3000, 3001, 3002, 2999, 3.3 if z >= 0 else -3.3, z, 7.7,
                     Significance.QUIET if abs(z) < 1.5 else Significance.SIGNIFICANT,
                     Direction.UP if z >= 0 else Direction.DOWN)
    return Explanation(window, seconds, 0, seconds * 1000, 1.0, move, "", [], [], 0.0, signals or {})


FLOW_DOWN = SignalResult("volume_imbalance", "Order flow", -0.6, 1.0, Direction.DOWN, "", "", "80% sell",
                         {"imbalance": -0.6, "buy_notional": 2e5, "sell_notional": 8e5})


def test_strong_agreement_earns_a_lean_and_stays_humble():
    # bid-heavy book + buyers + an up move that the flow explains: everything agrees
    o = make_outlook(ex(signals={"volume_imbalance": FLOW_UP}, z=2.0), book_imbalance=0.4, sigma_1s_bps=1.0,
                     horizon_key="short")
    sigma = math.sqrt(60)
    assert o.lean is Direction.UP and abs(o.score) >= STRONG_SCORE
    assert 0 < o.expected_bps <= SKILL * sigma + 1e-9
    assert 0.55 <= o.p_up <= 0.6 and o.odds_source == "model"
    assert abs(o.range_bps - sigma) < 1e-9
    assert o.line.startswith("Next 1m: lean up (") and "% est.)" in o.line and "bid-heavy" in o.line


def test_weak_agreement_is_a_coin_flip_with_the_tilt_shown():
    # bid-heavy book alone: points up, but nowhere near strong agreement
    o = make_outlook(ex(), book_imbalance=0.15, sigma_1s_bps=1.0, horizon_key="short")
    assert o.lean is Direction.NEUTRAL and o.tilt is Direction.UP
    assert o.line.startswith("Next 1m: coin flip · tilt up 5") and "% · typical" in o.line
    assert o.odds < 0.55 and o.odds_source == "model"


def test_earned_odds_replace_the_estimate_once_there_is_a_track_record():
    track = TrackRecord(hit_rate=0.62, called=900, periods=45.0, range_rate=0.7)
    o = make_outlook(ex(signals={"volume_imbalance": FLOW_UP}, z=2.0), 0.4, 1.0, "short", track)
    assert o.odds == 0.62 and o.odds_source == "earned"
    assert "lean up (62% earned)" in o.line


def test_no_evidence_means_no_lean():
    o = make_outlook(ex(), book_imbalance=0.0, sigma_1s_bps=1.0, horizon_key="short")
    assert o.lean is Direction.NEUTRAL and "coin flip · no tilt" in o.line


def test_long_horizons_still_tilt_on_flow_and_book_alone():
    # Normal funding used to silence the 6h+ outlook entirely; flow + book must still count.
    o = make_outlook(ex("12h", 43200, {"volume_imbalance": FLOW_UP}, z=2.0), book_imbalance=0.3, sigma_1s_bps=1.0,
                     horizon_key="very_long")
    assert o.tilt is Direction.UP and o.score >= 0.15


def test_conflicting_evidence_is_spelled_out():
    flow_down = SignalResult("volume_imbalance", "Order flow", -0.34, 0.8, Direction.DOWN, "", "", "",
                             {"imbalance": -0.34, "buy_notional": 3.3e5, "sell_notional": 6.7e5})
    o = make_outlook(ex("12h", 43200, {"volume_imbalance": flow_down}), 0.24, 1.0, "very_long")
    assert o.tilt is Direction.NEUTRAL and " vs " in o.line and o.line.endswith("cancel out")
    tilted = make_outlook(ex("10m", 600, {"volume_imbalance": FLOW_UP}, z=2.0), -0.1, 1.0, "medium")
    assert tilted.tilt is Direction.UP and tilted.line.endswith(" — but book 10% ask-heavy")


def test_a_faint_one_sided_sign_is_called_faint():
    o = make_outlook(ex(), book_imbalance=0.02, sigma_1s_bps=1.0, horizon_key="short")
    assert o.tilt is Direction.NEUTRAL and o.line.endswith("no tilt · typical ±0.08% — only faint signs: book 2% bid-heavy")


def test_crowded_funding_matters_more_on_long_horizons():
    fund = SignalResult("funding", "Funding & OI", 0, 0.1, Direction.NEUTRAL, "", "", "", {"funding_apr": 60.0})
    short = make_outlook(ex(signals={"funding": fund}), 0.0, 1.0, "short")
    long_ = make_outlook(ex("24h", 86400, {"funding": fund}), 0.0, 1.0, "very_long")
    assert short.score == 0
    assert long_.score < 0 and "crowded longs" in long_.line


def _up_lean():
    o = make_outlook(ex(signals={"volume_imbalance": FLOW_UP}, z=2.0), 0.4, 1.0, "short")
    assert o.lean is Direction.UP
    return o


def test_tracker_scores_leans_after_the_horizon():
    tr, o = OutlookTracker(), _up_lean()
    for i in range(MIN_PERIODS):                   # one up-lean per minute, each followed by a rise
        t = i * 60_000
        tr.record("1m", t, 3000.0, o)
        tr.evaluate(t + 60_000, 3003.0)
    t = tr.stats("1m")
    assert t.called == MIN_PERIODS and t.periods == MIN_PERIODS and t.hit_rate == 1.0


def test_overlapping_samples_count_as_fewer_periods():
    # A 1m lean sampled every 6s: ten samples share each minute of price action.
    tr, o = OutlookTracker(), _up_lean()
    for t in range(0, 99 * 6_000 + 60_001, 6_000):        # the engine ticks on, grading each sample on time
        if t < 100 * 6_000:
            tr.record("1m", t, 3000.0, o)
        tr.evaluate(t, 3003.0)
    t = tr.stats("1m")
    assert t.called == 100
    assert abs(t.periods - 10.9) < 1e-9            # 1 + 99 × 0.1
    assert t.hit_rate is None                      # 100 samples, but only ~11 independent minutes


def test_a_gap_counts_as_a_fresh_period():
    assert independent_periods([0, 6_000, 600_000], 60_000) == 1 + 0.1 + 1
    assert independent_periods([], 60_000) == 0


def test_flat_price_is_not_a_win_for_either_side():
    tr = OutlookTracker()
    down = make_outlook(ex(signals={"volume_imbalance": FLOW_DOWN}, z=-2.0), -0.4, 1.0, "short")
    assert down.lean is Direction.DOWN
    tr.record("1m", 0, 3000.0, down)
    tr.evaluate(60_000, 3000.0)                    # price didn't move
    t = tr.stats("1m")
    assert t.called == 0 and t.periods == 0


def test_range_rate_counts_outcomes_inside_the_stated_range():
    tr, o = OutlookTracker(), _up_lean()           # range ±√60 ≈ 7.7 bps around a small expected rise
    for i in range(MIN_PERIODS):
        t = i * 60_000
        tr.record("1m", t, 3000.0, o)
        tr.evaluate(t + 60_000, 3000.3 if i % 2 else 3015.0)   # +1 bp (inside) or +50 bps (outside)
    assert tr.stats("1m").range_rate == 0.5


def test_track_line_reports_periods_not_samples():
    assert track_line("10m", TrackRecord()) == f"scoring leans… 0 of {MIN_PERIODS} separate 10m periods checked"
    assert "~17 of" in track_line("10m", TrackRecord(called=173, periods=17.3))
    full = track_line("10m", TrackRecord(hit_rate=0.55, called=400, periods=40.0, range_rate=0.71))
    assert full == "leans right 55% over ~40 separate 10m periods · inside range 71%"
    o = make_outlook(ex(), 0.0, 1.0, "short", TrackRecord(called=12, periods=1.2))
    assert o.scored == 12 and o.periods == 1.2 and o.track.startswith("scoring leans… ~1 of")

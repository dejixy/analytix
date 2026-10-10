from models.signalModel import Direction
from signals.depthDelta import depth_delta
from signals.funding import funding
from signals.liquidations import liquidations
from signals.priceMove import price_move
from signals.volumeImbalance import volume_imbalance
from models.explanationModel import Significance
from tests.helpers import T0, ctx, make_slice, summary, trade


def test_volume_imbalance_measures_aggressor_share():
    trades = [trade(T0 + i, sz=3.0, side="B") for i in range(3)] + [trade(T0 + 10, sz=1.0, side="A")]
    r = volume_imbalance(make_slice(trades))
    assert r.direction is Direction.UP
    assert abs(r.score - 0.8) < 1e-9                  # (9 - 1) / 10
    assert r.strength > 0.5
    assert "90%" in r.phrase


def test_volume_imbalance_empty_window_is_neutral():
    r = volume_imbalance(make_slice([]))
    assert r.direction is Direction.NEUTRAL and r.strength == 0


def test_depth_delta_thinning_asks_is_bullish():
    r = depth_delta(make_slice(book_start=summary(T0, ask=1e6), book_end=summary(T0 + 60_000, ask=4e5)))
    assert r.direction is Direction.UP and r.score > 0.9
    assert "sell orders thinning out" in r.phrase


def test_depth_delta_pulled_bids_is_bearish():
    r = depth_delta(make_slice(book_start=summary(T0, bid=1e6), book_end=summary(T0 + 60_000, bid=5e5)))
    assert r.direction is Direction.DOWN
    assert "buy orders being pulled" in r.phrase


def test_funding_quadrants():
    up = (summary(T0, mid=3000), summary(T0 + 60_000, mid=3015))
    down = (summary(T0, mid=3000), summary(T0 + 60_000, mid=2985))
    oi_up = (ctx(T0, oi=100_000), ctx(T0 + 60_000, oi=101_000))
    oi_dn = (ctx(T0, oi=100_000), ctx(T0 + 60_000, oi=99_000))
    cases = [(up, oi_up, "new longs opening"), (up, oi_dn, "shorts covering"),
             (down, oi_up, "new shorts opening"), (down, oi_dn, "longs closing")]
    for books, ctxs, expected in cases:
        r = funding(make_slice(book_start=books[0], book_end=books[1], ctx_start=ctxs[0], ctx_end=ctxs[1]))
        assert expected in r.phrase, (expected, r.phrase)


def test_funding_flags_crowded_longs_being_flushed():
    r = funding(make_slice(book_start=summary(T0, mid=3000), book_end=summary(T0 + 60_000, mid=2970),
                           ctx_start=ctx(T0, oi=100_000, funding=0.00005),
                           ctx_end=ctx(T0 + 60_000, oi=98_000, funding=0.00005)))
    assert "crowded longs being forced out" in r.phrase


def test_liquidation_cascade_detected_from_chained_sell_sweeps():
    trades = []
    for k in range(5):                                   # 5 sweeps, 1s apart, each walking 4 levels
        ts = T0 + k * 1000
        trades += [trade(ts, px=3000 - k - lvl * 0.1, sz=6.0, side="A", tid=ts * 10 + lvl, h=f"sweep{k}")
                   for lvl in range(4)]
    trades += [trade(T0 + 10_000 + i, sz=0.5, side="B") for i in range(10)]   # small dip buyers
    r = liquidations(make_slice(trades))
    assert r.direction is Direction.DOWN and r.strength >= 0.8
    assert r.metrics["cascades"] == 1 and r.metrics["sweeps_sell"] == 5
    assert "long-liquidation-style cascade" in r.phrase


def test_small_orders_are_not_sweeps():
    r = liquidations(make_slice([trade(T0 + i, sz=1.0, side="A") for i in range(50)]))
    assert r.strength == 0 and r.metrics["sweeps_sell"] == 0


def test_price_move_significance_scales_with_volatility():
    start, end = summary(T0, mid=3000.0), summary(T0 + 60_000, mid=3006.0)   # +20 bps in 1m
    calm = price_move(make_slice(book_start=start, book_end=end))             # sigma 1 bps/√s → z≈2.6
    assert calm.significance is Significance.SIGNIFICANT
    from signals.base import Baseline
    wild = price_move(make_slice(book_start=start, book_end=end,
                                 baseline=Baseline(5.0, 10_000.0, 50_000.0, 900.0)))
    assert wild.significance is Significance.QUIET

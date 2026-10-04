import math

import numpy as np
import pytest

from engine.planner import (
    Candles,
    candles_from_bars,
    expected_funding_rate,
    fit_model,
    liq_probability,
    liquidation_price,
    make_plan,
    maintenance_rate,
    rough_model,
    simulate,
)
from models.barModel import Bar

ALPHA, BETA, VOL, DOF, SUB = 0.08, 0.90, 0.006, 4.0, 12


def _garch_t(n, seed, state=None):
    """Hourly candles from a known GARCH(1,1)-t process, each built from 12 sub-steps so it has real wicks."""
    rng = np.random.default_rng(seed)
    v, px = state if state else (VOL ** 2, 100.0)
    w = VOL ** 2 * (1 - ALPHA - BETA)
    close, low, high = [], [], []
    for _ in range(n):
        steps = rng.standard_t(DOF, SUB) * math.sqrt((DOF - 2) / DOF) * math.sqrt(v / SUB)
        path = px * np.exp(np.cumsum(steps))
        low.append(min(px, path.min()))
        high.append(max(px, path.max()))
        r = math.log(path[-1] / px)
        px = path[-1]
        close.append(px)
        v = w + ALPHA * r * r + BETA * v
    return Candles(3600, np.array(close), np.array(low), np.array(high)), (v, px)


def _true_touch(v0, hours, dist, n=20_000, seed=5):
    """Chance the true process trades `dist` (log) below its start within `hours`, by brute force."""
    rng = np.random.default_rng(seed)
    w = VOL ** 2 * (1 - ALPHA - BETA)
    v = np.full(n, v0)
    cum = np.zeros(n)
    low = np.zeros(n)
    for _ in range(hours):
        steps = rng.standard_t(DOF, (n, SUB)) * math.sqrt((DOF - 2) / DOF) * np.sqrt(v / SUB)[:, None]
        path = cum[:, None] + np.cumsum(steps, axis=1)
        low = np.minimum(low, path.min(axis=1))
        r = path[:, -1] - cum
        cum = path[:, -1]
        v = w + ALPHA * r * r + BETA * v
    return float(np.mean(-low >= dist))


@pytest.fixture(scope="module")
def market():
    candles, state = _garch_t(5000, seed=3)
    return candles, state, fit_model(candles)


def test_the_fit_recovers_how_volatility_clusters(market):
    candles, (v_now, _), m = market
    assert m.kind == "fhs" and m.n == 4999
    assert 0.95 <= m.alpha + m.beta <= 0.999                   # true persistence 0.98
    assert abs(math.sqrt(m.var_long) / VOL - 1) < 0.15
    assert abs(math.sqrt(m.var_next / v_now) - 1) < 0.25       # it knows how volatile it is right now


@pytest.mark.parametrize("hours,move", [(4, 0.01), (24, 0.02), (24, 0.05), (72, 0.08)])
def test_touch_odds_match_the_true_process(market, hours, move):
    """The planner's chance of price trading 1–8% lower before exit vs brute force on the real process."""
    _, (v_now, _), m = market
    dist = -math.log1p(-move)
    model = float(np.mean(-simulate(m, hours).low >= dist))
    truth = _true_touch(v_now, hours, dist)
    assert abs(model - truth) <= max(0.03, 0.2 * truth), (model, truth)


def test_liquidation_price_matches_hyperliquid_formula():
    m = maintenance_rate(40)                                   # BTC: 40× max → 1.25% maintenance
    long = liquidation_price(100.0, 1, 5, m)
    short = liquidation_price(100.0, -1, 5, m)
    # the docs' form: price − side·margin_available/size/(1 − l·side), margin_available = margin − maintenance
    for side, got in ((1, long), (-1, short)):
        margin_avail = 100.0 / 5 - 100.0 * m
        assert got == pytest.approx(100.0 - side * margin_avail / (1 - m * side))
    assert long == pytest.approx(81.0127, rel=1e-4) and short == pytest.approx(118.5185, rel=1e-4)
    assert liquidation_price(100.0, 1, 1, m) is None           # a 1× long can't be liquidated


def test_liquidation_odds_rise_with_leverage_and_the_plan_adds_up(market):
    _, _, m = market
    paths = simulate(m, 24)
    probs = [liq_probability(paths, 1, 100.0, lev, maintenance_rate(40)) for lev in (2, 5, 10, 20, 40)]
    assert probs == sorted(probs) and probs[0] == 0.0 and probs[-1] > 0.3
    plan = make_plan(coin="BTC", side=1, margin=1_000, leverage=10, hours=24, entry=100.0, paths=paths, model=m,
                     max_leverage=40, entry_slip_bps=1.0, exit_slip_bps=1.0, funding_now=0.0000125, funding_avg=0.0000125)
    assert plan.notional == 10_000 and plan.costs.fees == pytest.approx(9.0)
    assert plan.costs.slippage == pytest.approx(2.0) and plan.costs.funding == pytest.approx(0.0000125 * 24 * 10_000)
    assert plan.breakeven_pct == pytest.approx(plan.costs.total / 100)
    assert plan.liq_prob == pytest.approx(liq_probability(paths, 1, 100.0, 10, maintenance_rate(40)))
    assert liq_probability(paths, 1, 100.0, plan.safe_leverage["1"], plan.maint) <= 0.01
    assert plan.safe_leverage["1"] <= plan.safe_leverage["5"]
    assert plan.outcome["p5"] < plan.outcome["p50"] < plan.outcome["p95"] and 0.3 < plan.prob_profit < 0.55
    assert plan.drawdown_pct[1] < plan.drawdown_pct[0] < 0                  # a long's worst moves are down
    against = [t for t in plan.touches if t.kind == "against"]
    assert against and all(t.move_pct < 0 and t.pnl < 0 for t in against)
    assert [t.prob for t in against] == sorted((t.prob for t in against), reverse=True)


def test_a_short_mirrors_a_long_without_a_directional_view(market):
    _, _, m = market
    paths = simulate(m, 24)
    long = make_plan(coin="X", side=1, margin=1_000, leverage=5, hours=24, entry=100.0, paths=paths, model=m,
                     max_leverage=40, entry_slip_bps=0, exit_slip_bps=0, funding_now=None, funding_avg=None)
    short = make_plan(coin="X", side=-1, margin=1_000, leverage=5, hours=24, entry=100.0, paths=paths, model=m,
                      max_leverage=40, entry_slip_bps=0, exit_slip_bps=0, funding_now=None, funding_avg=None)
    assert abs(long.prob_profit - short.prob_profit) < 0.05
    assert abs(abs(long.drawdown_pct[0]) - abs(short.drawdown_pct[0])) < 0.3
    assert short.liq_price > 100.0 > long.liq_price


def test_funding_drifts_from_today_back_to_the_weekly_average():
    assert expected_funding_rate(10, 0.0001, 0.0001) == pytest.approx(0.001)
    far = expected_funding_rate(1000, 0.001, 0.0001)
    assert far == pytest.approx(0.0001 * 1000 + 0.0009 * 8 / math.log(2), rel=1e-3)
    assert expected_funding_rate(1, 0.001, 0.0) == pytest.approx(0.001 * (1 - 0.5 / 8 * 1), rel=0.05)
    assert expected_funding_rate(5, None, None) == 0.0


def test_too_little_history_falls_back_to_a_rough_fat_tailed_walk():
    short, _ = _garch_t(100, seed=9)
    assert fit_model(short) is None
    m = rough_model(sigma_1s_bps=1.0, step_s=300)
    paths = simulate(m, 24)
    assert m.kind == "rough" and abs(np.std(paths.final) / (1e-4 * math.sqrt(86_400)) - 1) < 0.1
    assert np.all(paths.low <= np.minimum(0, paths.final) + 1e-12) and np.all(paths.high >= 0)


def test_bars_aggregate_into_complete_candles_only():
    t0 = 1_790_000_000_000 // 300_000 * 300_000
    bars = [Bar(timestamp=t0 + i * 60_000, span_s=60, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i)
            for i in range(10)]
    bars += [Bar(timestamp=t0 + 15 * 60_000 + i * 60_000, span_s=60, open=1, high=2, low=0.5, close=1.5)
             for i in range(5)]                                 # after a 5-minute gap
    c = candles_from_bars(bars + [Bar(timestamp=t0 + 20 * 60_000, span_s=60, open=1, high=1, low=1, close=1)], 300)
    assert len(c) == 1 and c.close[0] == 1.5                    # the run restarts after the gap; forming bucket dropped

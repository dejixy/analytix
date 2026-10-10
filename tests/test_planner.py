import math

import numpy as np
import pytest

from engine.planner import (
    Candles,
    candles_from_bars,
    expected_funding_rate,
    fit_model,
    liq_distance,
    liq_probability,
    liquidation_price,
    make_plan,
    maintenance_rate,
    rough_model,
    season_bucket,
    simulate,
)
from models.barModel import Bar

ALPHA, BETA, VOL, DOF, SUB = 0.08, 0.90, 0.006, 4.0, 12
T_START = 1_780_000_000_000 // 3_600_000 * 3_600_000        # a UTC hour


def _w(vol):
    return vol ** 2 * (1 - ALPHA - BETA)


def _diurnal(t_ms, amp):
    """Volatility multiplier by hour of day: 1 ± amp, peaking at 14:00 UTC."""
    hour = (np.asarray(t_ms) // 3_600_000) % 24
    return 1 + amp * np.cos((hour - 14) / 24 * 2 * np.pi)


def _garch_t(n, seed, step_s=3600, vol=VOL, amp=0.0, state=None):
    """Candles from a known GARCH(1,1)-t process (optionally with a daily volatility cycle), each built from
    12 sub-steps so it has real wicks. Returns the candles and the state after the last one."""
    rng = np.random.default_rng(seed)
    v, px, t = state if state else (vol ** 2, 100.0, T_START)
    w = _w(vol)
    ts, close, low, high = [], [], [], []
    for _ in range(n):
        f = float(_diurnal(t, amp)) if amp else 1.0
        steps = rng.standard_t(DOF, SUB) * math.sqrt((DOF - 2) / DOF) * math.sqrt(v / SUB) * f
        path = px * np.exp(np.cumsum(steps))
        ts.append(t)
        low.append(min(px, path.min()))
        high.append(max(px, path.max()))
        r = math.log(path[-1] / px) / f
        px = path[-1]
        close.append(px)
        v = w + ALPHA * r * r + BETA * v
        t += step_s * 1000
    c = Candles(step_s, np.array(ts, dtype=np.int64), np.array(close), np.array(low), np.array(high))
    return c, (v, px, t)


def _true_touch(v0, steps, dist, start_t=T_START, step_s=3600, vol=VOL, amp=0.0, n=20_000, seed=5):
    """Chance the true process trades `dist` (log) below its start within `steps` candles, by brute force."""
    rng = np.random.default_rng(seed)
    w = _w(vol)
    v = np.full(n, v0)
    cum = np.zeros(n)
    low = np.zeros(n)
    for i in range(steps):
        f = float(_diurnal(start_t + i * step_s * 1000, amp)) if amp else 1.0
        x = rng.standard_t(DOF, (n, SUB)) * math.sqrt((DOF - 2) / DOF) * np.sqrt(v / SUB)[:, None] * f
        path = cum[:, None] + np.cumsum(x, axis=1)
        low = np.minimum(low, path.min(axis=1))
        r = (path[:, -1] - cum) / f
        cum = path[:, -1]
        v = w + ALPHA * r * r + BETA * v
    return float(np.mean(-low >= dist))


def _dist_for(v0, steps, target, **kw):
    """The log distance at which the true touch chance is about `target`."""
    lo, hi = 0.001, 0.5
    for _ in range(18):
        mid = (lo + hi) / 2
        if _true_touch(v0, steps, mid, n=8_000, **kw) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@pytest.fixture(scope="module")
def market():
    candles, state = _garch_t(5000, seed=3)
    return candles, state, fit_model(candles)


def _model_touch(m, hours, dist, v0=None, start_ms=None):
    _, state = None, None
    p = simulate(m, hours, v0, start_ms if start_ms is not None else m.last_t + m.step_s * 1000)
    return float(np.mean(-p.low >= dist))


def test_the_fit_recovers_how_volatility_clusters(market):
    candles, (v_now, _, _), m = market
    assert m.kind == "fhs" and m.n == 4999
    assert 0.96 <= m.alpha + m.beta <= 0.995                   # true persistence 0.98
    assert abs(m.alpha - ALPHA) < 0.04
    assert abs(math.sqrt(m.var_next / v_now) - 1) < 0.25       # it knows how volatile it is right now
    assert np.all(np.abs(m.season - 1) < 0.25)                 # no daily cycle in this market: factors near 1


@pytest.mark.parametrize("hours,move", [(4, 0.01), (24, 0.02), (24, 0.05), (72, 0.08)])
def test_touch_odds_match_the_true_process(market, hours, move):
    """The planner's chance of price trading 1–8% lower before exit vs brute force on the real process."""
    _, (v_now, _, _), m = market
    dist = -math.log1p(-move)
    model = _model_touch(m, hours, dist)
    truth = _true_touch(v_now, hours, dist)
    assert abs(model - truth) <= max(0.02, 0.15 * truth), (model, truth)


def test_tail_odds_hold_up_when_the_market_is_already_volatile():
    """After a volatility spike the 1-week, 1-in-100 tail must stay in the right range. How long volatility
    lingers is only known to about ±0.005 in persistence from 200 days of candles, which moves a 1-week 1%
    tail by up to ~2.5× either way: the reason each simulated path draws its own persistence from the
    likelihood. This seed is one of the unlucky ones (persistence estimated 0.968 vs 0.98), so the bound is loose."""
    candles, (v, px, t) = _garch_t(5000, seed=21)
    m = fit_model(candles)
    # a −4% hour and a −3% hour the fit hasn't seen, then the market carries on by its own rules
    v_true, closes = v, []
    for drop in (0.96, 0.97):
        v_true = _w(VOL) + ALPHA * math.log(drop) ** 2 + BETA * v_true
        px *= drop
        closes.append(px)
    rest, (v_true, px, t_end) = _garch_t(4, seed=99, state=(v_true, px, t + 2 * 3_600_000))
    shock = Candles(3600, np.concatenate([[t, t + 3_600_000], rest.t]).astype(np.int64),
                    np.concatenate([closes, rest.close]), np.concatenate([[c * 0.997 for c in closes], rest.low]),
                    np.concatenate([[c * 1.03 for c in closes], rest.high]))
    v0 = m.nowcast(shock, None, t_end)
    assert v0 / v_true == pytest.approx(1, abs=0.35)
    assert math.sqrt(v_true) / VOL > 1.4                             # genuinely a high-volatility moment
    dist = _dist_for(v_true, 168, 0.01)
    truth = _true_touch(v_true, 168, dist, n=40_000)
    model = _model_touch(m, 168, dist, v0=v0, start_ms=t_end)
    assert truth / 3 <= model <= 3 * truth, (model, truth)


def test_a_move_since_the_last_fit_raises_the_odds(market):
    """A −3% hour the fit hasn't seen must count: the reviewer measured 0.39% vs 5.7% before the nowcast."""
    candles, (v, px, t), m = market
    crash = Candles(3600, np.array([t], dtype=np.int64), np.array([px * 0.97]), np.array([px * 0.965]),
                    np.array([px * 1.001]))
    v0 = m.nowcast(crash, px * 0.97, t + 3_600_000)
    assert v0 > 3 * m.var_next
    calm = liq_probability(simulate(m, 24, m.var_next), 1, 100.0, 10, maintenance_rate(40))
    after = liq_probability(simulate(m, 24, v0), 1, 100.0, 10, maintenance_rate(40))
    assert after > 4 * max(calm, 0.002)
    # the move so far inside the forming candle counts too
    partial = m.nowcast(None, px * 0.98, m.last_t + 2 * 3_600_000 + 1_800_000)
    assert partial > 2 * m.var_next
    # …but a move across a gap of many missing candles is spread over them, not taken as one candle's crash
    spread = m.nowcast(None, px * 0.98, m.last_t + 50 * 3_600_000)
    assert spread < partial and spread < 3 * m.var_long


def test_time_of_day_is_taken_into_account():
    """A market with a strong daily cycle: 4h odds starting into the busy hours vs into the quiet ones. The
    5-minute model borrows the cycle from 200 days of hourly candles, as the live service does."""
    h1, _ = _garch_t(4800, seed=7, amp=0.6)
    hourly = fit_model(h1)
    peak, trough = hourly.season[14], hourly.season[2]
    assert 3.2 < peak / trough < 4.8                                  # true 1.6 / 0.4 = 4
    c5, state = _garch_t(5000, seed=8, step_s=300, vol=VOL / math.sqrt(12), amp=0.6)
    m = fit_model(c5, season=hourly.season)
    v = state[0]
    start_day = state[2] // 86_400_000 + 1
    for start_hour in (11, 23):                                       # 3h before the peak / before the trough
        start = start_day * 86_400_000 + start_hour * 3_600_000
        f_true = float(_diurnal(start, 0.6)) / math.sqrt(float(np.mean(_diurnal(c5.t, 0.6) ** 2)))
        v_model = v * (f_true / float(m.season[season_bucket(start)])) ** 2   # same raw volatility at the start
        dist = _dist_for(v, 48, 0.05, start_t=start, step_s=300, vol=VOL / math.sqrt(12), amp=0.6)
        truth = _true_touch(v, 48, dist, start_t=start, step_s=300, vol=VOL / math.sqrt(12), amp=0.6, n=30_000)
        model = _model_touch(m, 4, dist, v0=v_model, start_ms=start)
        assert abs(model - truth) <= max(0.015, 0.35 * truth), (start_hour, model, truth)


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


def _plan(m, paths, side=1, leverage=10, max_leverage=40, **kw):
    args = dict(coin="BTC", side=side, margin=1_000, leverage=leverage, hours=24, entry=100.0, paths=paths, model=m,
                max_leverage=max_leverage, entry_slip_bps=1.0, exit_slip_bps=1.0, funding_now=0.0000125,
                funding_avg=0.0000125)
    args.update(kw)
    return make_plan(**args)


def test_liquidation_odds_rise_with_leverage_and_the_plan_adds_up(market):
    _, _, m = market
    paths = simulate(m, 24, liq=(1, liq_distance(100.0, 1, 10, maintenance_rate(40))))
    probs = [liq_probability(paths, 1, 100.0, lev, maintenance_rate(40)) for lev in (2, 5, 10, 20, 40)]
    assert probs == sorted(probs) and probs[0] == 0.0 and probs[-1] > 0.3
    plan = _plan(m, paths)
    assert plan.notional == 10_000 and plan.costs.fees == pytest.approx(9.0)
    assert plan.costs.slippage == pytest.approx(2.0) and plan.costs.funding == pytest.approx(0.0000125 * 24 * 10_000)
    assert plan.breakeven_pct == pytest.approx(plan.costs.total / 100)
    assert plan.liq_prob == pytest.approx(liq_probability(paths, 1, 100.0, 10, maintenance_rate(40)))
    assert liq_probability(paths, 1, 100.0, plan.safe_leverage["1"], plan.maint) <= 0.01
    assert plan.safe_leverage["1"] <= plan.safe_leverage["5"]
    assert plan.outcome["p5"] < plan.outcome["p50"] < plan.outcome["p95"] and 0.3 < plan.prob_profit < 0.55
    assert plan.drawdown[1].move_pct < plan.drawdown[0].move_pct < 0                 # a long's worst moves are down
    against = [t for t in plan.touches if t.kind == "against"]
    assert against and all(t.move_pct < 0 and t.pnl < 0 for t in against)
    assert [t.prob for t in against] == sorted((t.prob for t in against), reverse=True)


def test_high_leverage_drawdown_stops_at_liquidation_and_targets_need_you_still_in(market):
    _, _, m = market
    dist = liq_distance(100.0, 1, 40, maintenance_rate(40))
    paths = simulate(m, 24, liq=(1, dist))
    plan = _plan(m, paths, leverage=40)
    assert plan.liq_prob > 0.3
    worst = plan.drawdown[1]
    assert worst.liquidated and worst.pnl == pytest.approx(-1_000 - 0.00045 * 40_000 - 4.0 - plan.costs.funding)
    assert all(d.pnl >= worst.pnl for d in plan.drawdown)            # nothing worse than being liquidated
    for t in (t for t in plan.touches if t.kind == "for"):
        unconditional = float(np.mean(paths.high >= math.log1p(t.move_pct / 100)))
        assert t.prob < unconditional                                # some paths are liquidated before getting there


def test_a_short_mirrors_a_long_without_a_directional_view(market):
    _, _, m = market
    kw = dict(leverage=5, entry_slip_bps=0, exit_slip_bps=0, funding_now=None, funding_avg=None)
    long = _plan(m, simulate(m, 24, liq=(1, liq_distance(100.0, 1, 5, maintenance_rate(40)))), side=1, **kw)
    short = _plan(m, simulate(m, 24, liq=(-1, liq_distance(100.0, -1, 5, maintenance_rate(40)))), side=-1, **kw)
    assert abs(long.prob_profit - short.prob_profit) < 0.05
    assert abs(abs(long.drawdown[0].move_pct) - abs(short.drawdown[0].move_pct)) < 0.3
    assert short.liq_price > 100.0 > long.liq_price
    assert short.drawdown[0].move_pct > 0 and short.drawdown[0].pnl < 0


def test_unknown_max_leverage_is_cautious_and_says_so(market):
    _, _, m = market
    plan = _plan(m, simulate(m, 24), leverage=5, max_leverage=None)
    assert plan.max_leverage == 10 and plan.maint == 0.05 and not plan.max_leverage_known
    assert any("hasn't loaded" in n for n in plan.notes)


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
    assert m.nowcast(None, 100.0, 10**12) == m.var_next


def test_bars_aggregate_into_complete_candles_only():
    t0 = 1_790_000_000_000 // 300_000 * 300_000
    bars = [Bar(timestamp=t0 + i * 60_000, span_s=60, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i)
            for i in range(10)]
    bars += [Bar(timestamp=t0 + 15 * 60_000 + i * 60_000, span_s=60, open=1, high=2, low=0.5, close=1.5)
             for i in range(5)]                                 # after a 5-minute gap
    c = candles_from_bars(bars + [Bar(timestamp=t0 + 20 * 60_000, span_s=60, open=1, high=1, low=1, close=1)], 300)
    assert len(c) == 1 and c.close[0] == 1.5 and c.t[0] == t0 + 15 * 60_000


def test_season_buckets_split_weekday_and_weekend_hours():
    sat_3am = 1_790_000_000_000 // 86_400_000 * 86_400_000
    while (sat_3am // 86_400_000 + 3) % 7 != 5:
        sat_3am += 86_400_000
    assert season_bucket(sat_3am + 3 * 3_600_000) == 24 + 3
    assert season_bucket(sat_3am - 86_400_000 + 3 * 3_600_000) == 3      # Friday


def _bracket_plan(m, side, stop, target, leverage=3, hours=72):
    from engine.planner import bracket_distances
    dist = liq_distance(100.0, side, leverage, maintenance_rate(40))
    paths = simulate(m, hours, liq=(side, dist), bracket=bracket_distances(side, stop, target))
    return make_plan(coin="X", side=side, margin=1_000, leverage=leverage, hours=hours, entry=100.0, paths=paths,
                     model=m, max_leverage=40, entry_slip_bps=1, exit_slip_bps=1, funding_now=0.0, funding_avg=0.0,
                     stop_pct=stop, target_pct=target)


@pytest.mark.parametrize("side", [1, -1])
def test_a_bracket_without_an_edge_wins_in_proportion_to_its_distances(market, side):
    """No directional view: a 2% stop with a 4% target should be won about a third of the decided trades
    (the gambler's-ruin odds), a 3%/3% bracket about half, and the average result is just minus the costs."""
    _, _, m = market
    b = _bracket_plan(m, side, 2.0, 4.0).bracket
    assert b.p_target + b.p_stop + b.p_liq + b.p_time == pytest.approx(1.0)
    decided = b.p_target / (b.p_target + b.p_stop)
    assert 0.29 < decided < 0.37, decided
    assert b.breakeven_win == pytest.approx(-b.pnl_stop / (b.pnl_target - b.pnl_stop))
    assert b.breakeven_win > decided - 0.02                    # no edge: the odds don't beat break-even
    costs = 2 * 0.00045 * 3_000 + 2 * 1e-4 * 3_000
    assert abs(b.ev + costs) < 0.15 * 3_000 * 0.02             # within noise of "you pay the costs"
    sym = _bracket_plan(m, side, 3.0, 3.0).bracket
    assert 0.44 < sym.p_target / (sym.p_target + sym.p_stop) < 0.53     # a tie, minus the cautious wick rule
    assert b.stop_price == pytest.approx(100.0 * (0.98 if side > 0 else 1.02))
    assert b.target_price == pytest.approx(100.0 * (1.04 if side > 0 else 0.96))


def test_a_stop_past_liquidation_never_fires_and_the_plan_says_so(market):
    _, _, m = market
    plan = _bracket_plan(m, 1, 30.0, 5.0, leverage=10, hours=168)     # liquidation ~9.8% below, stop at 30%
    b = plan.bracket
    assert b.stop_beyond_liq and b.p_stop == 0 and b.p_liq > 0 and b.pnl_stop is None
    assert any("past the liquidation price" in n for n in plan.notes)
    alone = _bracket_plan(m, 1, None, 5.0)                             # a target with no stop
    assert alone.bracket.p_stop == 0 and alone.bracket.p_target > 0


def test_cross_margin_uses_the_whole_account(market):
    """Cross margin, one position: liquidation is where the account (not just the margin) runs out (the same
    price as an isolated position at account ÷ notional leverage), and a liquidation takes the account."""
    _, _, m = market
    m40 = maintenance_rate(40)
    assert liquidation_price(100.0, 1, 5, m40, ratio=3.0) == pytest.approx(liquidation_price(100.0, 1, 5 / 3, m40))
    assert liquidation_price(100.0, 1, 2, m40, ratio=3.0) is None             # the account covers any fall
    paths = simulate(m, 72, liq=(1, liq_distance(100.0, 1, 20, m40, 3.0)))
    kw = dict(coin="X", side=1, margin=1_000, leverage=20, hours=72, entry=100.0, paths=paths, model=m,
              max_leverage=40, entry_slip_bps=0, exit_slip_bps=0, funding_now=0.0, funding_avg=0.0)
    cross = make_plan(**kw, account=3_000)
    isolated = make_plan(**{**kw, "paths": simulate(m, 72, liq=(1, liq_distance(100.0, 1, 20, m40)))})
    assert cross.margin_mode == "cross" and cross.equity == 3_000
    assert cross.liq_price < isolated.liq_price and cross.liq_prob < isolated.liq_prob
    liq_row = next(t for t in cross.touches if t.kind == "liq")
    assert liq_row.pnl <= -3_000
    assert cross.safe_leverage["1"] >= isolated.safe_leverage["1"]
    assert any(n.startswith("Cross margin") for n in cross.notes)


def test_the_price_cone_and_tail_risk_add_up(market):
    _, _, m = market
    paths = simulate(m, 72, liq=(1, liq_distance(100.0, 1, 10, maintenance_rate(40))))
    plan = make_plan(coin="X", side=1, margin=1_000, leverage=10, hours=72, entry=100.0, paths=paths, model=m,
                     max_leverage=40, entry_slip_bps=1, exit_slip_bps=1, funding_now=0.0, funding_avg=0.0)
    fan = np.array(plan.fan)
    assert fan[0, 0] == 0 and fan[-1, 0] == pytest.approx(72) and len(fan) <= 50
    assert np.all(np.diff(fan[:, 1:], axis=1) >= 0)                        # 5th ≤ 25th ≤ … ≤ 95th at every moment
    assert np.all(np.diff(fan[1:, 5] - fan[1:, 1]) > -0.5)                  # the cone widens with time (± noise)
    assert fan[-1, 1] == pytest.approx(100 * math.exp(np.percentile(paths.final, 5)))
    curve = np.array(plan.liq_curve)
    assert np.all(np.diff(curve[:, 1]) >= 0) and curve[-1, 1] == pytest.approx(plan.liq_prob)
    t = plan.tail
    assert t["es5"] <= t["p1"] + 1e-9 or t["es5"] <= plan.outcome["p5"]
    assert t["p1"] <= plan.outcome["p5"] and 0 <= t["p_lose_half"] <= 1


def test_square_root_impact_beyond_the_book():
    from engine.planner import sqrt_impact_bps
    # $1M into a $100M/day market with 3% daily volatility: 0.7 × 300 bps × √0.01 = 21 bps
    assert sqrt_impact_bps(1e6, 1e8, 0.03) == pytest.approx(21.0)
    assert sqrt_impact_bps(4e6, 1e8, 0.03) == pytest.approx(42.0)          # 4× the size, 2× the cost
    assert sqrt_impact_bps(1e6, 0.0, 0.03) is None


def test_walk_forward_check_is_honest_on_a_known_market():
    """On the unseen 40% of a known market, the levels the model gave under 10% are reached about as often as
    it said, and its 90% range holds about 90% of the time. Too little history: no check."""
    from engine.planner import calibrate
    c, _ = _garch_t(5000, seed=3, amp=0.3)
    cal = calibrate(c, 24)
    assert cal.starts > 150 and 80 < cal.days < 90
    assert cal.tail_n > 500 and abs(cal.tail_happened - cal.tail_predicted) < 0.5 * cal.tail_predicted + 0.005
    assert 0.84 <= cal.range_coverage <= 0.95
    for b in cal.bins:
        if b.n >= 300:
            assert abs(b.happened - b.predicted) < 0.06 + 0.25 * b.predicted, b
    short, _ = _garch_t(400, seed=4)
    assert calibrate(short, 24) is None

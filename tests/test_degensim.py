"""The degen simulation (scripts/degenSim.py): the weekly 2x-or-half game, the made-up weeks, the edge cuts."""
import numpy as np

from scripts.degenSim import DAY_MS, HOLD_S, by_day, hl_res, make_weeks, play, report, shifted


def test_the_game_stops_at_2x_or_half():
    idx = np.array([[0, 1, 2, -1], [3, 3, 3, 3], [4, -1, -1, -1]])
    res = np.array([0.5, 0.5, 0.5, -0.3, 0.1])            # results on margin
    s = play(res, idx, 1.0)
    # week 1: 1.5, 2.25 → 2x at the 2nd trade; week 2: 0.7, 0.49 → half at the 2nd; week 3: 1.1, neither
    assert s["up"] == 1 / 3 and s["down"] == 1 / 3 and s["none"] == 1 / 3
    assert abs(s["none_pot"] - 1.1) < 1e-9 and abs(s["down_pot"] - 0.49) < 1e-9
    assert play(np.array([-1.0]), np.array([[0]]), 1.0)["down_pot"] == 0.0     # a full bust overshoots -50%


def test_a_fair_coin_doubles_about_as_often_as_it_halves_but_swings_cost():
    rng = np.random.default_rng(1)
    idx = rng.integers(0, 5000, (4000, 400))
    fair = play(rng.choice([0.1, 1 / 1.1 - 1], 5000), idx, 1.0)     # ×1.1 or ÷1.1: fair in doubling-vs-halving terms
    assert abs(fair["up"] - fair["down"]) < 0.06
    even = play(rng.choice([0.1, -0.1], 5000), idx, 1.0)            # +10% or -10%: no edge on average...
    assert even["down"] > even["up"] + 0.2                         # ...but the swings drag it down (×1.1×0.9 = 0.99)


def test_days_take_one_trade_at_a_time_and_weeks_mix_days():
    d = 20_000
    t = lambda day, s: {"t": day * DAY_MS + s * 1000, "coin": "BTC", "bps": 1.0, "adv": 0.0}
    days = by_day([t(d, 0), t(d, 100), t(d, HOLD_S + 10), t(d + 1, 5)], [d, d + 1, d + 2])
    assert [len(x) for x in days] == [2, 1, 0]               # the one 100s in was skipped (the first still open)
    idx = make_weeks(days, 50)
    assert idx.shape[0] == 50 and idx.max() <= 2 and (idx >= -1).all()


def test_edge_cuts_and_liquidation():
    tr = [{"t": 0, "coin": "BTC", "bps": 10.0, "adv": 2.0}, {"t": 1, "coin": "BTC", "bps": -4.0, "adv": 8.0}]
    half = shifted(tr, 0.5)
    assert half[0]["bps"] == 8.5 and half[1]["adv"] == 9.5    # the average edge was +3, half of it (1.5) taken off
    assert np.mean([x["bps"] for x in shifted(tr, 0.0)]) == 0
    assert hl_res(10, 130, "BTC", 3) == -1.0                 # 40x: maintenance margin is 125 bps away
    assert abs(hl_res(10, 50, "BTC", 3) - 40 * 7e-4) < 1e-12


def test_the_report_runs():
    rng = np.random.default_rng(3)
    day0 = 20_000
    trades = [{"t": (day0 + i // 20) * DAY_MS + (i % 20) * 2000 * 1000, "coin": "ETH",
               "bps": float(rng.normal(8, 30)), "adv": float(abs(rng.normal(10, 10))), "left": 2e6} for i in range(200)]
    lines = report("TEST", trades, list(range(day0, day0 + 10)), 500)
    text = "\n".join(lines)
    assert "Papertrade 200x" in text and "no edge (after costs)" in text

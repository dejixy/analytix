"""The fade check (scripts/fadeCheck.py): which fades a one-at-a-time bot takes, and runs of fades."""
from scripts.degenSim import DAY_MS
from scripts.fadeCheck import place_in_run, row, shadow, taken


def f(t_s, coin="SOL", bps=5.0):
    return {"t": t_s * 1000, "coin": coin, "bps": bps, "adv": 1.0}


def test_one_at_a_time_and_one_per_coin():
    tr = [f(0), f(60, "BTC"), f(120), f(1000), f(1000, "BTC")]
    assert taken(tr, per_coin=False) == [True, False, False, True, False]
    assert taken(tr, per_coin=True) == [True, True, False, True, True]


def test_runs_of_fades_on_a_coin():
    tr = [f(0), f(300), f(600), f(2000), f(100, "BTC")]
    tr.sort(key=lambda t: t["t"])
    assert place_in_run(tr) == [1, 1, 2, 3, 1]


def test_rows_and_the_shadow_line():
    tr = [f(k * 1000 + d * DAY_MS // 1000, c, 5.0 + k) for d in range(3) for k in range(6) for c in ("SOL", "BTC")]
    tr.sort(key=lambda t: t["t"])
    assert "too few" not in row("all", tr)
    line = shadow(tr, ("SOL",), False, 3)
    assert "%" in line

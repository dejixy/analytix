"""The vault study (scripts/vaultStudy.py): returns from vault histories, and the walk-forward persistence test."""
import numpy as np

from scripts.vaultStudy import (DAY_MS, MONTH_MS, parse_summary, period_return, persistence, portfolio, series,
                                steps)


def test_parses_the_list_and_the_history():
    v = parse_summary({"apr": 0.2, "summary": {"name": "Growi HF ", "vaultAddress": "0xABC", "tvl": "13635922.3",
                                               "createTimeMillis": 1, "isClosed": False,
                                               "relationship": {"type": "normal"}}})
    assert v["address"] == "0xabc" and v["name"] == "Growi HF" and v["tvl"] > 1e7 and v["rel"] == "normal"
    assert parse_summary({"summary": {}}) is None
    d = {"portfolio": [["day", {}], ["allTime", {"accountValueHistory": [[1, "100"], [2, "110"], [3, "120"]],
                                                 "pnlHistory": [[1, "0"], [2, "10"], [3, "20"]]}]]}
    t, av, pnl = series(portfolio(d, "allTime"))
    assert list(t) == [1, 2, 3] and list(av) == [100, 110, 120] and list(pnl) == [0, 10, 20]
    assert series(portfolio(d, "month")) is None


def test_deposits_dont_count_as_returns():
    t = np.array([0, 1, 2, 3]) * DAY_MS
    av = np.array([10_000, 11_000, 111_000, 122_100.0])        # +10%, then a $100k deposit, then +10%
    pnl = np.array([0, 1_000, 1_000, 12_100.0])
    r = steps(t, av, pnl)
    assert np.allclose(r, [0.1, 0.0, 0.1])
    assert abs(period_return(t, r, 0, 3 * DAY_MS) - 0.21) < 1e-9
    assert abs(period_return(t, r, DAY_MS, 3 * DAY_MS) - 0.1) < 1e-9


def vaults(persist: bool, n=60, months=14, seed=1):
    """n vaults with a daily history. With persist, each has its own steady skill; without, months are random."""
    g = np.random.default_rng(seed)
    now = months * MONTH_MS
    t = np.arange(0, now + 1, DAY_MS)
    out = {}
    for i in range(n):
        skill = g.normal(0, 0.004) if persist else 0.0
        r = skill + g.normal(0, 0.01, len(t) - 1)
        av = 100_000 * np.concatenate([[1], np.cumprod(1 + r)])
        pnl = np.concatenate([[0], np.cumsum(np.diff(av))])
        out[f"v{i}"] = (t, av, pnl, steps(t, av, pnl))
    return out, now


def test_skill_that_repeats_is_found():
    v, now = vaults(True)
    s = persistence(v, now, MONTH_MS)
    assert s["m"] >= 10 and s["corr"] > 0.3 and s["corr_t"] > 3 and s["top"] > 0 and s["bottom"] < 0


def test_luck_is_not():
    v, now = vaults(False)
    s = persistence(v, now, MONTH_MS)
    assert abs(s["corr"]) < 0.15 and abs(s["corr_t"]) < 2.5

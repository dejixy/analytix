"""The follow check (scripts/followCheck.py): fades, follows and the control from the live basket's results."""
from scripts.followCheck import groups, stats


def r(score, bps, t=0):
    return {"score": score, "t_ms": t, "result": {"bps": bps, "adv": 1.0}}


def test_following_is_the_fades_opposite():
    g = groups([r(-9, 10), r(8, 4), r(None, 2), r(1, -3)])
    assert g["fade"] == [(0, 10)] and g["follow"] == [(0, -4)] and g["no score"] == [(0, 2)]
    assert [b for _, b in g["every"]] == [10, 4, 2, -3]


def test_stats_count_days():
    s = stats([(0, 10.0), (1, -2.0), (86_400_000, -5.0)])
    assert s["n"] == 3 and abs(s["bps"] - 1.0) < 1e-9 and s["days"] == 2 and s["days_up"] == 1 and s["maker"] == -2.0
    assert stats([]) == {"n": 0}

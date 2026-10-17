import numpy as np

from engine.bigmovers import Arrays, alerts, bankroll, bootstrap, one_at_a_time, pick, scores
from engine.walletstudy import Episode


def eps():
    out = []
    for k in range(30):                                         # 0xbig: 30 decisions, each followed by +8 bps
        out.append(Episode("ETH", "0xbig", 1_000_000 + k * 60_000, 1, 2000))
    out.append(Episode("ETH", "0xbig", 10_000_000, -1, 2000))
    out.append(Episode("ETH", "0xother", 10_010_000, -1, 2000))  # 10s later, same side: same alert
    out.append(Episode("ETH", "0xother", 10_100_000, -1, 2000))  # 90s later: a new alert
    out.append(Episode("BTC", "0xbig", 10_005_000, -1, 2000))    # other coin: its own alert
    return out


def test_scores_use_only_decisions_whose_window_closed_before_the_cutoff():
    a = Arrays.of(eps())
    mo = np.where(a.wallet == 0, 8.0 + np.sin(np.arange(len(a.t))), np.nan)
    sc = scores(a, mo, until_ms=1_000_000 + 29 * 60_000 + 61_000, h=60)      # the 30th decision's window just closed
    assert sc["n"][0] == 30 and 7 < sc["mean"][0] < 9
    sc = scores(a, mo, until_ms=1_000_000 + 29 * 60_000 + 60_000, h=60)      # one second short: 29
    assert sc["n"][0] == 29
    assert list(pick(scores(a, mo, 10**12, 60), 20, 2.5, 6.0)) == [0]


def test_decisions_on_one_coin_and_side_within_30s_are_one_alert():
    a = Arrays.of(eps())
    idx = np.nonzero(a.t >= 10_000_000)[0]
    als = alerts(a, idx)
    assert [(x.coin, x.side, x.count) for x in als] == [("ETH", -1, 2), ("BTC", -1, 1), ("ETH", -1, 1)]


def test_one_trade_at_a_time_skips_alerts_during_an_open_trade():
    from engine.bigmovers import Alert
    als = [Alert("ETH", t, 1, 0) for t in (0, 20_000, 40_000, 100_000)]
    taken = one_at_a_time(als, np.ones(4, bool), lag=5, hold=30)
    assert list(taken) == [0, 2, 3]                                 # 20s one arrives while the first (to 35s) is open
    assert list(one_at_a_time(als, np.ones(4, bool), 5, 30, cap=1)) == [0]


def test_bankroll_and_bootstrap_agree_on_the_rules():
    r = np.array([0.5, -1.0, -1.0, -1.0, -1.0, -1.0, 0.5])
    fixed = bankroll(r, 15, 0.2, compound=False)                   # $3 a trade: +1.5, then five busts → 1.5 left, stops
    assert fixed["bust"] and abs(fixed["end"] - 1.5) < 1e-9
    comp = bankroll(r, 15, 0.2, compound=True)
    assert not comp["bust"] and comp["end"] < 15
    b = bootstrap(np.array([0.3, -0.1]), 15, 0.2, False, trades=100, runs=200)
    assert b["up"] > 0.9 and b["bust"] == 0.0
    b = bootstrap(np.array([-1.0]), 15, 0.2, False, trades=10, runs=10)
    assert b["bust"] == 1.0 and abs(b["median"]) < 1e-9


def test_decisions_keep_when_they_could_first_be_acted_on():
    from engine.bigmovers import Decisions
    d = Decisions(min_usd=1000, keep_first_usd=100)
    d.add("ETH", "w", 0, 1, 400)
    d.add("ETH", "w", 0, 1, 300)              # the same market order taking a second level
    d.add("ETH", "w", 8000, 1, 500)           # reaches $1k here, 8s after it started
    d.add("ETH", "w", 9000, -1, 50)           # turns around: a new decision, too small to keep
    d.add("ETH", "w", 100_000, 1, 2000)       # one order of $2k: known at once
    d.add("BTC", "x", 5000, 1, 150)           # never reaches $1k, but its first order is $100+
    got = d.finish()
    assert [got.coins[c] for c in got.coin] == ["ETH", "BTC", "ETH"]
    assert got.t_first.tolist() == [0, 5000, 100_000]
    assert got.usd_first.tolist() == [700, 150, 2000]
    assert got.t_cross.tolist() == [8000, -1, 100_000]
    assert got.usd.tolist() == [1200, 150, 2000]
    assert got.side.tolist() == [1, 1, 1] and [got.wallets[w] for w in got.wallet] == ["w", "x", "w"]


def test_scores_only_use_values_known_before_the_day():
    from engine.bigmovers import score_by_wallet
    w = np.array([0, 0, 0, 1])
    v = np.array([2.0, 4.0, 100.0, 5.0])
    ready = np.array([10, 20, 30, -1])        # the third isn't known until 30; the last is never usable
    sc = score_by_wallet(w, v, ready, 25, 2)
    assert sc["n"].tolist() == [2, 0] and sc["mean"][0] == 3.0

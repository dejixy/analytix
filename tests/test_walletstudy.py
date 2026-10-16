import numpy as np

from engine.holdclock import Grid, papertrade_kept
from engine.walletstudy import Episodes, Marks, follow_table, kept, taker_of, wallet_stats


def test_one_wallets_burst_on_one_side_is_one_decision():
    eb = Episodes()
    eb.add("ETH", "0xa", 1_000, 1, 500)
    eb.add("ETH", "0xa", 20_000, 1, 700)          # 19s later, same side: same decision
    eb.add("ETH", "0xa", 25_000, -1, 300)         # flips: a new one
    eb.add("ETH", "0xa", 90_000, -1, 100)         # 65s gap: a new one
    eb.add("BTC", "0xa", 1_500, 1, 900)           # other coin: its own
    eps = eb.finish()
    assert [(e.coin, e.side, e.usd, e.fills) for e in eps] == [
        ("ETH", 1, 1200, 2), ("BTC", 1, 900, 1), ("ETH", -1, 300, 1), ("ETH", -1, 100, 1)]


def test_the_taker_is_the_buyer_on_B_and_the_seller_on_A():
    assert taker_of({"side": "B", "users": ["0xbuy", "0xsell"]}) == ("0xbuy", 1)
    assert taker_of({"side": "A", "users": ["0xbuy", "0xsell"]}) == ("0xsell", -1)
    assert taker_of({"side": "B"}) is None


def test_vector_cut_matches_papertrades_formula():
    for mv in (1e-5, 3e-4, 2e-3):
        assert abs(kept(np.array([mv]), "ETH")[0] - papertrade_kept(mv, "ETH")) < 1e-12
    assert kept(np.array([1e-5]), "ETH")[0] == 0.0                  # inside the deadband pays nothing


def market(n=40_000, seed=3):
    """A random walk where wallet 0xsmart buys/sells 20s before every 8 bps jump, and 0xnoise trades at random."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.3e-4, n)
    jumps = np.arange(300, n - 400, 400)
    sides = rng.choice([-1, 1], len(jumps))
    r[jumps] += sides * 8e-4
    mid = 2500 * np.exp(np.cumsum(r))
    g = Grid(np.arange(n) + 1_700_000_000, mid * (1 - 2e-6), mid * (1 + 2e-6), mid, np.ones(n, bool))
    eb = Episodes()
    for j, s in zip(jumps, sides):
        eb.add("ETH", "0xsmart", (int(g.t[j]) - 20) * 1000, int(s), 5_000)
        k = rng.integers(0, n - 400)
        eb.add("ETH", "0xnoise", int(g.t[k]) * 1000, int(rng.choice([-1, 1])), 5_000)
    return g, eb.finish()


def test_an_early_wallet_scores_well_and_following_it_pays():
    g, eps = market()
    marks = {"ETH": Marks(g)}
    st = wallet_stats(eps, marks, 60)
    assert st["0xsmart"]["t"] > 5 and st["0xsmart"]["mean"] > 5
    assert abs(st["0xnoise"]["t"]) < 3
    smart = follow_table([e for e in eps if e.wallet == "0xsmart"], marks, lags=(2,), horizons=(60,))[(2, 60)]
    noise = follow_table([e for e in eps if e.wallet == "0xnoise"], marks, lags=(2,), horizons=(60,))[(2, 60)]
    assert smart["mid"] > 5 and smart["win"] > 0.8 and smart["pt"][100] > 0
    assert abs(noise["mid"]) < 3

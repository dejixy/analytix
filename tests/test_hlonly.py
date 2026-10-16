import numpy as np

from engine.holdclock import Grid
from engine.hlonly import moves, venue_series
from engine.walletstudy import Marks, follow_table

T0 = 1_790_000_000


def market(n=3000, alone_at=(500, 1500), together_at=(1000, 2000)):
    hl = np.full(n, 100.0)
    bn = np.full(n, 100.0)
    for k in alone_at:                      # Hyperliquid alone: +10 bps for 60s, then back
        hl[k:k + 60] *= 1.001
    for k in together_at:                   # everyone: +10 bps and stays
        hl[k:] *= 1.001
        bn[k:] *= 1.001
    g = Grid(np.arange(n) + T0, hl * (1 - 1e-6), hl * (1 + 1e-6), hl, np.ones(n, bool))
    rows = {T0 + s: float(bn[s]) for s in range(n)}
    return g, [venue_series(rows), venue_series(rows)]


def test_lone_and_confirmed_moves_are_told_apart():
    g, venues = market()
    got = moves("ETH", g, venues, window=3, min_bps=5)
    # the jump and, 60s later, the snap back (also Hyperliquid alone)
    assert [(e.t_ms // 1000 - T0, e.side) for e in got["alone"]] == [(500, 1), (560, -1), (1500, 1), (1560, -1)]
    assert all(9 < e.usd < 11 for e in got["alone"])
    assert [e.t_ms // 1000 - T0 for e in got["confirmed"]] == [1000, 2000]


def test_a_stale_venue_never_counts_as_flat():
    g, venues = market()
    t0, mid, age = venues[0]
    age = age.copy()
    age[450:600] = 99                       # Binance went quiet around the first lone move
    got = moves("ETH", g, [(t0, mid, age), venues[1]], window=3, min_bps=5)
    assert [e.t_ms // 1000 - T0 for e in got["alone"]] == [1500, 1560]


def test_fading_a_lone_move_that_snaps_back_pays():
    g, venues = market()
    alone = [e for e in moves("ETH", g, venues, 3, 5)["alone"] if e.side == 1]       # the jumps
    r = follow_table(alone, {"ETH": Marks(g)}, flip=True, lags=(2,), horizons=(120,), leverages=(200,))[(2, 120)]
    assert r["mid"] > 9 and r["pt"][200] > 0

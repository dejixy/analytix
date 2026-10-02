from engine.levels import BREAK_CONFIRM_MS, MIN_HELD_MS, LevelTracker, defended_level
from models.explanationModel import DefendedLevel
from models.signalModel import Direction
from models.tradeModel import TradeSide
from tests.helpers import T0, trade

MIN = 60_000


def _absorbed_selling(level=3000.0, after_low=None):
    """Sellers hammer 3000.0 for a minute; a few small buys print above it."""
    ts = [trade(T0 + i * 1000, px=level, sz=20, side="A") for i in range(40)]
    ts += [trade(T0 + i * 1000 + 500, px=level + 0.6, sz=2, side="B") for i in range(20)]
    ts += [trade(T0 + 10_000 + i, px=level + 1.5, sz=3, side="A") for i in range(5)]   # some selling higher up
    if after_low is not None:
        ts.append(trade(T0 + 59_000, px=after_low, sz=1, side="A"))
    return sorted(ts, key=lambda t: t.timestamp)


def test_finds_the_price_where_selling_was_absorbed():
    lv = defended_level(_absorbed_selling(), TradeSide.SELL)
    assert lv.side == "bid" and abs(lv.price - 3000.0) < 0.01
    assert abs(lv.absorbed - 40 * 20 * 3000.0) < 1 and lv.share > 0.9


def test_no_level_if_price_later_traded_through_it():
    assert defended_level(_absorbed_selling(after_low=2998.0), TradeSide.SELL) is None   # 6.7 bps below
    assert defended_level(_absorbed_selling(after_low=2999.5), TradeSide.SELL) is not None  # a 1.7 bp wick is fine


def _tracked(created=T0):
    tr = LevelTracker("ETH")
    tr.observe("10m", DefendedLevel("bid", 3000.0, 2e6, 0.5), created, sweep_threshold=100_000)
    return tr


def test_small_levels_and_unknown_windows_are_not_tracked():
    tr = LevelTracker("ETH")
    tr.observe("10m", DefendedLevel("bid", 3000.0, 2e5, 0.5), T0, sweep_threshold=100_000)   # < 5 sweeps' worth
    tr.observe("6h", DefendedLevel("bid", 3000.0, 2e7, 0.5), T0, sweep_threshold=100_000)
    assert tr.active() == []


def test_nearby_levels_merge():
    tr = _tracked()
    tr.observe("60m", DefendedLevel("bid", 3000.9, 3e6, 0.4), T0 + MIN, sweep_threshold=100_000)   # 3 bps away
    (lv,) = tr.active()
    assert lv.price == 3000.0 and lv.absorbed == 3e6 and lv.window == "60m"


def test_a_level_breaks_only_after_price_stays_through_it():
    tr = _tracked()
    t = T0 + MIN_HELD_MS + MIN
    assert tr.check(t, 2998.0, sigma_1s_bps=0.5) == []                      # through it, not yet confirmed
    assert tr.check(t + 5_000, 3000.5, 0.5) == []                           # back above: the clock resets
    tr.check(t + 10_000, 2998.0, 0.5)
    (ev,) = tr.check(t + 10_000 + BREAK_CONFIRM_MS, 2997.5, 0.5)
    assert ev.kind == "level_break" and ev.direction is Direction.DOWN
    assert ev.title == "Bid level 3,000.00 broke — it had absorbed $2.00M of selling"
    assert ev.detail.startswith("10m level · held") and tr.active() == []


def test_a_level_that_gives_way_quickly_breaks_silently():
    tr = _tracked()
    tr.check(T0 + 30_000, 2998.0, 0.5)
    assert tr.check(T0 + 30_000 + BREAK_CONFIRM_MS, 2998.0, 0.5) == [] and tr.active() == []
    assert not tr.events


def test_levels_breaking_together_are_one_event():
    tr = _tracked()
    tr.observe("10m", DefendedLevel("bid", 2995.0, 1e6, 0.3), T0, sweep_threshold=100_000)   # 17 bps lower
    t = T0 + MIN_HELD_MS + MIN
    tr.check(t, 2997.0, 0.5)
    tr.check(t + BREAK_CONFIRM_MS, 2997.0, 0.5)                             # the upper one breaks
    tr.check(t + 15_000, 2993.0, 0.5)
    tr.check(t + 15_000 + BREAK_CONFIRM_MS, 2993.0, 0.5)                    # then the lower one
    (ev,) = tr.events
    assert ev.title == "Bid levels 2,995.00–3,000.00 broke — they had absorbed $3.00M of selling"


def test_a_broken_level_is_not_drawn_as_holding_again():
    tr = _tracked()
    t = T0 + MIN_HELD_MS + MIN
    tr.check(t, 2998.0, 0.5)
    tr.check(t + BREAK_CONFIRM_MS, 2998.0, 0.5)
    tr.observe("10m", DefendedLevel("bid", 3000.1, 2e6, 0.5), t + 20_000, sweep_threshold=100_000)
    assert tr.active() == []

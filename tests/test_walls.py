from engine.walls import MIN_LIFE_MS, WallTracker
from models.bookModel import BookLevel, OrderBook
from signals.base import WallStats
from signals.depthDelta import _wall_note
from tests.helpers import T0, trade


def _book(ts, mid=3000.0, wall_at=None, wall_side="ask", wall_size=400.0):
    """A 10-level book (10 coins a level) with an optional wall."""
    bids = [BookLevel(round(mid - 0.05 - i * 0.1, 2), 10.0, 3) for i in range(10)]
    asks = [BookLevel(round(mid + 0.05 + i * 0.1, 2), 10.0, 3) for i in range(10)]
    side = asks if wall_side == "ask" else bids
    for i, l in enumerate(side):
        if wall_at is not None and abs(l.price - wall_at) < 1e-9:
            side[i] = BookLevel(l.price, wall_size, 1)
    return OrderBook(ts, tuple(bids), tuple(asks))


def test_a_wall_pulled_as_price_nears_it_counts_as_bait():
    tr = WallTracker()
    for s in range(20):                                   # $1.2M ask wall at 3000.55, ~1.8 bps away
        tr.update(_book(T0 + s * 1000, wall_at=3000.55), [], T0 + s * 1000, sweep_threshold=100_000)
    assert len(tr.active(T0 + 19_000)) == 1
    tr.update(_book(T0 + 20_000), [], T0 + 20_000, 100_000)        # gone, nothing traded there…
    assert not tr.exits                                             # …judged after a 2s grace
    tr.update(_book(T0 + 22_000), [], T0 + 22_000, 100_000)
    (ex,) = tr.exits
    assert ex.outcome == "pulled_near" and ex.side == "ask"
    assert tr.stats(T0 + 22_000).pulled_near == {"bid": 0, "ask": 1}


def test_a_wall_traded_into_is_eaten_and_a_tested_one_that_stands_is_held():
    tr = WallTracker()
    for s in range(15):
        tr.update(_book(T0 + s * 1000, wall_at=2999.45, wall_side="bid"), [], T0 + s * 1000, 100_000)
    fills = [trade(T0 + 15_000 + i, px=2999.45, sz=60, side="A") for i in range(2)]   # 120 of 400 coins
    tr.update(_book(T0 + 16_000, wall_at=2999.45, wall_side="bid", wall_size=280), fills, T0 + 16_000, 100_000)
    assert tr.stats(T0 + 16_000).held == {"bid": 1, "ask": 0}
    tr.update(_book(T0 + 18_000), [], T0 + 18_000, 100_000)       # the book shows it gone first…
    more = [trade(T0 + 17_000 + i, px=2999.45, sz=70, side="A") for i in range(4)]
    tr.update(_book(T0 + 19_000), more, T0 + 19_000, 100_000)     # …then that block's fills arrive
    tr.update(_book(T0 + 21_000), [], T0 + 21_000, 100_000)
    assert [e.outcome for e in tr.exits] == ["eaten"] and tr.total_exits == 1


def test_flickering_quotes_are_not_walls():
    tr = WallTracker()
    tr.update(_book(T0, wall_at=3000.55), [], T0, 100_000)
    tr.update(_book(T0 + MIN_LIFE_MS - 1000), [], T0 + MIN_LIFE_MS - 1000, 100_000)
    assert not tr.exits


def test_depth_summary_flags_stacking_that_keeps_getting_pulled():
    w = WallStats(pulled_near={"bid": 0, "ask": 3}, eaten={"bid": 0, "ask": 1}, held={"bid": 0, "ask": 0})
    assert _wall_note(w, bid_chg=0.0, ask_chg=0.94).startswith(" Careful: 3 of the last 4 big ask walls were pulled")
    real = WallStats(pulled_near={"bid": 1, "ask": 0}, eaten={"bid": 2, "ask": 0}, held={"bid": 1, "ask": 0})
    assert "3 of 4 held or got traded into" in _wall_note(real, bid_chg=0.5, ask_chg=0.0)
    assert _wall_note(w, bid_chg=0.0, ask_chg=0.05) == ""          # not stacking: nothing to say


def test_a_wall_that_scrolls_out_of_the_visible_book_is_not_called_pulled():
    tr = WallTracker()
    for s in range(15):
        tr.update(_book(T0 + s * 1000, wall_at=2999.45, wall_side="bid"), [], T0 + s * 1000, 100_000)
    for s in range(15, 20):                       # price rallies 1.5: the wall is now below the 10 visible levels
        tr.update(_book(T0 + s * 1000, mid=3001.5), [], T0 + s * 1000, 100_000)
    assert not tr.exits and tr.active(T0 + 20_000) == []

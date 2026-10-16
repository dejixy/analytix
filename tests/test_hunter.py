from types import SimpleNamespace

import pytest

from buffer.ringBuffer import RingBuffer
from engine.hunter import DEFAULT_EXITS, EXITS, SETUPS, CoinMemory, detect, lead_signal, vol_stop_bps
from models.bookModel import BookSummary
from models.explanationModel import Metric, MarketEvent, PriceMove, Significance
from models.externalModel import ExternalQuote
from models.signalModel import Direction
from models.tradeModel import TradeSide

UP, DOWN, FLAT = Direction.UP, Direction.DOWN, Direction.NEUTRAL


def book(ts, mid, half=0.005):
    return BookSummary(ts, mid, mid - half, mid + half, 2 * half / mid * 1e4, 1e6, 1e6)


def quote(ts, mid):
    return ExternalQuote(ts, "binance", "BTC", mid - 0.005, mid + 0.005)


def state(hl, bn):
    st = SimpleNamespace(books=RingBuffer(max_seconds=3600), external={"binance": RingBuffer(max_seconds=3600)},
                         now_ms=0, book=None, context=None, trades=RingBuffer(max_seconds=3600))
    for ts, mid in hl:
        st.books.push(book(ts, mid))
    for ts, mid in bn:
        st.external["binance"].push(quote(ts, mid))
    return st


def test_lead_fires_when_binance_moves_and_hyperliquid_lags():
    st = state([(1000, 100.0), (3900, 100.01)], [(1000, 100.0), (2000, 100.03), (3900, 100.06)])
    side, b, h = lead_signal(st, 0.3, 3900)
    assert side == 1 and b == pytest.approx(6, abs=0.1) and h == pytest.approx(1, abs=0.1)
    caught_up = state([(1000, 100.0), (3900, 100.05)], [(1000, 100.0), (3900, 100.06)])
    assert lead_signal(caught_up, 0.3, 3900)[0] == 0
    small = state([(1000, 100.0), (3900, 100.0)], [(1000, 100.0), (3900, 100.01)])
    assert lead_signal(small, 0.3, 3900)[0] == 0


def m(key, lean, bar=0.6, tag="", value=""):
    return Metric(key, key.title(), value, tag, lean, bar)


def move(sig=Significance.QUIET, d=FLAT):
    return PriceMove(100, 100, 100, 100, 0.0, 0.0, 1.0, sig, d)


def ex(rows, mv=None):
    return SimpleNamespace(summary=tuple(rows), move=mv or move(), coverage=1.0)


def pipe_with(latest, cascades=(), events=()):
    st = state([(1000, 100.0)], [])
    an = SimpleNamespace(latest=latest, cascades=SimpleNamespace(_items=list(cascades)), market_events=lambda: list(events),
                         baseline=SimpleNamespace(sigma_1s_bps=0.5))
    return SimpleNamespace(state=st, analyzer=an)


def test_setups_fire_once_on_a_new_opportunity_with_the_right_direction():
    mem = CoinMemory()
    p = pipe_with({"1m": ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])})
    assert detect(p, mem, 1000) == {}                          # the first pass only records what's going on
    p.analyzer.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    assert detect(p, mem, 2000) == {"flow": 1}
    assert detect(p, mem, 3000) == {}                          # still lined up: no second trigger
    p.analyzer.latest["1m"] = ex([m("flow", DOWN, value="70% sell · net −$1M"), m("book", DOWN)],
                                 move(Significance.SIGNIFICANT, DOWN))
    assert detect(p, mem, 4000) == {"flow": -1, "winning": -1, "breakout": -1}
    p.analyzer.latest["1m"] = ex([m("flow", DOWN, tag="price held", value="72% sell · net −$2M"), m("book", FLAT)])
    assert detect(p, mem, 5000)["absorb"] == 1                 # heavy selling that isn't moving price: long


def test_winning_side_needs_price_going_the_flows_way_and_the_book_not_against_it():
    mem = CoinMemory()
    p = pipe_with({"1m": ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])})
    detect(p, mem, 1000)
    p.analyzer.latest["1m"] = ex([m("flow", UP, tag="steady"), m("book", DOWN)], move(Significance.NOTABLE, UP))
    assert "winning" not in detect(p, mem, 2000)               # bids thin: the book leans against the buyers
    p.analyzer.latest["1m"] = ex([m("flow", UP, tag="steady"), m("book", FLAT)], move(Significance.QUIET, UP))
    assert "winning" not in detect(p, mem, 3000)               # buyers, but price isn't going anywhere yet
    p.analyzer.latest["1m"] = ex([m("flow", UP, tag="steady"), m("book", FLAT)], move(Significance.NOTABLE, UP))
    assert detect(p, mem, 4000).get("winning") == 1




def test_cascades_fades_and_level_breaks():
    mem = CoinMemory()
    c = SimpleNamespace(id=7, side=TradeSide.SELL, start_ms=9000, end_ms=0, done=False)
    lvl = MarketEvent("lb-1", "level_break", 9500, UP, "broke")
    p = pipe_with({}, [], [])
    detect(p, mem, 1000)
    p.analyzer.cascades._items.append(c)
    assert detect(p, mem, 10_000) == {"cascade": -1}
    c.done, c.end_ms = True, 12_000
    p.analyzer.market_events = lambda: [lvl]
    assert detect(p, mem, 13_000) == {"fade": 1, "level": 1}
    assert detect(p, mem, 14_000) == {}

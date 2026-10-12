from types import SimpleNamespace

import pytest

from api.bot import BotError, BotService, auto_hourly_message
from buffer.ringBuffer import RingBuffer
from engine.hunter import EXITS, SETUPS, CoinMemory, detect, lead_signal
from models.bookModel import BookSummary
from models.explanationModel import Metric, MarketEvent, PriceMove, Significance
from models.externalModel import ExternalQuote
from models.signalModel import Direction
from models.tradeModel import TradeSide

UP, DOWN, FLAT = Direction.UP, Direction.DOWN, Direction.NEUTRAL


def book(ts, mid, half=0.005):
    return BookSummary(ts, mid, mid - half, mid + half, 1.0, 1e6, 1e6)


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
    assert detect(p, mem, 4000) == {"flow": -1, "breakout": -1}
    p.analyzer.latest["1m"] = ex([m("flow", DOWN, tag="price held", value="72% sell · net −$2M"), m("book", FLAT)])
    assert detect(p, mem, 5000)["absorb"] == 1                 # heavy selling that isn't moving price: long


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


# ── the service ─────────────────────────────────────────────────────────────
def runtime():
    st = state([(1000, 100.0)], [])
    an = SimpleNamespace(latest={}, cascades=SimpleNamespace(_items=[]), market_events=lambda: [],
                         baseline=SimpleNamespace(sigma_1s_bps=0.5))
    pipes = {"BTC": SimpleNamespace(state=st, analyzer=an)}
    return SimpleNamespace(coins=["BTC"], pipelines=pipes, mode="replay", planner=None, alerts=None), st, an


def test_auto_hunter_runs_every_setup_and_exit_and_trades_what_fires(tmp_path):
    rt, st, an = runtime()
    svc = BotService(rt, tmp_path / "bot.json")
    assert len(svc.auto_rules) == len(SETUPS) * len(EXITS) and len(svc.sims) == 2 * len(svc.auto_rules)
    st.now_ms = 1000
    an.latest["1m"] = ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])
    svc.tick()                                                   # primes
    st.books.push(book(2000, 100.0))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()
    flow_sims = [s for (rid, _), s in svc.sims.items() if svc.auto_rules[rid].signals[0] == "flow"]
    assert len(flow_sims) == 2 * len(EXITS) and all(s.market_due or s.pending for s in flow_sims)
    assert all(not (s.market_due or s.pending) for (rid, _), s in svc.sims.items() if svc.auto_rules[rid].signals[0] != "flow")
    st.books.push(book(2600, 100.0))
    st.books.push(book(3000, 100.3))                             # +30 bps: market quick and standard targets
    st.now_ms = 3000
    closed = svc.tick()
    assert {t["rule_id"] for t in closed} == {"auto-flow-BTC-quick", "auto-flow-BTC-standard"}
    pub = svc.public()["auto"]
    assert pub["strategies"] == len(SETUPS) * len(EXITS) and pub["last_hour"]["signals"] == 1
    flow_row = next(r for r in pub["setup_stats"] if r["key"] == "flow")
    assert flow_row["market"]["trades"] == 2 and flow_row["signals_1h"] == 1
    assert any(o["name"].startswith("Flow + Book · BTC · Runner") for o in pub["open"])

    # settings: fewer setups keeps the rest's record; new money settings start fresh; bad input is refused
    svc.save_auto({"setups": ["flow", "lead"], "exits": ["quick", "standard"]})
    assert len(svc.auto_rules) == 4 and svc.public()["auto"]["total"]["market"]["trades"] == 2
    svc.save_auto({"margin_usd": 50})
    assert svc.public()["auto"]["total"]["market"]["trades"] == 0
    with pytest.raises(BotError):
        svc.save_auto({"setups": []})
    svc.save_auto({"on": False})
    assert not svc.sims and BotService(rt, tmp_path / "bot.json").auto["on"] is False


def test_auto_hourly_digest():
    t = {"style": "market", "net_usd": 1.5, "exit_ms": 10, "entry_ms": 0, "gross_usd": 2, "fees_usd": 0.5, "net_pct": 1.5,
         "reason": "target"}
    title, lines = auto_hourly_message({"auto-lead-BTC-quick": {"market": [t] * 10, "limit": []}},
                                       {"auto-lead-BTC-quick": "Binance leads · BTC · Quick"}, 0, 100)
    assert "auto hunter" in title and "10 trades" in lines[0] and "Binance leads · BTC · Quick" in lines[2]
    assert auto_hourly_message({"x": {"market": [t]}}, {}, 100, 200) is None

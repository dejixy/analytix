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
    assert len(svc.auto_rules) == len(SETUPS) * len(EXITS)
    assert len(svc.sims) == 3 * len(svc.auto_rules) - len(SETUPS)            # no market orders on Quick: can't cover fees
    assert ("auto-flow-BTC-quick", "market") not in svc.sims and ("auto-flow-BTC-quick", "limit") in svc.sims
    st.now_ms = 1000
    an.latest["1m"] = ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])
    svc.tick()                                                   # primes
    st.books.push(book(2000, 100.0))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()
    flow_sims = [s for (rid, _), s in svc.sims.items() if svc.auto_rules[rid].signals[0] == "flow"]
    assert len(flow_sims) == 3 * len(EXITS) - 1 and all(s.market_due or s.pending for s in flow_sims)
    assert all(not (s.market_due or s.pending) for (rid, _), s in svc.sims.items() if svc.auto_rules[rid].signals[0] != "flow")
    st.books.push(book(2600, 100.0))
    st.books.push(book(3000, 100.3))                             # +30 bps: the market Standard target
    st.now_ms = 3000
    closed = svc.tick()
    assert {(t["rule_id"], t["style"]) for t in closed} == {("auto-flow-BTC-standard", "market"),
                                                         ("auto-flow-BTC-standard", "maker_exit"),
                                                         ("auto-flow-BTC-quick", "maker_exit")}
    pub = svc.public()["auto"]
    assert pub["strategies"] == pub["running"] == 3 * len(SETUPS) * len(EXITS) - len(SETUPS) and pub["last_hour"]["signals"] == 1
    flow_row = next(r for r in pub["setup_stats"] if r["key"] == "flow")
    assert flow_row["market"]["trades"] == 1 and flow_row["signals_1h"] == 1 and flow_row["market"]["gross_usd"] > 0
    assert any(o["name"].startswith("Flow + Book · BTC · Runner") for o in pub["open"])

    # settings: fewer setups keeps the rest's record; new money settings start fresh; bad input is refused
    svc.save_auto({"setups": ["flow", "lead"], "exits": ["quick", "standard"]})
    assert len(svc.auto_rules) == 4 and svc.public()["auto"]["total"]["market"]["trades"] == 1
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
    assert "auto hunter" in title and "10 trades" in lines[0] and "Binance leads · BTC · Quick" in lines[3]
    assert auto_hourly_message({"x": {"market": [t]}}, {}, 100, 200) is None


def test_clear_losers_are_benched_and_can_be_brought_back(tmp_path):
    rt, st, an = runtime()
    svc = BotService(rt, tmp_path / "bot.json")
    key = ("auto-lead-BTC-standard", "market")
    for i in range(20):
        x = -2.0 + (0.5 if i % 2 else -0.5)
        svc._record({"rule_id": key[0], "style": key[1], "net_usd": x, "gross_usd": x + 1.8, "fees_usd": 1.8,
                     "net_pct": x, "entry_ms": i * 1000, "exit_ms": i * 1000 + 500, "reason": "stop"})
        assert (key in svc.sims) == (i < 19)                     # benched on the 20th trade, not before
    pub = svc.public()["auto"]
    assert pub["benched_n"] == 1 and any(b["benched"] for b in pub["best"] + pub["worst"])
    svc.save_auto({"benched": []})
    assert key in svc.sims


def test_an_older_saved_hunter_starts_a_fresh_record(tmp_path):
    rt, st, an = runtime()
    (tmp_path / "bot.json").write_text('{"rules": [], "auto": {"on": true, "exits": ["quick"], "benched": ["x|market"]}}')
    svc = BotService(rt, tmp_path / "bot.json")
    assert svc._auto_fresh and svc.auto["exits"] == list(EXITS) and svc.auto["benched"] == [] and svc.auto["version"] == 3


def test_signals_are_scored_by_where_price_went_after_them(tmp_path):
    rt, st, an = runtime()
    svc = BotService(rt, tmp_path / "bot.json")
    st.now_ms = 1000
    an.latest["1m"] = ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])
    svc.tick()                                                   # primes
    st.books.push(book(2000, 100.0))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()                                                   # Flow + Book fires long at 100.00
    st.books.push(book(12_000, 100.05))
    st.now_ms = 12_500
    svc.tick()
    st.books.push(book(32_000, 99.98))
    st.now_ms = 32_500
    svc.tick()
    tab = {r["key"]: r for r in svc.public()["auto"]["markouts"]}
    after10, after30 = tab["flow"]["all"][0], tab["flow"]["all"][1]
    assert after10["n"] == 1 and after10["avg_bps"] == pytest.approx(5, abs=0.1) and after10["won"] == 1
    assert after30["avg_bps"] == pytest.approx(-2, abs=0.1) and after30["won"] == 0
    assert tab["flow"]["all"][2]["n"] == 0 and tab["flow"]["coins"]["BTC"][0]["n"] == 1
    svc.reset_auto()
    assert all(c["n"] == 0 for c in {r["key"]: r for r in svc.public()["auto"]["markouts"]}["flow"]["all"])


def test_binance_leads_are_scored_by_how_far_binance_moved(tmp_path):
    rt, st, an = runtime()
    st.external = {"binance": RingBuffer(max_seconds=3600)}
    svc = BotService(rt, tmp_path / "bot.json")
    st.now_ms = 1000
    svc.tick()                                                   # primes
    for ts, mid in ((1000, 100.0), (3900, 100.06)):
        st.external["binance"].push(quote(ts, mid))
    st.books.push(book(3900, 100.01))
    st.now_ms = 3900
    svc.tick()                                                   # Binance +6 bps, Hyperliquid +1: lead fires long
    st.books.push(book(13_900, 100.1))
    st.now_ms = 14_000
    svc.tick()
    sizes = {r["label"]: r["cells"][0] for r in svc.public()["auto"]["lead_sizes"]}
    assert sizes["4–7"]["n"] == 1 and sizes["4–7"]["avg_bps"] == pytest.approx(9, abs=0.2) and sizes["2–4"]["n"] == 0
    svc.save_auto({"lead_min_bps": 8})                         # only leads of 8 bps or more from now on
    assert svc.auto["lead_min_bps"] == 8 and sizes["4–7"]["n"] == 1


def test_a_version_2_hunter_keeps_its_record_and_gains_the_timed_exits(tmp_path):
    rt, st, an = runtime()
    (tmp_path / "bot.json").write_text('{"rules": [], "auto": {"on": true, "exits": ["standard"], "version": 2}}')
    svc = BotService(rt, tmp_path / "bot.json")
    assert not svc._auto_fresh and svc.auto["exits"] == ["t30", "t60", "standard"] and svc.auto["version"] == 3
    svc.save_auto({"taker_bps": 3.0, "maker_bps": 0.0})
    r = next(iter(svc.auto_rules.values()))
    assert r.taker_fee == pytest.approx(0.0003) and r.maker_fee == 0.0
    assert svc.public()["fees"] == {"taker": pytest.approx(0.0003), "maker": 0.0}

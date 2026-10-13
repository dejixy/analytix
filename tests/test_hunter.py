from types import SimpleNamespace

import pytest

from api.bot import BotError, BotService, auto_hourly_message
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


# ── the service ─────────────────────────────────────────────────────────────
def runtime():
    st = state([(1000, 100.0)], [])
    an = SimpleNamespace(latest={}, cascades=SimpleNamespace(_items=[]), market_events=lambda: [],
                         baseline=SimpleNamespace(sigma_1s_bps=0.5))
    pipes = {"BTC": SimpleNamespace(state=st, analyzer=an)}
    return SimpleNamespace(coins=["BTC"], pipelines=pipes, mode="replay", planner=None, alerts=None), st, an


def primed(tmp_path):
    rt, st, an = runtime()
    svc = BotService(rt, tmp_path / "bot.json")
    st.now_ms = 1000
    an.latest["1m"] = ex([m("flow", FLAT, 0.1), m("book", FLAT, 0.1)])
    svc.tick()                                                   # the first pass only notes what's already going on
    return svc, rt, st, an


def test_the_hunter_runs_timed_exits_with_market_and_limit_out_by_default(tmp_path):
    svc, rt, st, an = primed(tmp_path)
    assert svc.auto["exits"] == list(DEFAULT_EXITS) and svc.auto["styles"] == ["market", "maker_exit"]
    assert len(svc.auto_rules) == len(SETUPS) * 3 and len(svc.sims) == 2 * len(svc.auto_rules)
    st.books.push(book(2000, 100.0))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()
    flow = {k: s for k, s in svc.sims.items() if svc.auto_rules[k[0]].signals[0] == "flow"}
    assert len(flow) == 6 and all(s.market_due for s in flow.values())
    t300 = flow[("auto-flow-BTC-t300", "market")]
    assert t300.market_due[2] == pytest.approx(vol_stop_bps(0.5, 300))          # a stop three normal moves away
    an.latest["1m"] = ex([m("flow", UP, tag="steady"), m("book", FLAT)], move(Significance.NOTABLE, UP))
    st.now_ms = 5000
    svc.tick()                                                   # Winning side, 3s later, the same way: one event
    pub = svc.public()["auto"]
    assert pub["day"]["events"] == 1 and pub["day"]["signals"] == 2
    for t in (2600, 20_000, 33_000, 36_100):
        st.books.push(book(t, 100.05))
        st.now_ms = t
        svc.tick()
    closed = [t for lst in svc.trades.values() for t in lst]
    assert {(t["rule_id"], t["style"]) for t in closed} >= {("auto-flow-BTC-t30", "market"), ("auto-flow-BTC-t30", "maker_exit")}
    assert all(t["reason"] == "time" for t in closed)


def test_follow_through_is_measured_from_the_price_paid(tmp_path):
    svc, rt, st, an = primed(tmp_path)
    st.books.push(book(2000, 100.0))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()                                                   # long at 2000; a market buy lands at 2500
    st.books.push(book(2400, 100.0, half=0.02))                  # the ask at 2500: 100.02
    st.books.push(book(12_000, 100.05, half=0.02))               # 10s later the bid is 100.03
    st.now_ms = 12_500
    svc.tick()
    cell = {r["key"]: r for r in svc.public()["auto"]["markouts"]}["flow"]["all"][0]
    assert cell["n"] == 1 and cell["avg_bps"] == pytest.approx((100.03 / 100.02 - 1) * 1e4, abs=0.01)
    mid = svc._cell([svc.markouts[("flow", "BTC", 10)]], paid=False)
    assert mid["avg_bps"] == pytest.approx(5.0, abs=0.01)        # the mid says 5 bps; what you'd get is 1


def test_entries_are_skipped_when_the_spread_is_wide_or_the_record_cant_clear_fees(tmp_path):
    svc, rt, st, an = primed(tmp_path)
    for i in range(30):
        st.books.push(book(1100 + i * 10, 100.0, half=0.005))   # usual spread about 1 bp
    svc.markouts[("flow", "BTC", 30)] = [30, 210, 1600, 20, 30.0, 210.0, 1600.0, 20.0]  # paid +7 bps a signal at 30s
    st.books.push(book(2000, 100.0, half=0.005))
    st.now_ms = 2000
    an.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc.tick()
    t30m, t30x = svc.sims[("auto-flow-BTC-t30", "market")], svc.sims[("auto-flow-BTC-t30", "maker_exit")]
    assert t30m.skipped == {"cost": 1} and not t30m.market_due      # 7 bps doesn't cover 9 bps of market fees
    assert t30x.market_due                                           # 7 bps covers 6 (market in, limit out)
    edge = {r["key"]: r for r in svc.public()["auto"]["setup_stats"]}["flow"]
    assert edge["gated_coins"] == ["BTC"] and edge["edge"]["style"] == "maker_exit"
    svc2, rt2, st2, an2 = primed(tmp_path / "b")
    for i in range(30):
        st2.books.push(book(1100 + i * 10, 100.0, half=0.005))
    st2.books.push(book(2000, 100.0, half=0.05))                    # 10 bps wide: far over 2x usual
    st2.now_ms = 2000
    an2.latest["1m"] = ex([m("flow", UP), m("book", UP)])
    svc2.tick()
    assert all(s.skipped.get("spread") == 1 for k, s in svc2.sims.items() if k[0].startswith("auto-flow-"))


def test_binance_leads_fire_on_the_quote_not_the_next_tick(tmp_path):
    svc, rt, st, an = primed(tmp_path)
    for ts, mid in ((1000, 100.0), (3900, 100.06)):
        st.external["binance"].push(quote(ts, mid))
    st.books.push(book(3900, 100.01))
    st.now_ms = 3900
    svc.on_quote("BTC")
    lead = [s for k, s in svc.sims.items() if k[0].startswith("auto-lead-")]
    assert lead and all(s.market_due for s in lead) and svc.signals[-1][2] == "lead"
    svc.on_quote("BTC")                                              # one trigger per burst
    assert sum(1 for x in svc.signals if x[2] == "lead") == 1


def test_auto_hourly_digest():
    t = {"style": "market", "net_usd": 1.5, "exit_ms": 10, "entry_ms": 0, "gross_usd": 2, "fees_usd": 0.5, "net_pct": 1.5,
         "reason": "target"}
    title, lines = auto_hourly_message({"auto-lead-BTC-t30": {"market": [t] * 10, "limit": [], "maker_exit": []}},
                                       {"auto-lead-BTC-t30": "Binance leads · BTC · 30s"}, 0, 100)
    assert "auto hunter" in title and "10 trades" in lines[0] and "Binance leads · BTC · 30s" in lines[3]
    assert auto_hourly_message({"x": {"market": [t]}}, {}, 100, 200) is None


def test_clear_losers_are_benched_and_only_steady_winners_are_graded(tmp_path):
    rt, st, an = runtime()
    svc = BotService(rt, tmp_path / "bot.json")
    key = ("auto-lead-BTC-t30", "market")
    for i in range(20):
        x = -2.0 + (0.5 if i % 2 else -0.5)
        svc._record({"rule_id": key[0], "style": key[1], "net_usd": x, "gross_usd": x + 1.8, "fees_usd": 1.8,
                     "net_pct": x, "entry_ms": i * 1000, "exit_ms": i * 1000 + 500, "reason": "stop"})
        assert (key in svc.sims) == (i < 19)                     # benched on the 20th trade, not before
    win = ("auto-flow-BTC-t60", "maker_exit")
    for i in range(30):
        x = 1.0 + (0.2 if i % 2 else -0.2)
        svc._record({"rule_id": win[0], "style": win[1], "net_usd": x, "gross_usd": x + 1.2, "fees_usd": 1.2,
                     "net_pct": x, "entry_ms": i * 1000, "exit_ms": i * 1000 + 500, "reason": "time"})
    svc._record({"rule_id": "auto-cascade-BTC-t30", "style": "market", "net_usd": 9.0, "gross_usd": 10.0, "fees_usd": 1.0,
                 "net_pct": 9.0, "entry_ms": 0, "exit_ms": 500, "reason": "time"})          # one lucky trade
    pub = svc.public()["auto"]
    assert [g["id"] for g in pub["graded"]] == [win[0]] and pub["graded"][0]["grade"] == "proven"
    assert pub["benched_n"] == 1 and pub["worst"][0]["id"] == key[0] and pub["learning"] >= 1
    svc.save_auto({"benched": []})
    assert key in svc.sims


def test_an_older_saved_hunter_starts_fresh_on_the_new_defaults(tmp_path):
    rt, st, an = runtime()
    (tmp_path / "bot.json").write_text('{"rules": [], "auto": {"on": true, "exits": ["quick", "swing"], "version": 3, '
                                       '"benched": ["x|market"], "leverage": 10}}')
    svc = BotService(rt, tmp_path / "bot.json")
    assert svc._auto_fresh and svc.auto["exits"] == list(DEFAULT_EXITS) and svc.auto["benched"] == []
    assert svc.auto["leverage"] == 10 and svc.auto["version"] == 4
    svc.save_auto({"taker_bps": 3.0, "maker_bps": 0.0, "styles": ["market"], "spread_max": 3})
    r = next(iter(svc.auto_rules.values()))
    assert r.taker_fee == pytest.approx(0.0003) and r.maker_fee == 0.0 and svc.public()["fees"]["maker"] == 0.0
    assert all(k[1] == "market" for k in svc.sims)
    with pytest.raises(BotError):
        svc.save_auto({"styles": []})


def test_binance_leads_are_scored_by_how_far_binance_moved(tmp_path):
    svc, rt, st, an = primed(tmp_path)
    for ts, mid in ((1000, 100.0), (3900, 100.06)):
        st.external["binance"].push(quote(ts, mid))
    st.books.push(book(3900, 100.01))
    st.now_ms = 3900
    svc.on_quote("BTC")                                          # Binance +6 bps, Hyperliquid +1: long
    st.books.push(book(13_900, 100.1))
    st.now_ms = 14_000
    svc.tick()
    sizes = {r["label"]: r["cells"][0] for r in svc.public()["auto"]["lead_sizes"]}
    assert sizes["4–7"]["n"] == 1 and sizes["2–4"]["n"] == 0
    assert sizes["4–7"]["avg_bps"] == pytest.approx((100.095 / 100.015 - 1) * 1e4, abs=0.05)

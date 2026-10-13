from types import SimpleNamespace

import pytest

from api.bot import BotError, BotService, hourly_message, trade_message
from buffer.ringBuffer import RingBuffer
from engine.bot import (
    MAKER_FEE, TAKER_FEE, Rule, Sim, break_even_win_rate, card_signal, liq_distance, stats, walk,
)
from models.bookModel import BookLevel, BookSummary
from models.explanationModel import Metric
from models.signalModel import Direction
from models.tradeModel import Trade, TradeSide

UP, DOWN, FLAT = Direction.UP, Direction.DOWN, Direction.NEUTRAL


def m(key, lean, bar=0.6, partial=False):
    return Metric(key, key.title(), "", "", lean, bar, partial=partial)


def test_card_signal_needs_every_row_agreeing_and_strong():
    assert card_signal([m("flow", UP), m("book", UP), m("who", DOWN)], ("flow", "book"), 0.3) == 1
    assert card_signal([m("flow", DOWN), m("book", DOWN)], ("flow", "book"), 0.3) == -1
    assert card_signal([m("flow", UP), m("book", DOWN)], ("flow", "book"), 0.3) == 0
    assert card_signal([m("flow", UP), m("book", FLAT)], ("flow", "book"), 0.3) == 0
    assert card_signal([m("flow", UP), m("book", UP, 0.2)], ("flow", "book"), 0.3) == 0
    assert card_signal([m("flow", UP)], ("flow", "book"), 0.3) == 0                  # a row missing
    assert card_signal([m("flow", UP, partial=True), m("book", UP)], ("flow", "book"), 0.3) == 0


def test_walk_and_liquidation_distance():
    asks = (BookLevel(100.0, 10, 1), BookLevel(101.0, 10, 1))
    assert walk(asks, 500) == pytest.approx(100.0)
    assert 100.0 < walk(asks, 1500) < 101.0
    assert liq_distance(20, 40) == pytest.approx(1 / 20 - 1 / 80)
    assert liq_distance(20, None) == pytest.approx(0.05)


def rule(**kw):
    base = dict(id="r1", coin="BTC", leverage=20, margin_usd=100, take_profit_bps=20, stop_loss_bps=10,
                max_hold_s=60, cooldown_s=0, max_trades_per_hour=100, daily_loss_usd=1000, limit_wait_s=5)
    return Rule(**{**base, **kw})


def test_market_long_pays_spread_and_both_fees_on_the_target():
    s = Sim(rule(), "market", 40)
    s.signal(0, 1, 100.0, 100.01)
    assert s.on_book(100, 100.0, 100.01) is None and s.pos is None        # still inside the latency
    s.on_book(600, 100.0, 100.01)
    assert s.pos.entry_px == 100.01 and s.pos.fees == pytest.approx(2000 * TAKER_FEE)
    assert s.on_book(700, 100.1, 100.11) is None                           # +9 bps at the bid: not yet
    t = s.on_book(800, 100.25, 100.26)
    assert t["reason"] == "target" and t["exit_px"] == 100.25
    gross = (100.25 / 100.01 - 1) * 2000
    assert t["net_usd"] == pytest.approx(gross - 2000 * TAKER_FEE - 2000 * 100.25 / 100.01 * TAKER_FEE)


def test_stop_fills_at_the_bid_even_past_the_stop_and_liquidation_caps_the_loss():
    s = Sim(rule(), "market", 40)
    s.signal(0, 1, 100.0, 100.0)
    s.on_book(600, 100.0, 100.0)
    t = s.on_book(900, 99.8, 99.81)                                         # gapped through the 10 bp stop
    assert t["reason"] == "stop" and t["exit_px"] == 99.8 and t["net_usd"] < -(0.002 * 2000)
    s2 = Sim(rule(stop_loss_bps=1000), "market", 40)
    s2.signal(0, -1, 100.0, 100.0)
    s2.on_book(600, 100.0, 100.0)
    t2 = s2.on_book(900, 104.0, 104.1)                                     # short, +4%: past 1/20 - 1/80 = 3.75%
    assert t2["reason"] == "liquidated" and t2["net_usd"] == -100


def test_limit_entry_fills_only_when_a_trade_prints_through():
    s = Sim(rule(), "limit", 40)
    s.signal(0, 1, 100.0, 100.01)
    assert s.pending.price == 100.0
    s.on_trade(100, 100.0)                                                  # at our price: maybe not our turn
    assert s.pos is None
    s.on_trade(200, 99.99)
    assert s.pos.entry_px == 100.0 and s.pos.fees == pytest.approx(2000 * MAKER_FEE)
    t = s.on_trade(300, 100.21)                                             # through the resting target
    assert t["reason"] == "target" and t["exit_px"] == pytest.approx(100.2)


def test_limit_entry_times_out_and_counts_as_missed():
    s = Sim(rule(limit_wait_s=5), "limit", 40)
    s.signal(0, 1, 100.0, 100.01)
    s.on_tick(6000, 1, 100.05, 100.06)
    assert s.pending is None and s.missed == 1 and s.pos is None


def test_time_and_flip_exits_cooldown_and_hourly_cap():
    s = Sim(rule(max_hold_s=10, cooldown_s=30, max_trades_per_hour=2), "market", 40)
    s.signal(0, 1, 100.0, 100.0)
    s.on_book(600, 100.0, 100.0)
    t = s.on_tick(11_000, 1, 100.02, 100.03)
    assert t["reason"] == "time"
    s.signal(20_000, 1, 100.0, 100.0)
    assert s.market_due is None                                             # cooling down
    s.signal(42_000, -1, 100.0, 100.0)
    s.on_book(43_000, 100.0, 100.0)
    t = s.on_tick(44_000, 1, 99.99, 100.0)
    assert t["reason"] == "flip"
    s.signal(80_000, 1, 100.0, 100.0)
    assert s.market_due is None                                             # 2 trades this hour already


def test_daily_loss_stop_then_the_next_day():
    s = Sim(rule(daily_loss_usd=3), "market", 40)
    s.signal(0, 1, 100.0, 100.0)
    s.on_book(600, 100.0, 100.0)
    s.on_book(700, 99.8, 99.8)
    assert s.stopped_for_day
    s.signal(5000, 1, 100.0, 100.0)
    assert s.market_due is None
    s.signal(86_400_000, 1, 100.0, 100.0)                                   # a new UTC day
    assert s.market_due is not None


def test_stats_and_break_even():
    trades = [{"net_usd": x, "gross_usd": x + 1, "fees_usd": 1, "net_pct": x, "entry_ms": 0, "exit_ms": 1000,
               "reason": "target" if x > 0 else "stop"} for x in (2, -1, 3, -4, 2)]
    s = stats(trades, missed=3)
    assert s["trades"] == 5 and s["wins"] == 3 and s["net_usd"] == 2 and s["max_drawdown_usd"] == 4
    assert s["missed"] == 3 and s["by_reason"] == {"target": 3, "stop": 2} and "Too early" in s["verdict"]
    many = [{**trades[0], "net_usd": -1, "gross_usd": 0.5, "net_pct": -1}] * 30
    assert "costs eat" in stats(many)["verdict"]
    r = rule(take_profit_bps=15, stop_loss_bps=10)
    assert break_even_win_rate(r, "market") == pytest.approx(0.0019 / 0.0025)
    assert break_even_win_rate(r, "limit") < break_even_win_rate(r, "market")
    assert break_even_win_rate(rule(take_profit_bps=5), "market") is None   # the target doesn't cover the fees


# ── the service, on a hand-fed market ───────────────────────────────────────
def fake_runtime(tmp_path):
    st = SimpleNamespace(books=RingBuffer(max_seconds=3600), trades=RingBuffer(max_seconds=3600), now_ms=0, book=None,
                         context=None)
    an = SimpleNamespace(latest={})
    pipe = SimpleNamespace(state=st, analyzer=an)
    return SimpleNamespace(coins=["BTC"], pipelines={"BTC": pipe}, mode="replay", planner=None, alerts=None), st, an


def book(ts, bid, ask):
    return BookSummary(ts, (bid + ask) / 2, bid, ask, (ask - bid) / ((bid + ask) / 2) * 1e4, 1e6, 1e6)


def test_service_validates_and_trades_from_the_card(tmp_path):
    rt, st, an = fake_runtime(tmp_path)
    svc = BotService(rt, tmp_path / "bot.json")
    svc.save_auto({"on": False})                       # this test is about a hand-made rule
    with pytest.raises(BotError, match="closer"):
        svc.save_rule({"coin": "BTC", "leverage": 40, "stop_loss_bps": 300})
    with pytest.raises(BotError, match="Pick at least one"):
        svc.save_rule({"coin": "BTC", "timeframe": "60m", "signals": ["book"]})        # no Book row on 60m
    out = svc.save_rule({"coin": "BTC", "timeframe": "1m", "signals": ["flow", "book"], "leverage": 20,
                         "take_profit_bps": 20, "stop_loss_bps": 10, "cooldown_s": 0})
    rid = out["rules"][0]["id"]
    assert out["rules"][0]["name"] == "BTC 1m Flow + Book"
    assert set(out["timeframes"]["1m"]) == {"flow", "who", "forced", "book"}

    st.books.push(book(1000, 100.0, 100.01))
    st.now_ms = 1000
    an.latest["1m"] = SimpleNamespace(summary=(m("flow", UP), m("book", UP)))
    svc.tick()                                         # signal → market entry due, limit order resting at 100.0
    st.books.push(book(1700, 100.0, 100.01))
    st.trades.push(Trade(1800, 99.99, 1.0, TradeSide.SELL, 1))
    st.now_ms = 1800
    svc.tick()
    pub = svc.public()
    styles = pub["rules"][0]["styles"]
    assert styles["market"]["open"]["entry_px"] == 100.01 and styles["limit"]["open"]["entry_px"] == 100.0
    st.books.push(book(2500, 100.25, 100.26))
    st.now_ms = 2500
    closed = svc.tick()
    assert {t["style"] for t in closed} == {"market", "limit", "maker_exit"} and all(t["reason"] == "target" for t in closed)
    assert svc.public()["rules"][0]["styles"]["market"]["trades"] == 1

    # a new name keeps the record; new trading settings start a fresh one
    out = svc.save_rule({"id": rid, "name": "Scalper", "coin": "BTC", "signals": ["flow", "book"], "leverage": 20,
                         "take_profit_bps": 20, "stop_loss_bps": 10, "cooldown_s": 0})
    assert out["rules"][0]["id"] == rid and out["rules"][0]["styles"]["market"]["trades"] == 1
    out = svc.save_rule({**out["rules"][0], "take_profit_bps": 30})
    assert out["rules"][0]["id"] != rid and out["rules"][0]["styles"]["market"]["trades"] == 0
    assert BotService(rt, tmp_path / "bot.json").rules                  # saved to disk and loaded back


def test_messages_are_plain():
    r = rule(name="BTC 1m Flow + Book")
    t = {"side": 1, "entry_ms": 0, "exit_ms": 90_000, "entry_px": 100.0, "exit_px": 100.2, "reason": "target",
         "style": "market", "net_usd": 1.2, "net_pct": 1.2, "fees_usd": 1.8, "gross_usd": 3.0}
    title, lines = trade_message(r, t)
    assert "hit the target" in title and "+$1.20" in title and "—" not in title + "".join(lines)
    msg = hourly_message(r, {"market": [t], "limit": []}, 0, 3_600_000)
    assert msg and "1 trades this hour" in msg[1][0]
    assert hourly_message(r, {"market": [t]}, 3_600_000, 7_200_000) is None


def test_market_in_limit_out_closes_at_the_touch_as_a_maker_or_crosses_after_three_seconds():
    s = Sim(rule(max_hold_s=10), "maker_exit", 40)
    s.signal(0, 1, 100.0, 100.02)
    s.on_book(600, 100.0, 100.02)
    assert s.pos.entry_px == 100.02 and s.pos.fees == pytest.approx(2000 * TAKER_FEE)
    assert s.on_tick(11_000, 0, 100.05, 100.07) is None and s.closing[0] == 100.07     # sell limit at the ask
    t = s.on_trade(11_500, 100.08)                                                       # a buyer lifts it
    assert t["reason"] == "time" and t["exit_px"] == 100.07
    assert t["fees_usd"] == pytest.approx(2000 * TAKER_FEE + 2000 * 100.07 / 100.02 * MAKER_FEE)
    s2 = Sim(rule(max_hold_s=10, cooldown_s=0), "maker_exit", 40)
    s2.signal(0, 1, 100.0, 100.02)
    s2.on_book(600, 100.0, 100.02)
    s2.on_tick(11_000, 0, 100.05, 100.07)
    t2 = s2.on_tick(14_100, 0, 100.03, 100.05)                                          # nobody took it: cross
    assert t2["exit_px"] == 100.03 and t2["fees_usd"] > t["fees_usd"] - 0.01


def test_fee_rates_follow_the_rule():
    cheap = rule(take_profit_bps=15, stop_loss_bps=10)
    cheap.taker_fee, cheap.maker_fee = 0.0003, 0.0
    assert break_even_win_rate(cheap, "market") < break_even_win_rate(rule(take_profit_bps=15, stop_loss_bps=10), "market")
    assert break_even_win_rate(cheap, "maker_exit") < break_even_win_rate(cheap, "market")

import asyncio
import math
from types import SimpleNamespace

import numpy as np
import pytest

from api.runtime import Runtime
from api.scanner import Scanner
from engine.scanner import Baseline, CoinHistory, baseline_from_candles, score_coin, trade_reads
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.feedStatus import FeedStatus
from ingestion.parsers import trade_subscription
from state import MarketState
from tests.helpers import T0, trade

H = 3_600_000


def _candles(sigma=0.01, n=170, price=100.0, vol=1_000.0, seed=3):
    r = np.random.default_rng(seed).normal(0, sigma, n)
    closes = price * np.exp(np.cumsum(r))
    return [{"t": T0 + i * H, "c": str(c), "v": str(vol)} for i, c in enumerate(closes)]


def test_a_coins_normal_comes_from_its_own_week_of_candles():
    b = baseline_from_candles(_candles(), T0)
    assert 0.008 < b.sigma_1h < 0.012
    assert b.vol_day == pytest.approx(b.vol_hour * 24, rel=0.3)
    assert baseline_from_candles(_candles(n=20), T0) is None              # too new to judge


def test_history_gives_changes_and_the_usual_open_interest_move():
    h = CoinHistory()
    for i in range(400):                                                  # 10s samples, ~67 minutes
        h.add(T0 + i * 10_000, 100 + i * 0.01, 1_000 + i)
    now = T0 + 399 * 10_000
    assert h.change(now, 900, 1) == pytest.approx((103.99 / 103.09 - 1) * 100, rel=1e-6)
    assert h.change(now, 3600, 2) == pytest.approx((1399 / 1039 - 1) * 100, rel=1e-6)
    assert h.change(now, 7200, 1) is None                                 # not that much history yet
    assert h.typical_oi_1h() == 1.5                                       # too little to learn from: the default


BASE = Baseline(sigma_1h=0.01, vol_hour=1_000_000.0, vol_day=24_000_000.0, fetched_ms=T0)


def _ctx(mark=100.0, funding=0.0000125, oi=1_000.0, vol=24e6, prev=100.0, oracle=100.0):
    return {"mark": mark, "oracle": oracle, "funding": funding, "oi": oi, "day_volume": vol, "prev_day": prev}


def _hist(p_then, p_now, oi_then=1_000.0, oi_now=1_000.0):
    h = CoinHistory()
    for i in range(361):                                                  # an hour of 10s samples
        f = i / 360
        h.add(T0 + i * 10_000, p_then + (p_now - p_then) * f, oi_then + (oi_now - oi_then) * f)
    return h, T0 + 360 * 10_000


def test_a_quiet_coin_has_no_reasons_and_a_big_mover_explains_itself():
    h, now = _hist(100.0, 100.1)
    quiet = score_coin("AAA", _ctx(mark=100.1), h, BASE, None, now)
    assert quiet.score < 1 and quiet.reasons == ()
    h, now = _hist(100.0, 103.0, 1_000, 1_100)                           # +3% in an hour on a 1% coin, OI +10%
    mover = score_coin("BBB", _ctx(mark=103.0, oracle=103.0, vol=60e6), h, BASE, None, now)
    assert mover.score >= 1.5 and mover.lean == "up"
    assert mover.reasons[0].startswith("open interest +10.0% in 1h") or mover.reasons[0].startswith("price +3.0% in 1h")
    assert any(r.startswith("price +3.0% in 1h, 3.0× a normal hour") for r in mover.reasons)
    assert any("2.5× its usual daily volume" in r for r in mover.reasons)


def test_a_coin_nobody_trades_never_tops_the_list():
    h, now = _hist(100.0, 110.0)
    r = score_coin("DUST", _ctx(mark=110.0, oracle=110.0, vol=50_000), h, BASE, None, now)
    assert r.score < 1 and r.reasons and r.reasons[-1] == "thin trading: $50K in 24h"


def test_funding_and_the_index_gap_count_too():
    h, now = _hist(100.0, 100.0)
    r = score_coin("CCC", _ctx(funding=0.00006, mark=100.3, oracle=100.0), h, None, None, now)
    assert r.learning and r.funding_apr == pytest.approx(52.56)
    assert "funding 53% a year, longs pay" in r.reasons and "0.30% above the index" in r.reasons


def _state_with_trades():
    st = MarketState("DOGE")
    t = T0
    for i in range(300):                                                  # steady small trades, mostly buys
        t += 2_000
        st.apply_many([trade(t, px=0.2, sz=5_000, side="B" if i % 4 else "A")])
    for k in range(4):                                                    # then a run of big sells
        t += 500
        st.apply_many([trade(t, px=0.199, sz=400_000, side="A", h=f"big{k}")])
    return st, t


def test_trade_reads_find_the_flow_big_orders_and_runs():
    st, now = _state_with_trades()
    rd = trade_reads(st, now)
    assert rd.trades == 304 and rd.big_orders == 4 and rd.big_usd == pytest.approx(4 * 400_000 * 0.199)
    assert rd.buy_usd > 0 and rd.sell_usd > rd.buy_usd                    # the big sells outweigh the drip of buys
    (c,) = rd.cascades
    assert c["side"] == "sell" and c["orders"] == 4
    row = score_coin("DOGE", _ctx(mark=0.199, prev=0.2, oracle=0.199), None,
                     Baseline(0.01, 200_000.0, 4.8e6, T0), rd, now)
    assert row.traded and row.big_orders == 4 and any(r.startswith("run of 4 big sell orders") for r in row.reasons)


def test_small_trades_on_a_small_coin_are_not_an_event():
    st, now = _state_with_trades()
    rd = trade_reads(st, now)
    small = rd.__class__(**{**{f: getattr(rd, f) for f in rd.__dataclass_fields__},
                            "liquidated_usd": 20_000.0, "cascades": ()})
    row = score_coin("DOGE", _ctx(mark=0.199, prev=0.2, oracle=0.199), None,
                     Baseline(0.01, 200_000.0, 4.8e6, T0), small, now)           # 5% of its hour is just $10K
    assert not any("liquidated" in r for r in row.reasons) and row.kind_scores["trades"] < 1
    big = rd.__class__(**{**{f: getattr(small, f) for f in rd.__dataclass_fields__}, "liquidated_usd": 150_000.0})
    row = score_coin("DOGE", _ctx(mark=0.199, prev=0.2, oracle=0.199), None,
                     Baseline(0.01, 200_000.0, 4.8e6, T0), big, now)
    assert "$150K liquidated in 15m" in row.reasons and row.kind_scores["trades"] == 1.5


def test_each_kind_of_reading_has_its_own_score_for_filtering():
    h, now = _hist(100.0, 103.0, 1_000, 1_100)
    r = score_coin("BBB", _ctx(mark=103.0, oracle=103.0, vol=60e6, funding=0.00006), h, BASE, None, now)
    assert set(r.kind_scores) >= {"move", "volume", "positions", "funding", "index"}
    assert r.score == max(r.kind_scores.values())
    assert [n["kind"] for n in r.notes][:1] and all(n["score"] >= 1 for n in r.notes)
    assert r.kind_scores["funding"] == pytest.approx(52.56 / 40, abs=1e-3)


# ── the service ─────────────────────────────────────────────────────────────
def _scanner():
    rt = SimpleNamespace(mode="live", coins=["ETH"], pipelines={})
    sc = Scanner(rt)
    vols = {"ETH": 9e8, "DOGE": 5e7, "ZZZ": 1e3}

    async def contexts():
        return {c: _ctx(vol=v) for c, v in vols.items()}

    async def candles(coin, interval, n):
        return _candles()
    sc.fetch_contexts, sc.fetch_candles = contexts, candles
    return sc


def test_a_poll_ranks_every_coin_and_picks_the_busiest_to_follow():
    sc = _scanner()
    changes = []
    sc.on_top_change = lambda: changes.append(list(sc.top))
    asyncio.run(sc.poll())
    assert {r.coin for r in sc.rows} == {"ETH", "DOGE", "ZZZ"} and sc.top[:2] == ["ETH", "DOGE"] and changes
    pub = sc.public()
    assert pub["coins"] == 3 and next(r for r in pub["rows"] if r["coin"] == "ETH")["main"]
    assert sc._next_baseline() == "ETH"                                   # busiest coin learns its normal first
    sc.rt.pipelines["ETH"] = SimpleNamespace(state=None)
    assert sc.trade_coins() == ["DOGE", "ZZZ"]                           # ETH's trades come from its dashboard


def test_scanner_trades_only_go_to_followed_coins():
    sc = _scanner()
    asyncio.run(sc.poll())
    msg = {"channel": "trades", "data": [{"coin": "DOGE", "side": "B", "px": "0.2", "sz": "100", "time": T0,
                                          "hash": "0xabc", "tid": 1, "users": ["0x1", "0x2"]}]}
    sc.on_message(msg)
    assert sc.states["DOGE"].trades and "NOPE" not in sc.states
    sc.on_message({**msg, "data": [{**msg["data"][0], "coin": "NOPE"}]})
    assert "NOPE" not in sc.states


def test_unusual_coins_ping_once_per_episode_and_never_for_the_main_tabs():
    sc = _scanner()
    pushed = []
    from engine.alerts import DEFAULT_RULES
    sc.rt.alerts = SimpleNamespace(cfg={"rules": {k: dict(v) for k, v in DEFAULT_RULES.items()}}, push=pushed.append)
    funding = {"ETH": 0.0002, "DOGE": 0.0000125, "ZZZ": 0.0000125}       # ETH: 175% a year from the start

    async def contexts():
        return {c: _ctx(vol=9e8 if c == "ETH" else 5e7, funding=funding[c]) for c in funding}
    sc.fetch_contexts = contexts
    asyncio.run(sc.poll())
    assert pushed == []                                                   # nothing new yet; ETH has its own tab
    funding["DOGE"] = 0.00016                                             # 140% a year: 3.5× the line
    asyncio.run(sc.poll())
    (a,) = pushed
    assert a.kind == "scanner" and a.title == "DOGE: funding 140% a year, longs pay" and "More coins" in a.lines[-1]
    asyncio.run(sc.poll())
    assert len(pushed) == 1                                               # still unusual: no repeat
    funding["DOGE"] = 0.0000125
    asyncio.run(sc.poll())                                                # calms down: re-arms
    funding["DOGE"] = 0.00016
    asyncio.run(sc.poll())
    assert len(pushed) == 2


def test_already_unusual_coins_are_quiet_after_a_restart():
    sc = _scanner()
    pushed = []
    from engine.alerts import DEFAULT_RULES
    sc.rt.alerts = SimpleNamespace(cfg={"rules": {k: dict(v) for k, v in DEFAULT_RULES.items()}}, push=pushed.append)

    async def contexts():
        return {"DOGE": _ctx(vol=5e7, funding=0.00016)}
    sc.fetch_contexts = contexts
    asyncio.run(sc.poll())
    asyncio.run(sc.poll())
    assert pushed == []


# ── coins opened from More coins ────────────────────────────────────────────
def test_more_coins_open_on_demand_and_the_oldest_idle_one_makes_room(tmp_path):
    rt = Runtime("live", coins=["ETH"], bars_db=None, backfill_enabled=False, record=False, record_file=None,
                 alerts_file=tmp_path / "a.json", watch_file=tmp_path / "w.json")
    rt.scanner.contexts = {c: _ctx() for c in ("ETH", "DOGE", "SUI", "WIF", "PEPE")}
    assert rt.open_coin("ETH") and "ETH" not in rt.extra                 # a configured coin: nothing to open
    assert not rt.open_coin("NOTACOIN")
    for c in ("DOGE", "SUI", "WIF"):
        assert rt.open_coin(c)
        rt.extra[c] -= 100 if c == "DOGE" else 0                          # DOGE was viewed longest ago
    assert set(rt.extra) == {"DOGE", "SUI", "WIF"} and "DOGE" in rt.pipelines
    assert rt.open_coin("PEPE") and "DOGE" not in rt.pipelines and set(rt.extra) == {"SUI", "WIF", "PEPE"}
    from api.serializers import build_snapshot
    snap = build_snapshot(rt, "PEPE")
    assert snap["extra"] and snap["coin"] == "PEPE" and list(snap["tickers"]) == ["ETH"]   # tabs: configured coins only
    replay = Runtime("replay", alerts_file=tmp_path / "a2.json", watch_file=tmp_path / "w2.json")
    assert not replay.open_coin("DOGE")


def test_the_second_socket_changes_what_it_listens_to_while_connected():
    sent = []

    class FakeWs:
        async def send(self, text):
            sent.append(text)

    async def run():
        c = HyperliquidClient([], lambda m: None, FeedStatus(mode="live"))
        c.set_subscriptions([trade_subscription("DOGE")])                 # not connected: just remembered
        c._ws = FakeWs()
        c.set_subscriptions([trade_subscription("SUI")])
        await asyncio.sleep(0)
        return c
    c = asyncio.run(run())
    assert any('"unsubscribe"' in s and "DOGE" in s for s in sent) and any('"subscribe"' in s and "SUI" in s for s in sent)
    assert len(c._subs) == 1 and math.isfinite(len(sent))

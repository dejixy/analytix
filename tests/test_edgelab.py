import json
import math
import random

import pytest

from engine.edgelab import (
    BASE_LATENCY_MS, HORIZONS_S, LATENCIES_MS, MAKER_WAIT_MS, Event, Lab, Tracker, bucket, net_maker, net_market,
    split, study, summary,
)

T0 = 1_791_000_000_000


def _ev(side=1, t=0, **feats):
    return Event(1, "BTC", side, t, t, {"lead"}, 100.0, 0.5, {"coin": "BTC", **feats})


def test_entries_use_the_book_as_of_each_delay_and_exits_pay_the_spread():
    tr = Tracker()
    e = _ev()
    tr.add(e)
    tr.on_book(100, 99.99, 100.01)
    tr.on_book(400, 100.00, 100.02)            # 250 ms entry: the book as of 250 (the one at 100)
    assert e.entries[250] == 100.01
    tr.on_book(600, 100.04, 100.06)            # 500 ms: the book at 400 was still the latest at 500
    assert e.entries[500] == 100.02
    t = 600
    for h in HORIZONS_S:
        t = BASE_LATENCY_MS + h * 1000
        tr.on_book(t, 100.12, 100.14)
        tr.on_book(t + MAKER_WAIT_MS, 100.12, 100.14)
    tr.on_book(t + 10_000, 100.12, 100.14)
    assert e.exits[30] == 100.12 and e.gross(30) == pytest.approx((100.12 / 100.02 - 1) * 1e4)
    assert e.latency_gross(250, 30) > e.latency_gross(500, 30)        # getting in sooner paid less
    assert e.mae[5] <= e.mfe[5] and e.mfe[900] >= e.mfe[5]           # excursions only widen with time


def test_a_closing_limit_fills_only_when_traded_through_or_the_book_reaches_it():
    tr = Tracker()
    e = _ev(side=1)
    tr.add(e)
    tr.on_book(500, 100.0, 100.02)
    tr.on_book(BASE_LATENCY_MS + 5000, 100.05, 100.07)     # 5s exit: the closing sell rests at 100.07
    assert e.waiting[5][0] == 100.07
    tr.on_trade(100.07)                                     # at our price: not necessarily us
    assert 5 not in e.makers
    tr.on_trade(100.08)
    assert e.makers[5] == (100.07, True)
    tr.on_book(BASE_LATENCY_MS + 10_000, 100.05, 100.07)   # 10s exit rests at 100.07, nobody comes
    tr.on_book(BASE_LATENCY_MS + 10_000 + MAKER_WAIT_MS, 100.03, 100.05)
    assert e.makers[10] == (100.03, False)                  # crossed at the bid after 3s
    assert net_maker(e, 5, 4.5, 1.5) > net_market(e, 5, 4.5)


def _recording(seconds=2400, jump_every=120, lag_ms=1200, seed=4):
    """BTC: Binance quotes every 250 ms, Hyperliquid's book every 500 ms, Hyperliquid following Binance 1.2 s late.
    Every two minutes the price jumps 10 bps (up or down)."""
    random.seed(seed)
    path = []
    price, t = 100_000.0, 0
    while t <= seconds * 1000:
        if t and t % (jump_every * 1000) == 0 and t < (seconds - 1000) * 1000:
            price *= 1 + random.choice((1, -1)) * 0.001
        price *= math.exp(random.gauss(0, 0.00001))
        path.append((t, price))
        t += 250
    msgs = []

    def price_at(ms):
        i = max(0, min(len(path) - 1, ms // 250))
        return path[i][1]

    for t, p in path:
        msgs.append((T0 + t, {"channel": "ext", "data": {"venue": "binance", "coin": "BTC", "ts": T0 + t,
                                                         "bid": p - 1, "ask": p + 1}}))
        if t % 500 == 0:
            hp = price_at(t - lag_ms)
            msgs.append((T0 + t + 1, {"channel": "l2Book", "data": {"coin": "BTC", "time": T0 + t + 1, "levels": [
                [{"px": str(round(hp - 5, 1)), "sz": "3", "n": 2}], [{"px": str(round(hp + 5, 1)), "sz": "3", "n": 2}]]}}))
    return msgs


def test_the_lab_finds_a_lead_and_shows_it_fading_with_delay():
    lab = Lab(["BTC"], lead_only=True)
    for _, msg in _recording():
        lab.on_message(msg)
    evs = lab.events
    assert len(evs) >= 8 and all(e.tags == {"lead"} for e in evs)
    assert all(abs(e.features["lead_bps"]) >= 8 for e in evs)
    g30 = summary([e.gross(30) for e in evs])
    assert g30["mean"] > 7                                   # a 10 bp jump, less about 1 bp of spread
    fast = summary([e.latency_gross(250) for e in evs])["mean"]
    slow = summary([e.latency_gross(2000) for e in evs])["mean"]
    assert fast > 7 and slow < 2                             # 2 s late, Hyperliquid has already caught up
    s = study(evs, taker_bps=4.5, maker_bps=1.5)
    lead = s["setups"][0]
    assert lead["tag"] == "lead" and lead["train"]["mean"] > 0 and lead["latency"][250]["mean"] > lead["latency"][2000]["mean"]


def test_signals_together_make_one_event_and_features_bucket():
    lab = Lab(["BTC"], lead_only=True)
    for _, msg in _recording(seconds=60):
        lab.on_message(msg)
    lab.signal("BTC", "flow", 1, T0 + 30_000, {})
    lab.signal("BTC", "winning", 1, T0 + 35_000, {})          # same side within 10s: the same event
    lab.signal("BTC", "breakout", -1, T0 + 36_000, {})        # the other way: a new one
    open_ev = lab.open["BTC"]
    assert open_ev.tags == {"breakout"} and lab.signals >= 3
    flow_ev = next(e for e in lab.trackers["BTC"].pending if "flow" in e.tags)
    assert flow_ev.tags == {"flow", "winning"} and bucket(flow_ev, "together") == "with others"
    e = _ev(lead_bps=9.0, bybit_bps=6.0, vol_ratio=2.0, spread_ratio=1.0, book_lean=-0.5, session="US")
    assert (bucket(e, "lead size"), bucket(e, "bybit"), bucket(e, "volatility"), bucket(e, "spread"), bucket(e, "book")) == \
        ("7–12", "agrees", "busy", "usual", "against us")


def test_train_comes_before_test_and_summaries_are_honest():
    evs = [_ev(t=i * 1000) for i in range(10)]
    tr, te = split(evs, 0.6)
    assert max(e.t_ms for e in tr) < min(e.t_ms for e in te) and len(tr) + len(te) == 10
    s = summary([1.0, 2.0, 3.0])
    assert s["mean"] == 2.0 and s["hit"] == 1.0 and s["t"] > 3
    assert summary([])["mean"] is None


def test_the_script_runs_on_recording_files(tmp_path):
    from scripts.edgeLab import report, run, write_csv
    f = tmp_path / "btc_20261001_0000.jsonl"
    f.write_text("\n".join(json.dumps({"recv_ms": recv, "msg": msg}) for recv, msg in _recording()))
    lab, hours = run([str(tmp_path)], ["BTC"], lead_only=True, progress=False)
    text = report(lab, hours, 4.5, 1.5)
    assert "EDGE LAB" in text and "Binance leads" in text and "SPEED" in text
    write_csv(lab, str(tmp_path / "ev.csv"), 4.5, 1.5)
    rows = (tmp_path / "ev.csv").read_text().splitlines()
    assert len(rows) == len(lab.events) + 1 and "gross_30s" in rows[0]

"""The edge terminal's schedule and record (engine/edges.py) and its live service (api/edges.py)."""
import math
from datetime import datetime, timezone
from types import SimpleNamespace

from api.edges import EdgeService, correlations
from engine import edges as E


def ny(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=E.NY).timestamp() * 1000)


def test_sessions_follow_new_york_time_through_the_clock_change():
    assert E.session_state("xyz:CL", ny(2026, 10, 30, 16, 59))["open"]
    fri = E.session_state("xyz:CL", ny(2026, 10, 30, 17, 0))
    assert not fri["open"] and fri["why"] == "weekend" and fri["next_change_ms"] == ny(2026, 11, 1, 18)
    # 17:00 New York is 21:00 UTC before the clock change and 22:00 UTC after it
    assert fri["since_ms"] == utc(2026, 10, 30, 21)
    nxt = E.session_state("xyz:GOLD", ny(2026, 11, 6, 12))
    assert nxt["next_long_close_ms"] == utc(2026, 11, 6, 22)
    assert E.session_state("BRENTOIL", ny(2026, 11, 6, 17, 30))["open"]            # Brent shuts an hour later
    brk = E.session_state("xyz:SILVER", ny(2026, 11, 3, 17, 30))
    assert not brk["open"] and brk["why"] == "break" and brk["next_change_ms"] == ny(2026, 11, 3, 18)


def utc(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


def test_christmas_shuts_from_thursday_to_sunday():
    s = E.session_state("GOLD", ny(2026, 12, 24, 20))
    assert not s["open"] and s["why"] == "holiday" and s["next_change_ms"] == ny(2026, 12, 27, 18)


def test_weekends_next_to_holidays_are_left_out():
    got = E.weekends("WTIOIL", ny(2026, 11, 16, 0), ny(2026, 12, 10, 0))
    fridays = [datetime.fromtimestamp(c / 1000, E.NY).day for c, _ in got]
    assert fridays == [20, 4]                       # 27 Nov follows Thanksgiving


def candles_for(close_ms, reopen_ms, fri, pre, post):
    """Hourly candles: flat at fri up to the close, then pre through the weekend, post the hour after the reopen."""
    out = []
    t = close_ms - 5 * E.HOUR
    while t <= reopen_ms + 2 * E.HOUR:
        p = fri if t < close_ms else pre if t < reopen_ms else post
        out.append((t, p))
        t += E.HOUR
    return out


def test_weekend_rows_measure_the_weekend_and_the_reopen():
    close, reopen = ny(2026, 11, 6, 17), ny(2026, 11, 8, 18)
    rows = E.weekend_rows("WTIOIL", candles_for(close, reopen, 60.0, 63.0, 62.4))
    assert len(rows) == 1
    r = rows[0]
    assert r["w_bps"] == round(math.log(63 / 60) * 1e4, 1) and r["r_bps"] == round(math.log(62.4 / 60) * 1e4, 1)
    assert r["snap_bps"] < 0                        # gave some back at the reopen


def test_fit_and_fade():
    rows = [{"w_bps": w, "r_bps": 0.8 * w - 10, "snap_bps": -0.2 * w - 10} for w in (-300, -150, -50, 20, 120, 400)]
    f = E.fit(rows)
    assert f["b"] == 0.8 and f["a"] == -10 and f["r2"] == 1.0
    fd = E.fade(rows, min_bps=100, cost_bps=18)
    # big ones: -300 → +60-10 = +50; -150 → +20; 120 → +34; 400 → +90; minus 18 each
    assert fd["n"] == 4 and fd["avg_bps"] == round((50 + 20 + 34 + 90) / 4 - 18, 1) and fd["win"] == 1.0
    assert E.expected_snap(300, f)["from"] == "published"           # 6 weekends: too few of our own
    assert E.expected_snap(300, None)["snap_bps"] == round(-15 + (0.834 - 1) * 300, 1)


def test_roll_window_weights_and_contracts():
    r = E.roll_state("xyz:CL", ny(2026, 11, 3, 12))
    assert (r["start"], r["end"], r["phase"], r["front"], r["next"]) == ("2026-11-06", "2026-11-13", "before", "Z26", "F27")
    assert r["symbols"] == ["CLZ26.NYM", "CLF27.NYM"]
    assert E.roll_state("WTIOIL", ny(2026, 11, 7, 12))["front_weight"] == 1.0       # Saturday keeps Friday's
    mid = E.roll_state("WTIOIL", ny(2026, 11, 10, 12))
    assert mid["phase"] == "during" and mid["front_weight"] == 0.6
    dec = E.roll_state("BRENTOIL", ny(2026, 11, 20, 12))                           # November's is over: December's
    assert (dec["start"], dec["front"], dec["next"]) == ("2026-12-07", "G27", "H27")
    d = E.roll_drift({"front_weight": 1.0}, 60.0, 59.0)
    assert d["gap_pct"] == -1.67 and d["left_bps"] == round(-1 / 60 * 1e4, 1)


def test_recurring_events():
    ev = E.recurring(ny(2026, 11, 23, 9), 7)
    titles = [e["title"] for e in ev]
    assert "EIA crude stocks" in titles and "Oil, gold and silver reopen" in titles
    assert "Deribit options expiry (monthly)" in titles                             # 27 Nov, the last Friday
    assert all(e["t_ms"] > ny(2026, 11, 23, 9) for e in ev)


def test_service_shows_the_live_weekend(monkeypatch):
    rt = SimpleNamespace(mode="live", news=None, longtail=SimpleNamespace(client=object(), edge_coins=["xyz:GOLD"]))
    svc = EdgeService(rt, coins=["xyz:GOLD", "xyz:JPY"])
    sat = ny(2026, 11, 7, 12)
    close, reopen = ny(2026, 11, 6, 17), ny(2026, 11, 8, 18)
    svc.candles["xyz:GOLD"] = [(t, p) for t, p in candles_for(close, reopen, 4000.0, 4100.0, 4080.0) if t < sat]
    svc.take({"xyz:GOLD": {"mark": 4100.0, "oracle": 4098.0, "funding": 0.00001, "oi": 10.0, "prev_day": 4000.0,
                           "max_lev": 25.0},
              "xyz:JPY": {"mark": 150.0, "oracle": 150.0, "prev_day": 152.0}}, sat)
    d = svc.public(sat)
    gold = d["markets"][0]
    assert gold["session"]["why"] == "weekend" and gold["closed"]["ref"] == 4000.0
    assert gold["closed"]["band"] == [3840.0, 4160.0] and gold["closed"]["w_bps"] == round(math.log(4100 / 4000) * 1e4, 1)
    assert gold["closed"]["expect"]["from"] == "published"
    assert d["fx"][0]["label"] == "USD/JPY" and d["yen_watch"]                       # down 1.3% in a day
    assert [c["key"] for c in d["board"]][:2] == ["weekend", "roll"]
    assert d["recording"] == {"on": True, "markets": ["xyz:GOLD"]}
    assert gold["spark"][-1] == 4100.0 and len(gold["spark"]) <= 49 and d["hours"]["open"]


def test_wti_answers_to_both_names():
    assert E.bare("xyz:CL") == E.bare("xyz:WTIOIL") == "CL"
    assert E.roll_state("WTIOIL", ny(2026, 11, 10, 12))["symbols"][0] == "CLZ26.NYM"


def test_open_intervals_across_a_weekend():
    got = E.open_intervals("CL", ny(2026, 11, 5, 12), ny(2026, 11, 9, 12))
    assert got == [(ny(2026, 11, 5, 12), ny(2026, 11, 5, 17)), (ny(2026, 11, 5, 18), ny(2026, 11, 6, 17)),
                   (ny(2026, 11, 8, 18), ny(2026, 11, 9, 12))]
    assert E.open_intervals("CL", ny(2026, 11, 7, 12), ny(2026, 11, 7, 13)) == []        # Saturday: shut throughout


def test_correlations_of_hourly_moves():
    up = [100 * 1.01 ** i * (1.02 if i % 2 else 1) for i in range(40)]
    same = [2 * x for x in up]
    mirror = [1e4 / x for x in up]
    m = correlations({"A": up, "B": same, "C": mirror, "D": [None] * 40})
    assert m[0][1] == 1.0 and m[0][2] == -1.0 and m[0][3] is None and m[2][0] == -1.0


def test_compare_puts_every_market_on_one_grid():
    rt = SimpleNamespace(mode="live", news=None, longtail=None)
    svc = EdgeService(rt, coins=["xyz:GOLD", "xyz:JPY"])
    now = ny(2026, 11, 4, 12, 30)
    base = (now // E.HOUR) * E.HOUR
    svc.candles["xyz:GOLD"] = [(base - k * E.HOUR, 4000.0 + k) for k in range(200)]
    svc.candles["xyz:JPY"] = [(base - k * E.HOUR, 150.0 - k / 100) for k in range(0, 200, 2)]    # gaps: carried
    svc.take({"xyz:GOLD": {"mark": 3999.0}}, now)
    c = svc.compare(now)
    assert len(c["series"]["xyz:GOLD"]) == len(c["series"]["xyz:JPY"]) == 169
    assert c["series"]["xyz:GOLD"][-1] == 3999.0 and c["series"]["xyz:JPY"][1] == c["series"]["xyz:JPY"][0]
    assert c["corr"]["names"] == ["xyz:GOLD", "xyz:JPY"] and c["open"]


def test_live_trades_merge_fills_into_orders():
    import time as _t
    svc = EdgeService(SimpleNamespace(mode="live", news=None, longtail=None), coins=["xyz:GOLD", "xyz:CL"])
    now = int(_t.time() * 1000)
    fill = lambda px, sz, side, users, tid, t=now, coin="xyz:GOLD": {"coin": coin, "px": str(px), "sz": str(sz),
                                                                     "side": side, "time": t, "tid": tid, "users": users}
    svc.on_message({"channel": "trades", "data": [
        fill(4000, 10, "B", ["0xtaker", "0xm1"], 1), fill(4001, 10, "B", ["0xtaker", "0xm2"], 2),   # one order, 2 fills
        fill(4000, 0.1, "A", ["0xm3", "0xother"], 3),                                                # $400: too small to send
        fill(60, 500, "A", ["0xm4", "0xseller"], 4, coin="xyz:CL"),
        fill(1, 1, "B", ["a", "b"], 5, coin="xyz:SOMETHING"),                                       # not ours
    ]})
    got = svc.trades_public()["orders"]
    assert [(o["coin"], o["side"], o["usd"], o["fills"]) for o in got] == [("xyz:GOLD", 1, 80010, 2), ("xyz:CL", -1, 30000, 1)]
    assert got[0]["px"] == round((4000 * 40000 + 4001 * 40010) / 80010, 6) and "_taker" not in got[0]
    assert svc.trades_public(since_ms=now)["orders"] == []                    # nothing newer than the last one
    f = svc.trades_public()["flow"]
    assert f["xyz:GOLD"][15] == {"net": 80010 - 400, "buy": 80010, "sell": 400, "n": 2}   # flow counts every order
    assert f["xyz:CL"][5]["net"] == -30000


def test_longtail_hands_edge_messages_to_the_terminal(tmp_path):
    from api.longtail import LongTailRecorder
    rec = LongTailRecorder(SimpleNamespace(mode="live", record=True, coins=[]), folder=tmp_path, enabled=True)
    rec.edge_coins = ["xyz:GOLD"]
    seen = []
    rec.on_edge = seen.append
    rec._on_message({"channel": "trades", "data": [{"coin": "xyz:GOLD"}]})
    rec._on_message({"channel": "trades", "data": [{"coin": "DOGE"}]})
    assert seen == [{"channel": "trades", "data": [{"coin": "xyz:GOLD"}]}]

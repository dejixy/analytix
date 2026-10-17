"""The live bot: big TWAPs (api/bot.py, api/twaptrack.py) and their settling (engine/bigmovers.settle / pt_of)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from api.bot import BotError, BotService, spotted_message, stopped_message
from engine.bigmovers import pt_of, settle

T0 = 1_790_000_000          # seconds
TW = "0x" + "a" * 40        # the TWAP's wallet
OTHER = "0x" + "c" * 40


def rt():
    return SimpleNamespace(mode="replay", alerts=None)


def svc(tmp_path, **cfg):
    s = BotService(rt(), tmp_path / "bot.json", None)
    s.cfg.update(cfg)
    return s


def book(s, coin, sec, mid, half=0.05):
    s.on_message({"channel": "l2Book", "data": {"coin": coin, "time": sec * 1000 + 500, "levels": [
        [{"px": str(mid - half), "sz": "1", "n": 1}], [{"px": str(mid + half), "sz": "1", "n": 1}]]}})


def trade(s, coin, ms, wallet, side, usd, px=2500.0):
    users = [wallet, OTHER] if side == "B" else [OTHER, wallet]
    s.on_message({"channel": "trades", "data": [{"coin": coin, "side": side, "px": str(px), "sz": str(usd / px),
                                                 "time": ms, "users": users}]})


def run(s, secs, slices=10, drift=0.01, start=T0, side="B", usd=30_000, coin="ETH", from_k=0):
    """A market drifting `drift` a second, with a TWAP of `slices` slices every 30s from start+100."""
    for k in range(from_k, secs):
        book(s, coin, start + k, 2500.0 + drift * k)
        if 100 <= k < 100 + 30 * slices and (k - 100) % 30 == 0:
            trade(s, coin, (start + k) * 1000, TW, side, usd)
        if k % 5 == 0:
            s.tick()


def test_settle_reads_the_move_and_the_worst_point():
    rows = {s: [100.0 - 0.01, 100.0 + 0.01, 100.0] for s in range(0, 10)}
    rows.update({s: [100.0 + (s - 9) * 0.01 - 0.01, 100.0 + (s - 9) * 0.01 + 0.01, 100.0 + (s - 9) * 0.01]
                 for s in range(10, 60)})
    rows[20][0] = 99.9                                       # a dip on the best bid
    r = settle(rows, 0, 6, 30, 1)
    assert r["bps"] == pytest.approx((100.27 / 100 - 1) * 1e4)
    assert r["adv"] == pytest.approx(10.0)                   # 100 → 99.9 is 10 bps against a long
    assert settle(rows, 0, 6, 30, -1)["bps"] < 0
    gap = {s: v for s, v in rows.items() if not 15 <= s <= 25}
    assert settle(gap, 0, 6, 30, 1) is None                  # an 11s hole in the feed
    assert pt_of({"bps": 5, "adv": 13}, 841, "ETH") == -1.0   # 13 bps against at 841x (bust 11.9 away)
    assert pt_of({"bps": 5, "adv": 13}, 200, "ETH") > 0
    assert pt_of({"bps": -3, "adv": 4}, 200, "ETH") == pytest.approx(-3e-4 * 200)


def test_a_big_twap_is_spotted_shown_running_settled_and_shadow_traded(tmp_path):
    s = svc(tmp_path, hold="end")
    run(s, 100 + 30 * 3 + 10, slices=10)                       # just past the 4th slice
    p = s.public()
    assert len(p["running"]) == 1 and p["today"]["spotted"] == 1
    r = p["running"][0]
    assert r["coin"] == "ETH" and r["side"] == 1 and r["slices"] == 4 and r["usd"] == 120_000
    a = s.live()["alert"]
    assert a["coin"] == "ETH" and a["running"] and a["hold_s"] is None and a["fill_s"] == 6
    run(s, 4000, slices=10, from_k=100 + 30 * 3 + 10)            # the rest of the TWAP, then quiet
    p = s.public()
    assert not p["running"] and len(p["recent"]) == 1
    done = p["recent"][0]
    assert done["slices"] == 10 and done["settled"] and done["bps"] > 0 and done["pt"] > 0
    assert p["record"]["day"]["end"]["n"] == 1 and p["record"]["day"]["300"]["n"] == 1
    assert s.shadow["trades"] == 1 and s.shadow["balance"] > 50
    assert s.live()["alert"] is None                           # its "close now" moment has passed


def test_the_banner_says_close_when_the_twap_stops(tmp_path):
    s = svc(tmp_path, hold="end")
    run(s, 100 + 30 * 5 + 40, slices=6)                        # 6 slices, then 40s of nothing
    a = s.live()["alert"]
    assert a is not None and not a["running"]                  # stopped: still shown, to say close now


def test_a_fixed_hold_and_alerts_off(tmp_path):
    s = svc(tmp_path, hold="300")
    run(s, 900, slices=8)
    assert s.shadow["trades"] == 1
    assert s.public()["recent"][0]["bps"] == pytest.approx(s.twaps.records[0]["results"]["300"]["bps"], abs=0.01)
    q = svc(tmp_path / "q", alerts=False)
    run(q, 4000, slices=8)
    assert q.public()["record"]["all"]["end"]["n"] == 1         # still scored
    assert q.shadow["trades"] == 0 and q.live()["alert"] is None and q.status() == "silent"


def test_small_twaps_and_other_coins_are_ignored(tmp_path):
    s = svc(tmp_path)
    run(s, 600, slices=8, usd=5_000)
    run(s, 600, slices=8, coin="SOL", start=T0 + 1000)
    assert not s.twaps.records


def test_settings_validate_restart_the_shadow_and_survive_a_restart(tmp_path):
    s = svc(tmp_path)
    s.shadow["balance"] = 70.0
    with pytest.raises(BotError):
        s.save_settings({"hold": "45"})
    with pytest.raises(BotError):
        s.save_settings({"leverage": 5000})
    with pytest.raises(BotError):
        s.save_settings({"nope": 1})
    s.save_settings({"telegram": True})
    assert s.shadow["balance"] == 70.0                        # not a money setting
    s.save_settings({"hold": "900", "leverage": 500})
    assert s.shadow["balance"] == 50.0                        # restarted
    again = BotService(rt(), tmp_path / "bot.json", None)
    assert again.cfg["hold"] == "900" and again.cfg["leverage"] == 500 and again.cfg["telegram"] is True


def test_the_big_mover_bot_file_keeps_money_settings_but_not_its_shadow(tmp_path):
    (tmp_path / "bot.json").write_text(json.dumps({"settings": {"on": False, "telegram": True, "leverage": 841,
                                                                "cutoff_bps": 8, "fill_s": 6},
                                                   "shadow": {"balance": 12.0}, "since_ms": 5}))
    s = BotService(rt(), tmp_path / "bot.json", None)
    assert s.cfg["leverage"] == 841 and s.cfg["telegram"] is True and s.cfg["alerts"] is True
    assert "cutoff_bps" not in s.cfg and s.shadow["balance"] == 50.0 and s.since_ms > 5


def test_records_are_saved_and_loaded(tmp_path):
    async def go():
        s = BotService(rt(), tmp_path / "bot.json", tmp_path / "bars.sqlite")
        await s.start()
        run(s, 4000, slices=8)
        await s.stop()
        again = BotService(rt(), tmp_path / "bot.json", tmp_path / "bars.sqlite")
        await again.start()
        await again.stop()
        return again
    again = asyncio.run(go())
    assert len(again.twaps.records) == 1 and again.twaps.records[0]["results"]["end"]["bps"] > 0
    assert again.public()["recent"][0]["shadow"] is not None


def test_messages_are_plain():
    rec = {"coin": "ETH", "side": 1, "usd": 120_000, "slices": 4, "mid": 2500.0, "hold": "end"}
    title, lines = spotted_message(rec, {"margin_pct": 20, "balance": 50, "leverage": 200, "hold": "end"})
    assert title == "Big TWAP: ETH LONG"
    assert "buying about $30.0k every 30s ($120.0k so far)" in lines[0]
    assert "within 6s near 2,500.00, close when it stops" in lines[1] and "$10.00 margin" in lines[2]
    assert stopped_message({**rec, "slices": 10, "usd": 300_000})[0] == "ETH TWAP stopped: close your long"


def test_parse_plan_picks_the_twap_on_this_coin_and_side():
    from api.bot import parse_plan
    t = T0 * 1000
    hist = [
        {"time": T0, "state": {"coin": "ETH", "side": "B", "sz": "100", "minutes": 30, "timestamp": t,
                               "executedSz": "10", "executedNtl": "25000"}, "status": {"status": "activated"}},
        {"time": T0, "state": {"coin": "ETH", "side": "A", "sz": "999", "minutes": 30, "timestamp": t}},
        {"time": T0, "state": {"coin": "BTC", "side": "B", "sz": "999", "minutes": 30, "timestamp": t}},
        {"time": T0 - 86_400, "state": {"coin": "ETH", "side": "B", "sz": "5", "minutes": 10,
                                        "timestamp": t - 86_400_000}},          # an old one, long finished
    ]
    p = parse_plan(hist, "ETH", 1, t + 90_000, 2500.0)
    assert p["sz"] == 100 and p["minutes"] == 30 and p["ends_ms"] == t + 30 * 60_000
    assert p["total_usd"] == 250_000 and p["done_usd"] == 25_000 and p["status"] == "activated"
    assert parse_plan(hist, "ETH", -1, t + 90_000, 2500.0)["sz"] == 999
    assert parse_plan(hist, "SOL", 1, t, 1.0) is None and parse_plan([], "ETH", 1, t, 1.0) is None


def test_the_live_bot_looks_up_the_plan_and_shows_whats_left(tmp_path, monkeypatch):
    import httpx

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"time": T0 + 100, "status": {"status": "activated"},
                     "state": {"coin": "ETH", "side": "B", "sz": "240", "minutes": 10, "timestamp": (T0 + 100) * 1000,
                               "executedSz": "36", "executedNtl": "90000"}}]

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            assert json == {"type": "twapHistory", "user": TW}
            return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    s = BotService(SimpleNamespace(mode="live", alerts=None), tmp_path / "bot.json", None)

    async def go():
        run(s, 100 + 30 * 3 + 10, slices=10)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    p = s.public()["running"][0]["plan"]
    assert p["minutes"] == 10 and p["ends_ms"] == (T0 + 100 + 600) * 1000
    assert p["total_usd"] == pytest.approx(240 * 2501.9, rel=1e-3)          # at the mid when spotted
    assert p["left_usd"] == pytest.approx(p["total_usd"] - 120_000, abs=1)   # minus the 4 slices seen
    assert s.live()["alert"]["plan"]["minutes"] == 10
    title, lines = spotted_message(s.twaps.records[0], s.cfg)
    assert "Its plan: $600.5k over 10 min" in lines[0]

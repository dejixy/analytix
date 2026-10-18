"""Basket 2 live (api/informed.py): spots hidden orders, fades the reliably wrong, scores everything."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from api.bot import BotError
from api.informed import FILL_S, HOLD_S, InformedBasket, fade_message, hl_result

T0 = 1_790_000_000
WRONG = "0x" + "e" * 40
NEW = "0x" + "f" * 40
OTHER = "0x" + "c" * 40


def rt(mode="replay"):
    return SimpleNamespace(mode=mode, alerts=None)


def basket(tmp_path, **cfg):
    b = InformedBasket(rt(), tmp_path / "bot_informed.json", None, scores_file=tmp_path / "scores.json")
    b.set_scores({"built_ms": 1, "k": 10, "wallets": {WRONG: {"n": 30, "mean": -12.0, "score": -9.0}}})
    b.cfg.update(cfg)
    return b


def book(b, coin, sec, mid):
    b.on_message({"channel": "l2Book", "data": {"coin": coin, "time": sec * 1000 + 500, "levels": [
        [{"px": str(mid - 0.01), "sz": "1", "n": 1}], [{"px": str(mid + 0.01), "sz": "1", "n": 1}]]}})


def buy(b, coin, ms, wallet, usd, px=100.0):
    b.on_message({"channel": "trades", "data": [{"coin": coin, "side": "B", "px": str(px), "sz": str(usd / px),
                                                 "time": ms, "users": [wallet, OTHER]}]})


def run(b, coin="SOL", wallet=WRONG, secs=1200, drift=-0.002, start=T0):
    """A wallet buys $20k every 20s for 2 minutes from start+10; price drifts down after (its push reverses)."""
    for k in range(secs):
        book(b, coin, start + k, 100.0 + drift * k)
        if 10 <= k <= 130 and (k - 10) % 20 == 0:
            buy(b, coin, (start + k) * 1000, wallet, 20_000)
        if k % 5 == 0:
            b.tick()


def test_a_wrong_wallets_hidden_order_is_faded_and_settled(tmp_path):
    b = basket(tmp_path)
    run(b, "SOL")
    assert len(b.records) == 1
    rec = b.records[0]
    assert rec["fade"] and rec["side"] == -1 and rec["order_side"] == 1 and rec["score"] == -9.0
    assert rec["t_ms"] == (T0 + 70) * 1000                     # the 4th piece: $80k, 60s in
    assert rec["settled"] and rec["result"]["bps"] > 0          # price fell: the fade (a short) made money
    assert b.shadows["hl"]["trades"] == 1 and b.shadows["hl"]["balance"] > 50
    assert b.shadows["pt"]["trades"] == 0                       # SOL isn't on Papertrade
    p = b.public()
    assert p["record"]["day"]["fade"]["n"] == 1 and p["recent"][0]["hl"] > 0
    assert p["today"] == {"orders": 1, "fades": 1} and p["status"] == "testing"


def test_unscored_wallets_are_scored_as_the_control_but_not_faded(tmp_path):
    b = basket(tmp_path)
    run(b, "BTC", wallet=NEW)
    rec = b.records[0]
    assert not rec["fade"] and rec["score"] is None and rec["settled"]
    assert b.shadows["hl"]["trades"] == 0 and b.shadows["pt"]["trades"] == 0
    p = b.public()["record"]["day"]
    assert p["all"]["n"] == 1 and p["fade"]["n"] == 0


def test_btc_fades_go_to_the_papertrade_shadow_too(tmp_path):
    b = basket(tmp_path)
    run(b, "BTC")
    assert b.shadows["pt"]["trades"] == 1 and b.shadows["hl"]["trades"] == 1


def test_alerts_are_off_until_turned_on(tmp_path):
    b = basket(tmp_path)
    run(b, "ETH", secs=200)
    assert b.live()["alert"] is None
    c = basket(tmp_path / "c", alerts=True)
    run(c, "ETH", secs=200)
    a = c.live()["alert"]
    assert a["kind"] == "fade" and a["side"] == -1 and a["hold_s"] == HOLD_S and a["fill_s"] == FILL_S
    assert c.status() == "live"


def test_settings_and_shadows(tmp_path):
    b = basket(tmp_path)
    b.shadows["hl"]["balance"] = 70.0
    with pytest.raises(BotError):
        b.save_settings({"threshold": 5})
    with pytest.raises(BotError):
        b.save_settings({"coins": ["DOGE"]})
    b.save_settings({"alerts": True})
    assert b.shadows["hl"]["balance"] == 70.0
    b.save_settings({"threshold": -8})
    assert b.shadows["hl"]["balance"] == 50.0
    again = InformedBasket(rt(), tmp_path / "bot_informed.json", None, scores_file=tmp_path / "none.json")
    assert again.cfg["threshold"] == -8 and again.cfg["alerts"] is True and again.status() == "warming"


def test_hyperliquid_result_caps_leverage_and_busts():
    assert hl_result({"bps": 13.0, "adv": 2.0}, 200, "HYPE") == pytest.approx(10 * 10 / 1e4)   # 10x max, 3 bps fees
    assert hl_result({"bps": 13.0, "adv": 600.0}, 200, "BTC") == -1.0                           # 40x busts 250 bps away
    assert hl_result({"bps": -5.0, "adv": 10.0}, 20, "SOL") == pytest.approx(20 * -8 / 1e4)


def test_orders_are_saved_and_loaded(tmp_path):
    async def go():
        b = InformedBasket(rt(), tmp_path / "bot_informed.json", tmp_path / "bars.sqlite", scores_file=tmp_path / "s.json")
        b.set_scores({"wallets": {WRONG: {"n": 30, "mean": -12.0, "score": -9.0}}})
        await b.start()
        run(b, "SOL", start=int(__import__("time").time()) - 1300)
        await b.stop()
        again = InformedBasket(rt(), tmp_path / "bot_informed.json", tmp_path / "bars.sqlite", scores_file=tmp_path / "s.json")
        await again.start()
        await again.stop()
        return again
    again = asyncio.run(go())
    assert len(again.records) == 1 and again.records[0]["settled"] and again.shadows["hl"]["trades"] == 1


def test_the_message_is_plain():
    rec = {"coin": "SOL", "side": -1, "order_side": 1, "usd": 80_000, "score": -9.0, "n": 30, "mid": 100.0}
    title, lines = fade_message(rec, {})
    assert title == "Fade: SOL SHORT"
    assert "usually wrong is buying in pieces ($80.0k so far; score -9.0 over 30 orders)" in lines[0]
    assert "close after 15 min" in lines[1]


def test_the_live_move_shows_even_when_the_feed_skips_the_entry_second(tmp_path):
    b = basket(tmp_path)
    for k in range(300):
        if k != 76:                                              # the entry second (spotted at +70, in 6s later) is missing
            book(b, "SOL", T0 + k, 100.0 - 0.002 * k)
        if 10 <= k <= 130 and (k - 10) % 20 == 0:
            buy(b, "SOL", (T0 + k) * 1000, WRONG, 20_000)
    row = b.public()["open"][0]
    assert row["move_now"] == pytest.approx(-1 * ((100.0 - 0.002 * 299) / (100.0 - 0.002 * 75) - 1) * 1e4)
    assert row["move_now"] > 0                                   # price fell: the short is up


def test_the_record_shows_how_far_fades_dipped_before_closing(tmp_path):
    b = basket(tmp_path)
    run(b, "SOL")
    rec = b.public()["record"]["day"]["fade"]
    assert rec["dip"] == rec["dip_max"] == b.records[0]["result"]["adv"] >= 0
    assert b.public()["recent"][0]["dip"] == round(rec["dip"], 2)

"""Basket 3, degen (api/degen.py): the informed basket's SOL and HYPE fades, a big share of the pot each, weeks that
end at 2x or -50%."""
import pytest

from api.degen import DOWN, UP, WEEK_MS, DegenBasket, result
from api.informed import HL_MAX_LEV
from tests.test_informed_live import NEW2, T0, WRONG, basket, book, buy


def pair(tmp_path):
    inf = basket(tmp_path)
    dg = DegenBasket(inf.rt, tmp_path / "bot_degen.json", inf)
    dg.state["seen_ms"] = 0                       # the test's clock is in the past
    return inf, dg


def feed(inf, dg, coins=("SOL",), secs=1200, drift=-0.002, start=T0, second_sol=False):
    for k in range(secs):
        for c in coins:
            book(inf, c, start + k, 100.0 + drift * k)
        if 10 <= k <= 130 and (k - 10) % 20 == 0:
            for c in coins:
                buy(inf, c, (start + k) * 1000, WRONG, 20_000)
        if second_sol and 200 <= k <= 320 and (k - 200) % 20 == 0:
            buy(inf, "SOL", (start + k) * 1000, NEW2, 20_000)
        if k % 5 == 0:
            inf.tick()
            dg.tick()


def test_a_fade_is_sized_from_the_pot_and_booked(tmp_path):
    inf, dg = pair(tmp_path)
    feed(inf, dg, secs=300)                                         # spotted at +70, open
    p = dg.public()
    assert len(p["open"]) == 1 and p["open"][0]["margin"] == 20.0 and p["open"][0]["lev"] == HL_MAX_LEV["SOL"]
    assert p["open"][0]["move_now"] > 0 and p["status"] == "testing"
    feed(inf, dg, start=T0, secs=1200)
    rec = inf.records[0]
    want = 20.0 * result(rec["result"], "SOL")
    assert not dg.state["open"] and dg.state["trades"] == 1 and dg.state["pot"] == pytest.approx(50.0 + want)
    assert want > 0 and dg.public()["recent"][0]["pnl"] == pytest.approx(want, abs=1e-3)


def test_only_its_coins(tmp_path):
    inf, dg = pair(tmp_path)
    inf.cfg["coins"] = ["BTC", "SOL", "HYPE"]
    feed(inf, dg, coins=("BTC",))
    assert inf.records[0]["fade"] and dg.state["trades"] == 0 and not dg.state["open"]


def test_one_open_per_coin_and_open_trades_dont_size_the_next(tmp_path):
    inf, dg = pair(tmp_path)
    feed(inf, dg, coins=("SOL", "HYPE"), secs=300, second_sol=True)
    assert [o["coin"] for o in dg.state["open"]] == ["SOL", "HYPE"]   # the 2nd SOL fade waits its turn
    assert all(o["margin"] == 20.0 for o in dg.state["open"])          # both from the $50 pot
    feed(inf, dg, coins=("SOL", "HYPE"), secs=1200, second_sol=True)
    assert dg.state["trades"] == 2


def test_a_week_ends_at_2x_and_starts_again(tmp_path):
    inf, dg = pair(tmp_path)
    dg.state["pot"] = 1.99 * dg.state["start"]
    feed(inf, dg)
    st = dg.state
    assert [w["result"] for w in st["weeks"]] == ["2x"] and st["pot"] == st["start"] and st["week_ms"] == 0
    assert st["banked"] > (UP - 1) * st["start"] - 1


def test_a_week_ends_at_half(tmp_path):
    inf, dg = pair(tmp_path)
    dg.state["pot"] = 0.51 * dg.state["start"]
    feed(inf, dg, drift=+0.002)                                     # the push holds: the fade loses
    st = dg.state
    assert [w["result"] for w in st["weeks"]] == ["-50%"] and st["banked"] < -(1 - DOWN) * st["start"]


def test_a_quiet_week_ends_after_7_days(tmp_path):
    inf, dg = pair(tmp_path)
    feed(inf, dg)
    dg.state["pot"] = 60.0
    inf.now_ms["SOL"] = dg.state["week_ms"] + WEEK_MS + 1
    dg.tick()
    assert dg.state["weeks"][-1]["result"] == "neither" and dg.state["banked"] == pytest.approx(10.0)


def test_an_open_trade_survives_a_reload(tmp_path):
    inf, dg = pair(tmp_path)
    feed(inf, dg, secs=300)
    dg._save()
    again = DegenBasket(inf.rt, tmp_path / "bot_degen.json", inf)
    assert len(again.state["open"]) == 1
    feed(inf, again, start=T0, secs=1200)
    assert again.state["trades"] == 1 and not again.state["open"]


def test_settings(tmp_path):
    from api.bot import BotError
    inf, dg = pair(tmp_path)
    dg.state["pot"] = 70.0
    dg.save_settings({"coins": ["HYPE"]})
    assert dg.state["pot"] == 70.0 and dg.cfg["coins"] == ["HYPE"]
    dg.save_settings({"pot_pct": 25})
    assert dg.state["pot"] == 50.0
    with pytest.raises(BotError):
        dg.save_settings({"pot_pct": 0})
    assert result({"bps": 10.0, "adv": 260.0}, "SOL") == -1.0          # 20x: maintenance margin 250 bps away
    assert result({"bps": 10.0, "adv": 5.0}, "SOL") == pytest.approx(20 * 7e-4)

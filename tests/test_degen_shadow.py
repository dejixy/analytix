"""The degen shadow in the TWAP basket (api/bot.py): strong TWAPs, 75% of the pot at 100x, weeks end at 2x or -50%."""
from types import SimpleNamespace

import pytest

import api.bot as bot
from api.bot import DEGEN_FRAC, DEGEN_LEV, DEGEN_WEEK_MS, BotService
from engine.bigmovers import pt_of

T0 = 1_790_000_000
TW = "0x" + "a" * 40
OTHER = "0x" + "c" * 40


@pytest.fixture(autouse=True)
def strong(monkeypatch):
    monkeypatch.setattr(bot, "strength", lambda rec, now, rate: {"strong": True, "known": True, "share": 0.05})


def svc(tmp_path):
    s = BotService(SimpleNamespace(mode="replay", alerts=None), tmp_path / "bot.json", None)
    s.cfg.update({"alert_on": "strong"})
    return s


def book(s, sec, mid):
    s.on_message({"channel": "l2Book", "data": {"coin": "ETH", "time": sec * 1000 + 500, "levels": [
        [{"px": str(mid - 0.05), "sz": "1", "n": 1}], [{"px": str(mid + 0.05), "sz": "1", "n": 1}]]}})


def twap(s, start, secs=1400, drift=0.01):
    for k in range(secs):
        book(s, start + k, 2500.0 + drift * k)
        if 100 <= k < 400 and (k - 100) % 30 == 0:
            s.on_message({"channel": "trades", "data": [{"coin": "ETH", "side": "B", "px": "2500", "sz": str(30_000 / 2500),
                                                          "time": (start + k) * 1000, "users": [TW, OTHER]}]})
        if k % 5 == 0:
            s.tick()


def test_a_strong_twap_is_traded_with_most_of_the_pot(tmp_path):
    s = svc(tmp_path)
    twap(s, T0)
    dg = s.degen
    rec = next(r for r in s.twaps.records if r.get("degen"))
    want = DEGEN_FRAC * dg["start"] * pt_of(rec["results"]["900"], DEGEN_LEV, "ETH")
    assert dg["trades"] == 1 and rec["degen"]["pnl"] == pytest.approx(want, abs=1e-3) and dg["pot"] > dg["start"]
    p = s.public()["degen"]
    assert p["lev"] == 100 and p["count"] == {"2x": 0, "-50%": 0, "neither": 0} and p["week_left_s"] > 0


def test_a_week_ends_at_2x_and_starts_again(tmp_path):
    s = svc(tmp_path)
    s.degen["pot"] = 1.95 * s.degen["start"]
    twap(s, T0)
    dg = s.degen
    assert [w["result"] for w in dg["weeks"]] == ["2x"]
    assert dg["pot"] == dg["start"] and dg["banked"] > 0.95 * dg["start"]


def test_a_week_ends_at_minus_half(tmp_path):
    s = svc(tmp_path)
    s.degen["pot"] = 0.52 * s.degen["start"]
    twap(s, T0, drift=-0.01)                                 # it goes against the TWAP: a loss
    assert [w["result"] for w in s.degen["weeks"]] == ["-50%"] and s.degen["banked"] < -0.45 * s.degen["start"]


def test_a_quiet_week_ends_after_7_days(tmp_path):
    s = svc(tmp_path)
    s.degen["pot"], s.degen["week_ms"] = 1.3 * s.degen["start"], T0 * 1000
    s._degen_week(T0 * 1000 + DEGEN_WEEK_MS + 1)
    assert s.degen["weeks"][0]["result"] == "neither" and s.degen["pot"] == s.degen["start"]
    assert s.degen["banked"] == pytest.approx(0.3 * s.degen["start"])


def test_reset_and_a_reload_keep_or_clear_it(tmp_path):
    s = svc(tmp_path)
    s.degen["pot"] = 70.0
    s._save()
    again = BotService(SimpleNamespace(mode="replay", alerts=None), tmp_path / "bot.json", None)
    assert again.degen["pot"] == 70.0
    again.reset_shadow()
    assert again.degen["pot"] == again.degen["start"] and not again.degen["weeks"]

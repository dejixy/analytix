"""The bot's baskets (api/baskets.py): separate strategies with separate balances, behind one bot and one API."""
from types import SimpleNamespace

import pytest

from api.baskets import Bot
from api.bot import BotError


def bot(tmp_path, shown=("twap", "informed", "degen")):
    return Bot(SimpleNamespace(mode="replay", alerts=None), tmp_path / "bot.json", None, shown=shown)


def test_each_basket_has_its_own_balance_and_settings(tmp_path):
    b = bot(tmp_path)
    p = b.public("informed")
    assert p["basket"] == "informed" and p["status"] == "warming"            # no wallet scores yet
    assert [x["key"] for x in p["baskets"]] == ["twap", "informed", "degen"]
    b.save_settings("informed", {"balance": 100})
    assert b.public("informed")["shadow"]["balance"] == 100
    assert b.public("twap")["shadow"]["balance"] == 50                # the TWAP basket is untouched
    b.twap.shadow["balance"] = 60.0
    b.reset_shadow("informed")
    assert b.twap.shadow["balance"] == 60.0
    again = bot(tmp_path)
    assert again.informed.cfg["balance"] == 100 and (tmp_path / "bot_informed.json").exists()
    d = b.public("degen")
    assert d["basket"] == "degen" and d["pot"] == 50 and d["settings"]["coins"] == ["SOL", "HYPE"]
    b.save_settings("degen", {"balance": 200})
    assert b.public("degen")["shadow"]["balance"] == 200 and b.public("informed")["shadow"]["balance"] == 100


def test_unknown_baskets_and_settings_are_errors(tmp_path):
    b = bot(tmp_path)
    with pytest.raises(BotError):
        b.save_settings("informed", {"hold": "900"})
    with pytest.raises(BotError):
        b.public("nope")
    assert b.public()["basket"] == "twap"


def test_live_carries_the_twap_call_with_its_basket(tmp_path):
    b = bot(tmp_path)
    b.twap.live = lambda: {"status": "live", "alert": {"id": "x", "coin": "ETH"}}
    lv = b.live()
    assert lv["alert"]["basket"] == "twap" and lv["alert"]["basket_name"] == "TWAPs"


def test_hidden_baskets_keep_running_but_stay_off_the_panel_and_banner(tmp_path):
    b = bot(tmp_path, shown=("degen",))
    p = b.public()
    assert p["basket"] == "degen" and [x["key"] for x in p["baskets"]] == ["degen"]
    assert b.public("twap")["basket"] == "twap"                           # still readable by name
    assert b.twap.hidden and b.informed.hidden and not b.degen.hidden
    b.twap.live = lambda: {"status": "live", "alert": {"id": "x", "coin": "ETH"}}
    assert b.live()["alert"] is None                                      # no call on the banner
    sent = []
    b.twap.rt = SimpleNamespace(mode="live", alerts=SimpleNamespace(notify=sent.append))
    b.twap._notify(("title", ("line",)), "ETH")
    assert sent == []                                                     # and no Telegram

"""Surviving a restart (api/livestate.py): both baskets save what's in flight and pick it back up, and a trade that
spans the restart is still scored."""
from types import SimpleNamespace

from api import livestate
from api.bot import BotService
from api.informed import InformedBasket

T0 = 1_790_000_000
TW = "0x" + "a" * 40
WRONG = "0x" + "e" * 40
OTHER = "0x" + "c" * 40


def live():
    return SimpleNamespace(mode="live", alerts=None)


def book(b, coin, sec, mid):
    b.on_message({"channel": "l2Book", "data": {"coin": coin, "time": sec * 1000 + 500, "levels": [
        [{"px": str(mid - 0.01), "sz": "1", "n": 1}], [{"px": str(mid + 0.01), "sz": "1", "n": 1}]]}})


def buy(b, coin, ms, wallet, usd, px=100.0):
    b.on_message({"channel": "trades", "data": [{"coin": coin, "side": "B", "px": str(px), "sz": str(usd / px),
                                                 "time": ms, "users": [wallet, OTHER]}]})


def test_stale_for_only_stretches_across_a_restart():
    rows = {s: [1, 1, 1] for s in list(range(0, 100)) + list(range(130, 200))}
    assert livestate.stale_for(rows, [], 10, 190) == 5
    assert livestate.stale_for(rows, [99], 10, 190) == 31 + 5           # last save at 99, prices again from 130
    assert livestate.stale_for(rows, [99], 150, 190) == 5               # the restart was before this trade
    assert livestate.pauses_after({"saved_ms": 5_000_000, "pauses": [4000]}) == [4000, 5000]
    assert livestate.pauses_after(None) == []
    assert livestate.ring_load(livestate.ring_dump({"BTC": {5: [1.0, 2.0, 1.5]}})) == {"BTC": {5: [1.0, 2.0, 1.5]}}


# ── informed basket ──────────────────────────────────────────────────────────
def basket(tmp_path):
    b = InformedBasket(live(), tmp_path / "bot_informed.json", None, scores_file=tmp_path / "scores.json")
    b.set_scores({"built_ms": 1, "k": 10, "wallets": {WRONG: {"n": 30, "mean": -12.0, "score": -9.0}}})
    return b


def feed(b, ks, coin="SOL", drift=-0.002):
    """A wallet buys $20k every 20s from +10 to +130; price drifts down (its push reverses)."""
    for k in ks:
        book(b, coin, T0 + k, 100.0 + drift * k)
        if 10 <= k <= 130 and (k - 10) % 20 == 0:
            buy(b, coin, (T0 + k) * 1000, WRONG, 20_000)
        if k % 5 == 0:
            b.tick()


def test_an_open_fade_survives_a_restart_and_is_scored(tmp_path):
    b = basket(tmp_path)
    feed(b, range(0, 300))                                   # spotted at +70, in at +76, open
    assert len(b.public()["open"]) == 1
    b._keep()
    again = basket(tmp_path)
    again._restore()
    assert len(again.public()["open"]) == 1
    feed(again, range(330, 1200))                             # 30s down for the restart
    rec = again.records[0]
    assert rec["settled"] and rec["result"] and rec["result"]["bps"] > 0
    assert again.shadows["hl"]["trades"] == 1


def test_without_the_restart_window_it_would_be_a_feed_gap(tmp_path):
    b = basket(tmp_path)
    feed(b, range(0, 300))
    b._pauses = []
    rows = {s: v for s, v in b.ring["SOL"].items()}
    assert livestate.stale_for(rows, [], 76, 976) == 5        # the normal limit: a 30s hole would fail settle()


def test_a_hidden_order_half_spotted_carries_on(tmp_path):
    b = basket(tmp_path)
    feed(b, range(0, 55))                                    # 3 pieces, $60k: not spotted yet
    assert not b.records and len(b.ho.open) == 1
    b._keep()
    again = basket(tmp_path)
    again._restore()
    feed(again, range(65, 300))                               # the 4th piece at +70 spots it
    assert len(again.records) == 1 and again.records[0]["pieces"] == 4 and again.records[0]["usd"] == 80_000


def test_nothing_is_saved_outside_live_mode(tmp_path):
    b = InformedBasket(SimpleNamespace(mode="replay", alerts=None), tmp_path / "x.json", None,
                       scores_file=tmp_path / "s.json")
    b._keep()
    assert not (tmp_path / "x_live.json").exists()


# ── TWAP basket ──────────────────────────────────────────────────────────────
def svc(tmp_path):
    s = BotService(live(), tmp_path / "bot.json", None)
    s.lookup = False
    s.cfg.update({"alert_on": "all", "hold": "end"})
    return s


def twap(s, ks, slices=10):
    for k in ks:
        book(s, "ETH", T0 + k, 2500.0 + 0.01 * k)
        if 100 <= k < 100 + 30 * slices and (k - 100) % 30 == 0:
            buy(s, "ETH", (T0 + k) * 1000, TW, 30_000, px=2500.0)
        if k % 5 == 0:
            s.tick()


def test_a_running_twap_survives_a_restart(tmp_path):
    s = svc(tmp_path)
    twap(s, range(0, 260))                                    # 6 slices in, spotted at the 4th
    assert len(s.public()["running"]) == 1
    s._keep()
    again = svc(tmp_path)
    again._restore()
    assert len(again.public()["running"]) == 1 and again._vol
    twap(again, range(275, 4000))                             # 15s down; the beat carries on at +280
    p = again.public()
    assert not p["running"] and len(p["recent"]) == 1
    done = p["recent"][0]
    assert done["slices"] == 10 and done["settled"] and done["bps"] > 0
    assert again.shadow["trades"] == 1

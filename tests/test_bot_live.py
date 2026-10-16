"""The live bot: big-mover alerts (api/bot.py) and their settling (engine/bigmovers.settle / pt_of)."""
import json
from types import SimpleNamespace

import pytest

from api.bot import BotError, BotService, alert_message
from engine.bigmovers import pt_of, settle

T0 = 1_790_000_000          # seconds
BIG = "0x" + "b" * 40
SMALL = "0x" + "c" * 40


def rt():
    return SimpleNamespace(mode="replay", alerts=None)


def svc(tmp_path, n_wallets=12, **cfg):
    s = BotService(rt(), tmp_path / "bot.json", None, list_file=tmp_path / "list.json")
    wallets = [{"wallet": BIG, "n": 40, "mean_bps": 9.0, "t": 4.0, "usd": 2000, "coins": ["ETH"], "last_ms": 0}]
    wallets += [{"wallet": f"0x{k:040x}", "n": 30, "mean_bps": 8.5, "t": 3.0, "usd": 1500, "coins": ["BTC"], "last_ms": 0}
                for k in range(n_wallets - 1)]
    s.set_list({"built_ms": 1, "wallets": wallets})
    s.cfg.update(cfg)
    return s


def book(s, coin, sec, mid, half=0.05):
    s.on_message({"channel": "l2Book", "data": {"coin": coin, "time": sec * 1000 + 500, "levels": [
        [{"px": str(mid - half), "sz": "1", "n": 1}], [{"px": str(mid + half), "sz": "1", "n": 1}]]}})


def trade(s, coin, ms, wallet, side, usd, px=2500.0):
    users = [wallet, SMALL] if side == "B" else [SMALL, wallet]
    s.on_message({"channel": "trades", "data": [{"coin": coin, "side": side, "px": str(px), "sz": str(usd / px),
                                                 "time": ms, "users": users}]})


def run_market(s, coin="ETH", start=T0, secs=120, drift=0.0, base=2500.0):
    for k in range(secs):
        book(s, coin, start + k, base + drift * k)


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


def test_a_listed_wallet_reaching_1k_raises_one_alert_and_others_join_it(tmp_path):
    s = svc(tmp_path)
    run_market(s, secs=5)
    trade(s, "ETH", (T0 + 3) * 1000, BIG, "B", 600)          # $600: not a decision yet
    assert not s.alerts
    trade(s, "ETH", (T0 + 4) * 1000, BIG, "B", 600)          # $1,200 within 30s: alert
    assert len(s.alerts) == 1 and s.alerts[0]["side"] == 1 and s.alerts[0]["announced"]
    other = f"0x{0:040x}"
    trade(s, "ETH", (T0 + 10) * 1000, other, "B", 5000)      # another big mover, same side, 6s later: joins
    assert len(s.alerts) == 1 and s.alerts[0]["wallets"] == [BIG, other]
    trade(s, "ETH", (T0 + 12) * 1000, SMALL, "B", 50_000)    # not listed: ignored
    trade(s, "BTC", (T0 + 12) * 1000, other, "A", 5000)      # other coin: its own alert
    assert len(s.alerts) == 2
    live = s.live()
    assert live["status"] == "live" and live["alert"]["coin"] == "BTC"


def test_alerts_settle_into_the_record_and_the_shadow_balance(tmp_path):
    s = svc(tmp_path, leverage=200.0, balance=50.0, margin_pct=20.0, fill_s=6, hold_s=30)
    run_market(s, secs=80, drift=0.05)                       # steady rise: a long wins
    trade(s, "ETH", (T0 + 2) * 1000, BIG, "B", 2000)
    s.tick()
    assert s.alerts[0]["results"] is not None
    r6 = s.alerts[0]["results"]["6"]
    assert r6["bps"] > 0
    assert s.shadow["trades"] == 1 and s.shadow["balance"] > 50
    rec = s.record()
    assert rec["6"]["n"] == 1 and rec["6"]["win"] == 1.0 and rec["2"]["n"] == 1 and rec["10"]["n"] == 1
    pub = s.public()
    assert pub["alerts"][0]["bps"] == pytest.approx(r6["bps"], abs=0.01) and pub["shadow"]["margin"] == 10.0
    json.dumps(pub)                                          # the dashboard can read it


def test_one_trade_at_a_time_and_no_alerts_while_the_list_is_short(tmp_path):
    s = svc(tmp_path)
    run_market(s, secs=100)
    trade(s, "ETH", (T0 + 2) * 1000, BIG, "B", 2000)
    trade(s, "ETH", (T0 + 3) * 1000, BIG, "A", 2000)         # the other side 1s later: its own alert
    s.tick()
    assert len(s.alerts) == 2 and s.shadow["trades"] == 1    # the second came while the first was open
    short = svc(tmp_path / "x", n_wallets=5)
    run_market(short, secs=5)
    trade(short, "ETH", (T0 + 2) * 1000, BIG, "B", 2000)
    assert short.status() == "warming" and not short.alerts[0]["announced"] and short.alerts[0]["why"] == "warming"


def test_auto_pause_and_resume(tmp_path):
    s = svc(tmp_path)
    for k in range(40):                                      # 40 losing alerts
        s.alerts.append({"id": str(k), "coin": "ETH", "side": 1, "t_ms": T0 * 1000 + k, "announced": True,
                         "results": {"6": {"bps": -2.0, "adv": 3.0}}, "wallets": [BIG], "usd": 2000})
    s._check_pause(quiet=True)
    assert s.paused and s.status() == "paused" and "averaged -2.0 bps" in s.pause_reason
    for k in range(50):
        s.alerts.append({"id": f"w{k}", "coin": "ETH", "side": 1, "t_ms": T0 * 1000 + 100 + k, "announced": True,
                         "results": {"6": {"bps": 3.0, "adv": 1.0}}, "wallets": [BIG], "usd": 2000})
    s._check_pause(quiet=True)
    assert not s.paused and s.status() == "live"


def test_settings_validate_restart_the_shadow_and_survive_a_restart(tmp_path):
    s = svc(tmp_path)
    s.shadow["balance"] = 80.0
    with pytest.raises(BotError):
        s.save_settings({"leverage": 5000})
    with pytest.raises(BotError):
        s.save_settings({"coins": ["DOGE"]})
    with pytest.raises(BotError):
        s.save_settings({"nonsense": 1})
    s.save_settings({"hold_s": 45})
    assert s.shadow["balance"] == 80.0                       # hold doesn't touch the money
    s.save_settings({"leverage": 841, "balance": 15})
    assert s.shadow["balance"] == 15.0 and s.shadow["start"] == 15.0
    again = BotService(rt(), tmp_path / "bot.json", None, list_file=tmp_path / "list.json")
    assert again.cfg["leverage"] == 841 and again.cfg["hold_s"] == 45 and again.shadow["start"] == 15.0


def test_the_old_rule_bot_settings_are_set_aside(tmp_path):
    (tmp_path / "bot.json").write_text(json.dumps({"rules": [{"id": "x"}], "auto": {"on": True}}))
    s = BotService(rt(), tmp_path / "bot.json", None, list_file=tmp_path / "list.json")
    assert s.cfg["leverage"] == 200.0
    assert json.loads((tmp_path / "bot.old.json").read_text())["rules"][0]["id"] == "x"


def test_alert_message_is_plain():
    a = {"coin": "ETH", "side": -1, "usd": 8200, "mid": 2503.45, "t_ms": T0 * 1000, "fill_s": 6, "hold_s": 30}
    title, lines = alert_message(a, {"leverage": 200.0, "margin_pct": 20.0, "balance": 50.0})
    assert title == "ETH SHORT now"
    assert lines[0] == "A big mover sold $8.2k at 2,503.45." and "within 6s" in lines[1] and lines[2] == "200x, $10 margin."
    assert all("—" not in x for x in (title, *lines))

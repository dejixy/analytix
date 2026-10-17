"""scripts/botCheck.py: replays recordings through the live bot and compares with the live alerts."""
import json
import sqlite3
import sys

from scripts import botCheck

T0 = 1_790_000_000
BIG = "0x" + "b" * 40
OTHER = "0x" + "c" * 40


def test_the_check_replays_the_bot_and_compares_with_live(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "rec"
    rec.mkdir()
    lines = []
    for k in range(3000):
        ms = (T0 + k) * 1000 + 100
        mid = 2500 + 0.01 * k
        lines.append({"recv_ms": ms, "msg": {"channel": "l2Book", "data": {"coin": "ETH", "time": ms, "levels": [
            [{"px": f"{mid - 0.05:.2f}", "sz": "1", "n": 1}], [{"px": f"{mid + 0.05:.2f}", "sz": "1", "n": 1}]]}}})
        if k in (100, 1000, 2000):                            # three big-mover buys, minutes apart
            lines.append({"recv_ms": ms + 1, "msg": {"channel": "trades", "data": [
                {"coin": "ETH", "side": "B", "px": str(mid), "sz": str(4000 / mid), "time": ms, "users": [BIG, OTHER]}]}})
    (rec / "a.jsonl").write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    wallets = [{"wallet": BIG, "n": 40, "mean_bps": 9.0, "t": 4.0, "usd": 2000, "coins": ["ETH"], "last_ms": 0}]
    wallets += [{"wallet": f"0x{k:040x}", "n": 30, "mean_bps": 8.5, "t": 3.0, "usd": 1500, "coins": ["BTC"],
                 "last_ms": 0} for k in range(11)]
    (tmp_path / "list.json").write_text(json.dumps({"built_ms": T0 * 1000, "wallets": wallets}))
    (tmp_path / "bot.json").write_text(json.dumps({"settings": {"coins": ["ETH"]}, "since_ms": T0 * 1000}))
    db = tmp_path / "bars.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bot_alerts (id TEXT PRIMARY KEY, t_ms INTEGER NOT NULL, data TEXT NOT NULL)")
    for k in (100, 1000):                                     # the live bot caught two of the three
        ms = (T0 + k) * 1000 + 100
        con.execute("INSERT INTO bot_alerts VALUES (?, ?, ?)",
                    (f"ETH-{ms}-1", ms, json.dumps({"coin": "ETH", "side": 1, "t_ms": ms})))
    con.commit()
    con.close()
    monkeypatch.setattr(sys, "argv", ["botCheck", str(rec), "--days", "0", "--settings", str(tmp_path / "bot.json"),
                                      "--list", str(tmp_path / "list.json"), "--db", str(db)])
    botCheck.main()
    out = capsys.readouterr().out
    assert "replay 3 alerts" in out and "live 2" in out and "in both 2 · replay only 1 · live only 0" in out
    assert "losing trades" in out
    assert "0x" + "b" * 4 in out                               # the wallet table

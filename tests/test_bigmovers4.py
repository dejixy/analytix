"""scripts/bigMovers4.py runs end to end on a small recording."""
import json
import sys

from scripts import bigMovers4

T0 = 1_790_035_200 - 3600          # an hour before a UTC midnight (1_790_035_200 is one)
BIG = "0x" + "b" * 40
OTHER = "0x" + "c" * 40


def test_v4_runs_and_prints_every_section(tmp_path, monkeypatch, capsys):
    assert 1_790_035_200 % 86_400 == 0
    lines = []
    for k in range(8 * 3600):
        ms = (T0 + k) * 1000 + 100
        mid = 2500 + 0.05 * k
        lines.append({"recv_ms": ms, "msg": {"channel": "l2Book", "data": {"coin": "ETH", "time": ms, "levels": [
            [{"px": f"{mid - 0.05:.2f}", "sz": "1", "n": 1}], [{"px": f"{mid + 0.05:.2f}", "sz": "1", "n": 1}]]}}})
        if k % 300 in (10, 18):                               # every 5 min: $400, then $700 8s later
            usd = 400 if k % 300 == 10 else 700
            lines.append({"recv_ms": ms + 1, "msg": {"channel": "trades", "data": [
                {"coin": "ETH", "side": "B", "px": str(mid), "sz": str(usd / mid), "time": ms, "users": [BIG, OTHER]}]}})
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    monkeypatch.setattr(sys, "argv", ["bigMovers4", str(tmp_path), "--days", "0", "--coins", "ETH", "--trade", "ETH",
                                      "--history-h", "0.5", "--min-n", "3"])
    bigMovers4.main()
    out = capsys.readouterr().out
    assert "1. CHECKING THE CODE" in out and "2. WHEN x WHO" in out and "3. THE BEST 3 ROWS" in out
    assert "reaches $1k (the bot now)" in out and "first order $100+" in out

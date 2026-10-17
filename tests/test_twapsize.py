"""scripts/twapSize.py runs end to end on a small recording."""
import json
import sys

from scripts import twapSize

T0 = 1_790_000_000
TW = "0x" + "a" * 40
OTHER = "0x" + "c" * 40


def test_twap_size_study_runs(tmp_path, monkeypatch, capsys):
    lines = []
    starts = [600 + 2400 * j for j in range(4)]
    for k in range(10_800):
        ms = (T0 + k) * 1000 + 100
        mid = 2500 + 0.01 * k
        lines.append({"recv_ms": ms, "msg": {"channel": "l2Book", "data": {"coin": "ETH", "time": ms, "levels": [
            [{"px": f"{mid - 0.05:.2f}", "sz": "1", "n": 1}], [{"px": f"{mid + 0.05:.2f}", "sz": "1", "n": 1}]]}}})
        trades = [{"coin": "ETH", "side": "A", "px": str(mid), "sz": str(2000 / mid), "time": ms, "users": [OTHER, TW[:-1] + "b"]}]
        for j, s0 in enumerate(starts):                    # four buy TWAPs of different lengths
            if s0 <= k < s0 + 30 * (6 + 4 * j) and (k - s0) % 30 == 0:
                trades.append({"coin": "ETH", "side": "B", "px": str(mid), "sz": str(25_000 / mid), "time": ms,
                               "users": [TW, OTHER]})
        lines.append({"recv_ms": ms + 1, "msg": {"channel": "trades", "data": trades}})
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    monkeypatch.setattr(sys, "argv", ["twapSize", str(tmp_path), "--days", "0", "--coins", "ETH"])
    twapSize.main()
    out = capsys.readouterr().out
    assert "4 big native TWAPs" in out
    for sec in ("1. ALL OF THEM", "2. BY HOW MUCH", "3. BY HOW LONG", "4. BY ITS SHARE", "5. SPOTTED EARLIER", "6. THE COMBINATION"):
        assert sec in out

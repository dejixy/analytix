"""Informed flow: hidden orders, wallet scores (engine/informed.py) and the first study (scripts/informedStudy.py)."""
import json
import sys

import numpy as np
import pytest

from engine.holdclock import Grid
from engine.informed import HiddenOrders, follow, rank_corr, scores, shrink_k

T0 = 1_790_035_200 - 6 * 3600          # six hours before a UTC midnight


def test_a_hidden_order_is_spotted_once_it_has_size_pieces_and_time():
    ho = HiddenOrders(min_usd=50_000, min_pieces=4, min_span_ms=60_000)
    t = T0 * 1000
    for k, usd in enumerate((10_000, 15_000, 5_000, 20_000, 30_000)):
        ho.add("w", "ETH", 1, t + k * 20_000, usd)
        ho.add("w", "ETH", 1, t + k * 20_000 + 500, 1_000)    # the same market order, another level
    ho.add("w", "ETH", -1, t + 200_000, 5_000)                 # turns around: the order is over
    ho.add("x", "ETH", 1, t, 80_000)                           # one big order: not hidden
    ho.add("y", "BTC", -1, t, 20_000)
    ho.add("y", "BTC", -1, t + 300_000, 20_000)                # too long a pause: two orders, neither spotted
    got = ho.finish()
    assert len(got) == 1
    h = got[0]
    assert h.wallet == "w" and h.side == 1 and h.pieces == 5
    assert h.t_detect == t + 3 * 20_000                         # the 4th piece: $53k, 4 pieces, 60s in
    assert h.usd_detect == pytest.approx(53_000) and h.pieces_detect == 4 and not h.regular_detect


def test_a_30s_beat_is_marked_twap_like():
    ho = HiddenOrders(min_usd=50_000)
    for k in range(6):
        ho.add("t", "ETH", 1, T0 * 1000 + k * 30_000, 20_000)
    assert ho.finish()[0].regular_detect


def test_scores_shrink_small_samples_and_only_use_the_past():
    w = np.array([0] * 3 + [1] * 30)
    v = np.array([20.0] * 3 + [5.0, 7.0, 3.0] * 10)
    ready = np.arange(33)
    sc = scores(w, v, ready, 100, 2, k=10.0)
    assert sc["score"][0] == pytest.approx(3 * 20 / 13)        # 3 orders: pulled hard toward 0
    assert sc["score"][1] == pytest.approx(30 * 5 / 40)
    assert scores(w, v, ready, 1, 2, k=10.0)["n"][0] == 2     # only what was known by then
    assert 1 <= shrink_k(w, v) <= 200
    assert rank_corr(np.arange(10.0), np.arange(10.0)) == pytest.approx(1.0)


def test_follow_prices_long_holds_and_busts():
    n = 4000
    mid = 100 * (1 + 1e-5 * np.arange(n))
    g = Grid(np.arange(n) + T0, mid - 0.001, mid + 0.001, mid, np.ones(n, bool))
    m, p = follow(g, np.array([(T0 + 10) * 1000]), np.array([1]), 6, 1800, 200, "ETH")
    assert m[0] == pytest.approx((mid[16 + 1800] / mid[16] - 1) * 1e4) and p[0] > 0
    m, p = follow(g, np.array([(T0 + 10) * 1000]), np.array([-1]), 6, 1800, 200, "ETH")
    assert p[0] == -1.0                                        # 1.8% against a 200x short: bust


def test_the_study_runs_end_to_end(tmp_path, monkeypatch, capsys):
    rng = np.random.default_rng(1)
    lines, mid = [], 2500.0
    smart, noise = "0x" + "a" * 40, ["0x" + f"{k:040x}" for k in range(1, 6)]
    other = "0x" + "c" * 40
    n = 30 * 3600
    push = np.zeros(n)
    for start in range(1800, n - 4000, 3600):                  # every hour the smart wallet buys, then price rises
        push[start + 120:start + 1500] += 0.25 / 1380 * 2500 / 100
    for k in range(n):
        mid += push[k] + rng.normal(0, 0.05)
        ms = (T0 + k) * 1000 + 100
        lines.append({"recv_ms": ms, "msg": {"channel": "l2Book", "data": {"coin": "ETH", "time": ms, "levels": [
            [{"px": f"{mid - 0.05:.2f}", "sz": "1", "n": 1}], [{"px": f"{mid + 0.05:.2f}", "sz": "1", "n": 1}]]}}})
        trades = []
        h = k % 3600
        if 1800 <= h < 1920 and h % 20 == 0:
            trades.append(("B", smart, 15_000))
        if 2700 <= h < 2820 and h % 20 == 0:
            trades.append(("A" if (k // 3600) % 2 else "B", noise[(k // 3600) % 5], 15_000))
        for side, w, usd in trades:
            users = [w, other] if side == "B" else [other, w]
            lines.append({"recv_ms": ms + 1, "msg": {"channel": "trades", "data": [
                {"coin": "ETH", "side": side, "px": f"{mid:.2f}", "sz": str(usd / mid), "time": ms, "users": users}]}})
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n")
    monkeypatch.setattr(sys, "argv", ["informedStudy", str(tmp_path), "--days", "0", "--coins", "ETH",
                                      "--history-h", "4"])
    from scripts import informedStudy
    informedStudy.main()
    out = capsys.readouterr().out
    for sec in ("1. THE HIDDEN ORDERS", "2. WHAT PRICE DOES", "3. DOES BEING RIGHT REPEAT", "4. FOLLOWING THE BEST",
                "5. THE SHAPE OF THE MOVE"):
        assert sec in out

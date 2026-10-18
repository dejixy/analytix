"""The SA study (scripts/saStudy.py): card reads from a replay, scores, and the TP/SL trade."""
from datetime import datetime, timezone

import numpy as np
import pytest

import scripts.saStudy as S
from engine.holdclock import Grid


def grid(mids, spread=0.0):
    m = np.array(mids, dtype=float)
    n = len(m)
    return Grid(np.arange(1000, 1000 + n), m - spread, m + spread, m, np.ones(n, bool))


def test_take_profit_stop_and_timeout():
    up = grid([100.0] * 3 + [100.0 + 0.01 * k for k in range(200)])
    move, held, won = S.ride(up, 1000, 1, 0.001, 0.0005)           # +10 bps target, -5 bps stop
    assert won and move == 0.001 and held > 0
    down = grid([100.0] * 3 + [100.0 - 0.01 * k for k in range(200)], spread=0.01)
    move, held, won = S.ride(down, 1000, 1, 0.001, 0.0005)
    assert not won and move <= -0.0005                             # the stop fills at the real bid, maybe worse
    move, _, won = S.ride(down, 1000, -1, 0.001, 0.0005)           # a short on the same fall hits its target
    assert won
    flat = grid([100.0] * 50)
    assert S.ride(flat, 1000, 1, 0.001, 0.0005)[2] is False         # neither: out at the end of the data


def test_scores_and_the_loop():
    cards = {"1m": {"flow": 1, "who": 1, "book": 0, "forced": 0, "liquidity": 0},
             "10m": {"flow": 1, "trend": 1, "vwap": 1, "who": 0, "book": 0},
             "60m": {"trend": 1, "vwap": 0, "flow": 1, "who": 1, "positioning": 1}}
    sc = S.card_scores(cards)
    assert sc["1m"] == pytest.approx(0.4) and sc["sa"] == pytest.approx((0.4 + 0.6 + 0.8) / 3)
    assert S.sa_lean(cards, "≥0.35") == 1 and S.sa_lean(cards, "all agree") == 1
    g = grid([100.0] * 5 + [100.0 + 0.02 * k for k in range(4000)])
    reads = {"BTC": [(1000 + 60 * k, sc) for k in range(30)]}
    with_ = S.run_bot({"BTC": g}, reads, ["BTC"], "sa", 0.35, 1, 0.2, 0.1, "pt", 200)
    against = S.run_bot({"BTC": g}, reads, ["BTC"], "sa", 0.35, -1, 0.2, 0.1, "pt", 200)
    assert with_ and all(won for *_, won in with_) and not any(won for *_, won in against)
    assert len(with_) < 30                                          # one at a time, a 5 min wait after each


def test_a_replay_gives_card_reads(tmp_path, monkeypatch):
    from ingestion.synthetic import generate_session
    path = generate_session(tmp_path / "eth.jsonl", start=datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(S, "CARDS", {"1m": 60, "10m": 600})        # the synthetic session is 25 min long
    grids, reads = S.replay([path], {"ETH"})
    assert len(grids["ETH"]) > 1000 and len(reads["ETH"]) > 10
    t_s, cards = reads["ETH"][-1]
    assert set(cards) == {"1m", "10m"} and set(cards["1m"].values()) <= {-1, 0, 1}

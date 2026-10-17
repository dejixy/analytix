"""The low-frequency lab: trade simulation, walk-forward, sizing (engine/lowfreq.py), the history cache
(ingestion/history.py) and the lab end to end (scripts/lowFreqLab.py)."""
import json
import sys

import numpy as np
import pytest

from engine.lowfreq import HOUR_MS, concat, months_of, simulate, sizing, summary, walk_forward
from ingestion.history import Bars, Fetcher, load, update

T0 = 1_700_000_000_000 - 1_700_000_000_000 % HOUR_MS


def bars(c, o=None, h=None, l=None, funding=None, coin="BTC"):
    c = np.asarray(c, dtype=float)
    o = c.copy() if o is None else np.asarray(o, dtype=float)
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    l = np.minimum(o, c) if l is None else np.asarray(l, dtype=float)
    n = len(c)
    return Bars(coin, "hl", T0 + np.arange(n) * HOUR_MS, o, h, l, c, np.ones(n),
                np.zeros(n) if funding is None else np.asarray(funding, dtype=float))


def test_a_trade_enters_at_the_next_open_and_leaves_at_the_max_hold():
    n = 60
    c = 100 + np.arange(n) * 0.1
    b = bars(c, o=np.concatenate([[100], c[:-1]]))
    sig = np.zeros(n, dtype=int)
    sig[30] = 1
    tr = simulate(b, sig, stop_atr=50, max_hold=5, cost_bps=10, atr_n=24)
    assert len(tr) == 1 and tr.reason == ["time"]
    assert tr.entry_t[0] == b.t[31] and tr.hold_h[0] == 5
    entry, out = b.o[31], b.c[35]
    assert tr.gross[0] == pytest.approx(out / entry - 1)
    assert tr.net[0] == pytest.approx(out / entry - 1 - 10e-4)


def test_stops_and_gaps_and_funding():
    n = 40
    c = np.full(n, 100.0)
    o, h, l = c.copy(), c + 0.5, c - 0.5
    l[32] = 90.0                                              # a long's stop is hit inside bar 32
    o[36], h[36], l[36], c[36] = 80.0, 80.5, 79.5, 80.0       # a gap far through a short... (short wins here)
    fund = np.full(n, 1e-4)
    b = bars(c, o, h, l, fund)
    sig = np.zeros(n, dtype=int)
    sig[30] = 1
    tr = simulate(b, sig, stop_atr=2, max_hold=10, cost_bps=0, atr_n=24)
    stop_dist = tr.stop[0] * 100
    assert tr.reason[0] == "stop" and tr.gross[0] == pytest.approx(-stop_dist / 100)
    assert tr.net[0] == pytest.approx(tr.gross[0] - 2 * 1e-4)  # two hours of funding paid by a long
    sig = np.zeros(n, dtype=int)
    sig[34] = 1
    o2 = o.copy()
    tr2 = simulate(bars(c, o2, h, l, fund), sig, stop_atr=2, max_hold=10, cost_bps=0, atr_n=24)
    assert tr2.reason[0] == "stop" and tr2.gross[0] == pytest.approx(80 / 100 - 1)   # out at the gap's open
    sig = np.zeros(n, dtype=int)
    sig[34] = -1
    tr3 = simulate(bars(c, o2, h, l, fund), sig, stop_atr=2, max_hold=3, cost_bps=0, atr_n=24)
    assert tr3.net[0] > tr3.gross[0]                            # a short receives funding


def test_one_position_at_a_time_and_flips():
    n = 60
    c = np.full(n, 100.0)
    b = bars(c, h=c + 0.5, l=c - 0.5)
    sig = np.zeros(n, dtype=int)
    sig[30:40] = 1
    tr = simulate(b, sig, stop_atr=100, max_hold=5, cost_bps=0, atr_n=24)
    assert list(tr.entry_t) == [b.t[31], b.t[37]]               # out at bar 35, next signal 36, in at 37
    sig = np.zeros(n, dtype=int)
    sig[30], sig[33] = 1, -1
    tr = simulate(b, sig, stop_atr=100, max_hold=20, cost_bps=0, atr_n=24, exit_on_flip=True)
    assert tr.reason[0] == "flip" and tr.side.tolist() == [1, -1]


def test_walk_forward_only_uses_trades_that_finished_before_the_month():
    def mk(net, coin="BTC"):
        k = len(net)
        t = T0 + np.arange(k) * 24 * HOUR_MS
        from engine.lowfreq import Trades
        return Trades(coin, t, t + HOUR_MS, np.ones(k, dtype=np.int64), np.array(net), np.array(net),
                      np.full(k, 0.01), np.ones(k), ["time"] * k)
    good_then_bad = mk([0.01, 0.012] * 20 + [-0.01] * 40)
    steady = mk([0.002, 0.001] * 40)
    months = [(T0 + 40 * 24 * HOUR_MS, T0 + 80 * 24 * HOUR_MS)]
    honest, picks = walk_forward({"a": good_then_bad, "b": steady}, months, min_n=30)
    assert picks[0][1] == "a"                                  # it looked best on what was known
    assert len(honest) == 40 and honest.net.mean() == pytest.approx(-0.01)   # and then it failed: that's honest
    s = summary(honest, 40 * 24 * HOUR_MS)
    assert s["n"] == 40 and s["net_bps"] == pytest.approx(-100)
    assert len(months_of(T0, T0 + 100 * 86_400_000, 30)) == 3


def test_sizing_caps_leverage_and_counts_halvings():
    from engine.lowfreq import Trades
    k = 40
    t = T0 + np.arange(k) * 24 * HOUR_MS
    net = np.where(np.arange(k) % 2 == 0, 0.004, -0.002)
    tr = Trades("BTC", t, t + HOUR_MS, np.ones(k, dtype=np.int64), net, net, np.full(k, 0.001), np.ones(k), ["time"] * k)
    s = sizing(tr, 0.05, tr.coins, weeks=1)
    assert s["lev"] == 40 and s["capped"] == 1.0              # 5% / 0.1% = 50x, capped at BTC's 40x
    assert s["median"] > 0 and 0 <= s["hit25"] <= 1 and s["half"] == 0


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(json)
        if json["type"] == "candleSnapshot":
            r = json["req"]
            ts = range(r["startTime"] - r["startTime"] % HOUR_MS, min(r["endTime"], T0 + 50 * HOUR_MS), HOUR_MS)
            data = [{"t": t, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"} for t in ts if t >= T0]
        else:
            first = -(-json["startTime"] // HOUR_MS) * HOUR_MS        # real funding times are on the hour
            data = [{"time": t, "fundingRate": "0.0001"} for t in range(max(first, T0 + HOUR_MS),
                                                                         min(json["endTime"], T0 + 50 * HOUR_MS), HOUR_MS)][:20]
        return Resp(data)

    def get(self, url, params, timeout):
        raise RuntimeError("451 restricted location")


class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_history_fetches_pages_caches_and_tops_up(tmp_path, monkeypatch):
    import time as _t
    import ingestion.history as hist
    monkeypatch.setattr(hist.time, "sleep", lambda s: None)
    monkeypatch.setattr(hist.time, "time", lambda: (T0 + 60 * HOUR_MS) / 1000)
    said = []
    fc = FakeClient()
    update(["BTC"], 60 * HOUR_MS / 86_400_000, 30, tmp_path, client=fc, say=said.append)
    assert any("bn BTC 1h: couldn't fetch" in m for m in said)  # Binance blocked: noted, not fatal
    b = load("BTC", "hl", tmp_path)
    assert len(b) == 50 and b.c[0] == 1.5
    assert b.funding[0] == pytest.approx(1e-4) and b.funding[-1] == 0   # funding at T covers the hour before T
    n_calls = len(fc.calls)
    update(["BTC"], 60 * HOUR_MS / 86_400_000, 0, tmp_path, client=fc, say=said.append)
    assert "(+0)" in said[-1] or "(+0)" in said[-2]             # nothing new: only the gap is asked for
    assert all(c.get("req", c).get("startTime", 0) > T0 for c in fc.calls[n_calls:])


def test_the_lab_runs_end_to_end(tmp_path, monkeypatch, capsys):
    rng = np.random.default_rng(3)
    n = 24 * 120
    for coin, src in (("BTC", "hl"), ("ETH", "hl"), ("BTC", "bn")):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
        o = np.concatenate([[100], c[:-1]])
        h = np.maximum(o, c) * 1.002
        l = np.minimum(o, c) * 0.998
        rows = "\n".join(f"{T0 + k * HOUR_MS},{o[k]},{h[k]},{l[k]},{c[k]},1" for k in range(n))
        (tmp_path / f"{src}_{coin}_1h.csv").write_text("t_ms,open,high,low,close,volume\n" + rows + "\n")
        fr = "\n".join(f"{T0 + (k + 1) * HOUR_MS},{rng.normal(1e-5, 2e-5)}" for k in range(n))
        (tmp_path / f"{src}_{coin}_funding.csv").write_text("t_ms,rate\n" + fr + "\n")
    from scripts import lowFreqLab
    monkeypatch.setattr(lowFreqLab, "FAMILIES", {k: v[:3] for k, v in lowFreqLab.FAMILIES.items()})
    monkeypatch.setattr(sys, "argv", ["lowFreqLab", "--coins", "BTC,ETH", "--folder", str(tmp_path)])
    lowFreqLab.main()
    out = capsys.readouterr().out
    assert "HYPERLIQUID" in out and "1. HONEST RESULT" in out and "3. SIZING" in out and "BINANCE" in out
    for fam in ("breakout", "momentum", "pullback", "reversion", "funding", "squeeze"):
        assert fam in out

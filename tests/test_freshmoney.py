"""Fresh money (engine/freshmoney.py, scripts/freshMoney.py): first-seen wallets, buckets, the walk-forward spikes."""
import time

import numpy as np

from engine import freshmoney as F

H, D, S = F.HOUR_MS, F.DAY_MS, F.STEP_MS
T0 = 1_790_000_000_000 // D * D                               # a midnight UTC


def test_first_seen_is_the_earliest_trade_and_never_after_it():
    seen = F.Seen()
    g = seen.gids(np.array(["0xa", "0xb"]))
    seen.update(g[[1, 0, 1]], np.array([T0 + 50, T0 + 10, T0 + 5]))
    assert seen.first.tolist() == [T0 + 10, T0 + 5]
    g2 = seen.gids(np.array(["0xc", "0xa"]))                  # a later day's table: 0xa keeps its id
    assert g2.tolist() == [2, 0] and seen.first[2] == F.NEVER
    seen.update(g2, np.array([T0 + D, T0 + D]))
    assert seen.first[0] == T0 + 10
    assert seen.age(np.array([0, 2, -1]), np.array([T0 + D, T0 + D, T0])).tolist() == [D - 10, 0, F.NEVER]


def test_buckets_split_fresh_from_known_and_merge_split_buckets():
    c = F.Coin()
    t = np.array([T0, T0 + 1000, T0 + S + 5, T0 + 3 * S])
    age = np.array([5 * D, 10 * H, 30 * 60_000, F.NEVER])
    c.add(t, np.array([100.0, 101, 102, 103]), np.array([1000.0, 500, 200, 50]), np.array([1, -1, 1, 1]),
          np.array([0, 1, 2, 3]), age)
    c.add(np.array([T0 + 3 * S + 10]), np.array([104.0]), np.array([25.0]), np.array([-1]), np.array([4]),
          np.array([2 * H]))                                   # the same bucket, from another hour's file
    g = c.grid()
    assert g["total"].tolist() == [1500, 200, 0, 75]
    assert g["fresh"].tolist() == [500, 200, 0, 25] and g["fnet"].tolist() == [-500, 200, 0, -25]
    assert g["new"].tolist() == [0, 200, 0, 0] and g["net"].tolist() == [500, 200, 0, 25]
    assert g["px"].tolist() == [101, 102, 102, 104]           # an empty bucket carries the price on
    ft, fw, fu = c.fresh_trades()
    assert fw.tolist() == [1, 2, 4] and fu.tolist() == [-500, 200, -25]
    assert F.concentration((ft, fw, fu), T0, T0 + 4 * S) == 500 / 725


def test_thresholds_only_look_at_earlier_days():
    per = D // S
    k = np.arange(T0 // S, T0 // S + 6 * per)
    v = np.repeat([1.0, 2, 3, 4, 100, 5], per)                # day 5's huge values can't move day 5's own threshold
    thr = F.thresholds(v, k, k[0], 1.0, lookback=7, min_days=3)
    days = [thr[i * per] for i in range(6)]
    assert np.isnan(days[:3]).all() and days[3] == 3 and days[4] == 4 and days[5] == 100


def _grid(days=14, seed=3):
    rng = np.random.default_rng(seed)
    n = days * (D // S)
    k = np.arange(T0 // S, T0 // S + n)
    total = rng.uniform(80_000, 120_000, n)
    fresh = total * rng.uniform(0.02, 0.06, n)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 4e-4, n)))
    return {"k": k, "total": total, "fresh": fresh, "fnet": fresh * rng.choice([-1, 1], n), "net": total * 0.01,
            "new": fresh * 0.1, "px": px}


def test_a_fresh_spike_is_found_once_and_scored():
    g = _grid()
    burn = g["k"][0] + F.BURN_DAYS * (D // S)
    i0 = 12 * (D // S) + 100                                  # day 12: an hour where fresh money floods in, buying
    g["fresh"][i0 - 11:i0 + 1] = g["total"][i0 - 11:i0 + 1] * 0.6
    g["fnet"][i0 - 11:i0 + 1] = g["fresh"][i0 - 11:i0 + 1]
    g["px"][i0 + 1:] *= 1.02                                  # and price jumps 200 bps right after
    idx, test = F.spikes(g, burn, q=1.0)                      # above everything before: only the planted burst
    assert len(idx) == 1 and i0 - 11 <= idx[0] <= i0          # one spike for the burst (the cooldown)
    assert test[i0] and not test[: 10 * (D // S)].any()       # burn-in plus 3 days of lookback before any test
    direction = np.sign(F.roll(g["fnet"])[idx])
    sc = F.score(g, idx[:1], direction[:1], test)
    assert sc["4h"]["ratio"] > 5 and sc["4h"]["signed"] > 150


def test_the_volume_control_needs_busy_hours_without_fresh_money():
    g = _grid()
    burn = g["k"][0] + F.BURN_DAYS * (D // S)
    i0 = 13 * (D // S) + 40
    g["total"][i0 - 11:i0 + 1] *= 5                           # busy, and the fresh share falls with it
    burst = lambda idx: [i for i in idx if i0 - 11 <= i <= i0]
    assert len(burst(F.spikes(g, burn, q=1.0, kind="volume")[0])) == 1
    g["fresh"][i0 - 11:i0 + 1] *= 25                          # the same busy hour, but the new money came with it
    assert not burst(F.spikes(g, burn, q=1.0, kind="volume")[0])


def _store(tmp_path, days=14, seed=5):
    """A small fast store: regulars trade BTC every day; on day 12, wallets never seen before buy hard for an hour
    and the price jumps 300 bps after. XYZ is another coin whose wallets only count as known."""
    rng = np.random.default_rng(seed)
    regulars = [f"0x{i:040x}" for i in range(80)]
    px = 60_000.0
    for d in range(days):
        day = time.strftime("%Y%m%d", time.gmtime((T0 + d * D) / 1000))
        t = np.sort(rng.integers(T0 + d * D, T0 + (d + 1) * D, 3000))
        steps = rng.normal(0, 2e-4, len(t))
        names = list(regulars)
        buyer = rng.integers(0, 80, len(t))
        seller = rng.integers(0, 80, len(t))
        sz = rng.uniform(0.01, 0.2, len(t))
        if d == 12:
            hit = (t >= T0 + d * D + 10 * H) & (t < T0 + d * D + 11 * H)
            fresh = [f"0xf{d:02d}{i:037x}" for i in range(30)]
            names += fresh
            buyer[hit] = 80 + rng.integers(0, 30, hit.sum())
            sz[hit] *= 20
            steps[np.searchsorted(t, T0 + d * D + 11 * H)] += 0.03
        side = np.where(rng.random(len(t)) < 0.5, 1, -1)
        if d == 12:
            side[hit] = 1
        p = px * np.exp(np.cumsum(steps))
        px = float(p[-1])
        for coin, rows in (("BTC", slice(None)), ("XYZ", slice(0, 50))):
            (tmp_path / coin).mkdir(parents=True, exist_ok=True)
            np.savez_compressed(tmp_path / coin / f"{day}.npz", trade_t=t[rows], trade_px=p[rows], trade_sz=sz[rows],
                                trade_side=side[rows].astype("i1"), trade_engine=np.zeros(len(t[rows]), bool),
                                trade_buyer=buyer[rows], trade_seller=seller[rows], wallets=np.array(names, dtype="<U42"))
    return tmp_path


def test_the_study_finds_a_planted_burst_end_to_end(tmp_path, capsys, monkeypatch):
    import json
    import sys
    from scripts import freshMoney
    store = _store(tmp_path / "store")
    out = tmp_path / "fresh.json"
    monkeypatch.setattr(sys, "argv", ["freshMoney", "--coins", "BTC", "--store", str(store), "--out", str(out)])
    freshMoney.main()
    text = capsys.readouterr().out
    assert "Fresh money · 14 days" in text and "1. Fresh-money spikes" in text and "Verdict" in text
    res = json.loads(out.read_text())
    sp = res["coins"]["BTC"]["spikes"]
    burst_end = T0 + 12 * D + 11 * H
    assert any(abs(s["t_ms"] - burst_end) <= H and s["dir"] == 1 and s["share"] > 0.5 for s in sp)
    assert res["coins"]["BTC"]["top_wallet_share"] < 0.5         # 30 wallets, not one
    assert all(v < 0.05 for k, v in res["daily_share"]["BTC"].items() if k != time.strftime("%Y-%m-%d", time.gmtime((T0 + 12 * D) / 1000)))

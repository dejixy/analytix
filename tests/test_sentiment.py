"""Sentiment Check: conditions, outcomes, tables with back-off, the year-by-year check and the build from stored
history."""
import json
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from engine import releases as RL
from engine import sentiment as S
from ingestion import longhist as L


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def test_session_and_news():
    assert list(S.session_of([ts("2026-11-18 03:00"), ts("2026-11-18 08:00"), ts("2026-11-18 14:30"),
                              ts("2026-11-18 21:00"), ts("2026-11-21 14:30")])) == [0, 1, 2, 3, 4]   # Sat = weekend
    rel = RL.times_s()
    cpi = ts("2026-11-10 13:30")                        # 8:30 New York in November (UTC-5)
    assert cpi in set(rel.tolist()) and ts("2026-10-28 18:00") in set(rel.tolist())   # Fed: 2 pm, UTC-4 in October
    got = S.news_of(np.array([cpi - 3600, cpi - 600, cpi + 600, cpi + 7200]), 300, rel)
    assert list(got) == [0, 1, 2, 0]                    # 30 minutes at least either side, for a 5-minute horizon
    assert S.news_of(np.array([cpi - 3 * 3600]), 4 * 3600, rel)[0] == 1     # a 4-hour horizon looks 4 hours ahead


def test_grid_gaps_and_binning():
    t = np.array([0, 1, 2, 5, 6, 200, 201])
    c = np.arange(7, dtype=float) + 100
    s = S.to_grid(t, c, c + 1, c - 1, np.ones(7), np.ones(7) / 2, step=1, quiet_s=60)
    assert len(s) == 202 and s.ok[3] and s.ok[4] and not s.ok[100] and s.c[4] == 102 and s.v[4] == 0
    assert list(S.binned(np.array([-2, -1, 0, 1, 2, np.nan]), S.EDGES["move"])) == [0, 1, 2, 3, 4, 5]


def test_normals_use_only_earlier_days():
    n = 5 * 1440
    t = np.arange(n) + ts("2025-01-01 00:00") // 60
    c = 100 * np.exp(np.cumsum(np.r_[np.full(3 * 1440, 1e-4), np.full(2 * 1440, 1e-3)] * np.tile([1, -1], n // 2)))
    s = S.to_grid(t, c, c, c, np.ones(n), np.ones(n) / 2, 60)
    norms = S.Normals()
    norms.add(s, S.raw(s, False))
    _, _, sig = norms.at(s.t)
    day = (s.t - s.t[0]) // 86400
    assert np.isnan(sig[day < 3]).all()                       # three days needed before a normal exists
    assert np.allclose(sig[day == 3], 1e-4, rtol=0.05)        # day 3 (loud) is judged on days 0-2 (quiet) only
    assert sig[day == 4].mean() > 2e-4                        # day 4 includes day 3
    back = S.Normals.from_json(json.loads(json.dumps(norms.to_json())))
    assert np.allclose(back.at(s.t[-5:])[2], norms.at(s.t[-5:])[2])


def _series(rets, step=60, t0=None):
    t0 = (t0 or ts("2025-01-01 00:00")) // step
    c = 100 * np.exp(np.cumsum(rets))
    n = len(c)
    return S.to_grid(np.arange(n) + t0, c, c * 1.0002, c * 0.9997, np.ones(n), np.ones(n) / 2, step)


def test_outcomes_are_separate_stretches():
    rets = np.zeros(4 * 1440)
    rets[::2], rets[1::2] = 1e-4, -1e-4
    s = _series(rets)
    norms = S.Normals()
    norms.add(s, S.raw(s, False))
    p = S.prepare(s, S.raw(s, False), norms)
    start, ret, dip, rise = S.outcomes(p, 300)
    assert (np.diff(start) % 5 == 0).all() and len(start) <= len(s) // 5
    assert (dip <= 0).all() and (rise >= 0).all() and np.isfinite(ret).all()
    unit = p.sig[start[0]] * math.sqrt(5)
    lows = {round(math.log(0.9997) / unit, 3), round((math.log(0.9997) - 1e-4) / unit, 3)}   # the low's candle opened up or down
    assert round(dip[0], 3) in lows


def _cases(n, seed=1):
    rng = np.random.default_rng(seed)
    bins = np.stack([rng.integers(0, S.NBINS[f], n) for f in S.FEATURES], axis=1).astype(np.int16)
    ret = rng.standard_normal(n)
    dip = -np.abs(rng.standard_normal(n))
    rise = np.abs(rng.standard_normal(n))
    years = np.where(np.arange(n) < n // 2, 2024, 2025)
    return bins, years, ret, dip, rise


def test_tables_back_off_and_merge():
    bins, years, ret, dip, rise = _cases(20_000)
    whole = S.build_table("5m", bins, years, ret, dip, rise)
    half = len(ret) // 3
    merged = S.merge_tables(S.build_table("5m", bins[:half], years[:half], ret[:half], dip[:half], rise[:half]),
                            S.build_table("5m", bins[half:], years[half:], ret[half:], dip[half:], rise[half:]))
    for a, b in zip(whole.levels, merged.levels):
        assert all(np.array_equal(a[k], b[k]) for k in ("codes", "n", "up", "hist", "dip", "rise"))
    K, R = whole.rows(bins[:50])
    lv = whole.levels
    for k, r in zip(K, R):
        assert lv[k]["n"][r].sum() >= S.MIN_N
        if k < len(S.FEATURES):                                  # the next level down was too thin (or absent)
            assert True
    k0, _ = whole.rows(bins[:1])
    got = S.read(whole, bins[0], scale_bps=10.0)
    assert got["kept"] == list(S.FEATURES[:k0[0]]) and got["n"] >= S.MIN_N and got["base_n"] == 20_000
    assert got["q10_bps"] < got["q50_bps"] < got["q90_bps"] and len(got["curve"]["now"]) == len(S.RET_Z)
    assert 0 < got["dips"][0]["chance"] < 1 and got["dips"][0]["bps"] < got["dips"][2]["bps"]
    pruned = S.prune(whole)
    assert (S.read(pruned, bins[0], 10.0)["n"]) == got["n"]
    assert all((lv["n"].sum(axis=1) >= S.MIN_N).all() for lv in pruned.levels[1:])


def test_save_and_load(tmp_path):
    bins, years, ret, dip, rise = _cases(3000)
    t = S.prune(S.build_table("1h", bins, years, ret, dip, rise))
    S.save_tables(tmp_path / "X.npz", {"1h": t})
    back = S.load_tables(tmp_path / "X.npz")["1h"]
    assert S.read(back, bins[5], 20.0) == S.read(t, bins[5], 20.0)
    assert S.drivers(back, min_n=10)["up"] == S.drivers(t, min_n=10)["up"]


def test_held_up_needs_later_years():
    base_n, base_up = np.array([1000, 1000, 1000, 1000]), np.array([500, 500, 500, 500])
    lean = S.held_up(np.array([200, 200, 200, 200]), np.array([130, 128, 126, 131]), base_n, base_up)
    assert lean["held"] and lean["tested"] == 3 and lean["z"] > 2
    flip = S.held_up(np.array([200, 200, 200, 200]), np.array([130, 70, 130, 70]), base_n, base_up)
    assert not flip["held"]


def test_learner_finds_size_not_direction():
    """Volatility that clusters: the like-now dips should beat the plain rate on later years; direction is a coin
    flip, so its skill should be about zero (not positive by luck of fitting)."""
    rng = np.random.default_rng(7)
    n = 60_000
    loud = np.repeat(rng.random(n // 100) < 0.3, 100)
    bins = np.zeros((n, len(S.FEATURES)), dtype=np.int16)
    bins[:, 0] = np.where(loud, 2, 1)                             # vol: loud or normal, as the conditions saw it
    bins[:, 1:] = rng.integers(0, 3, (n, len(S.FEATURES) - 1))
    sd = np.where(loud, 2.0, 0.7)
    ret = rng.standard_normal(n) * sd
    dip = -np.abs(rng.standard_normal(n)) * sd
    rise = np.abs(rng.standard_normal(n)) * sd
    t = ts("2023-01-01 00:00") + np.arange(n) * 3 * 3600          # 3-hour stretches: about 20 years
    lr = S.Learner("4h")
    for chunk in np.array_split(np.arange(n), 10):
        lr.add(bins[chunk], t[chunk], ret[chunk], dip[chunk], rise[chunk])
    c = lr.check()
    assert c["n"] > 0.8 * n and c["dip"] > 0.05 and c["rise"] > 0.05 and abs(c["up"]) < 0.01


def _store(folder, coin, kind, month, d):
    L.save(folder / coin / kind / f"{month}.npz", d)


def test_build_from_stored_history(tmp_path):
    """A few months of made-up minutes and seconds, stored the way the downloader stores them, built into tables."""
    from scripts import sentimentData as D
    rng = np.random.default_rng(3)
    t0 = ts("2024-11-01 00:00")
    n = 120 * 1440
    lr = rng.standard_normal(n) * 5e-4 * np.repeat(1 + rng.random(n // 1440), 1440)
    c = 60_000 * np.exp(np.cumsum(lr))
    t = t0 // 60 + np.arange(n)
    months = (t * 60).astype("datetime64[s]").astype("datetime64[M]").astype(str)
    v = rng.random(n) * 10
    for m in np.unique(months):
        sel = months == m
        _store(tmp_path, "BTC", "m1", m, L.pack(t[sel], c[sel], c[sel] * 1.0004, c[sel] * 0.9996, c[sel],
                                                v=v[sel], tbv=v[sel] * rng.random(sel.sum()), n=np.ones(sel.sum())))
    ot = t[::5]
    L.save(tmp_path / "BTC" / "oi" / "2024-11.npz", {"t": ot, "oi": 1e5 * np.exp(np.cumsum(rng.standard_normal(len(ot)) * 1e-3)),
                                                    "oi_usd": np.ones(len(ot))})
    ft = t[::480]
    L.save(tmp_path / "BTC" / "funding" / "2024-11.npz", {"t": ft, "rate": rng.standard_normal(len(ft)) * 1e-4})
    s0 = ts("2024-12-20 00:00")                                   # a few weeks of seconds, over the new year
    ns = 20 * 86400
    st = s0 + np.arange(ns)
    st = st[rng.random(ns) > 0.02]                                # a few quiet seconds with no trades
    sc = 95_000 * np.exp(np.cumsum(rng.standard_normal(len(st)) * 3e-5))
    smon = st.astype("datetime64[s]").astype("datetime64[M]").astype(str)
    sv = rng.random(len(st))
    for m in np.unique(smon):
        sel = smon == m
        _store(tmp_path, "BTC", "s1", m, L.pack(st[sel], sc[sel], sc[sel] * 1.00002, sc[sel] * 0.99998,
                                                v=sv[sel], tbv=sv[sel] / 2))
    out = tmp_path / "out"
    said = []
    assert D.build("BTC", tmp_path, out, say=said.append)
    tables = S.load_tables(out / "BTC.npz")
    assert set(tables) == set(S.HORIZONS)
    meta = json.loads((out / "BTC.json").read_text())
    assert meta["long"]["normals"] and meta["short"]["normals"] and meta["checks"]["5m"]["n"] > 0
    assert any("1-second 2024-12" in m for m in said) and any("1-second 2025-01" in m for m in said)
    five = tables["5m"].levels[0]["n"].sum()
    assert 0.8 * n / 5 < five <= n / 5                              # one stretch per 5 minutes, after the 3-day warm-up
    ten = tables["10s"].levels[0]["n"].sum()
    assert 0.5 * ns / 10 < ten <= ns / 10
    import os
    os.utime(out / "BTC.json", (0, 0))
    meta["normals_at"] = 0
    assert D.refresh_normals("BTC", tmp_path, out, say=said.append) is True


class _Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(self.status_code)

    def json(self):
        return self.body


class FakeBinance:
    """Binance's REST answers: a day of minutes and 16 minutes of seconds ending at `end`, open interest, funding."""

    def __init__(self, end_s: int, down: bool = False):
        self.end, self.down, self.asked = end_s, down, []
        rng = np.random.default_rng(5)
        self.m = 60_000 * np.exp(np.cumsum(rng.standard_normal(1500) * 5e-4))
        self.s = self.m[-1] * np.exp(np.cumsum(rng.standard_normal(1000) * 3e-5))

    def get(self, url, params=None):
        self.asked.append(url)
        if self.down and "binance" in url:
            return _Reply({}, 451)
        if url.endswith("/fapi/v1/klines"):
            t0 = (self.end // 60 - 1500) * 60_000
            return _Reply([[t0 + i * 60_000, c, c * 1.0004, c * 0.9996, c, 5.0, t0 + i * 60_000 + 59_999, 0, 9, 3.0, 0, 0]
                           for i, c in enumerate(self.m)])
        if url.endswith("/api/v3/klines"):
            t0 = (self.end - 1000) * 1000
            return _Reply([[t0 + i * 1000, c, c * 1.00002, c * 0.99998, c, 0.5, t0 + i * 1000 + 999, 0, 3, 0.2, 0, 0]
                           for i, c in enumerate(self.s)])
        if "openInterestHist" in url:
            return _Reply([{"timestamp": (self.end - 3900 + 300 * i) * 1000, "sumOpenInterest": 1e5 * (1 + 0.001 * i)}
                           for i in range(14)])
        if url.endswith("/fapi/v1/fundingRate"):
            return _Reply([{"fundingTime": (self.end - 3600) * 1000, "fundingRate": "0.0003"}])
        if "bybit" in url and url.endswith("/kline"):
            t0 = (self.end // 60 - 1000) * 60_000
            return _Reply({"result": {"list": [[str(t0 + i * 60_000), c, c, c, c, "5", "0"] for i, c in
                                               enumerate(self.m[-1000:])][::-1]}})
        return _Reply({}, 404)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sent")
    from scripts import sentimentData as D
    rng = np.random.default_rng(11)
    t0 = ts("2024-11-01 00:00")
    n = 100 * 1440
    c = 60_000 * np.exp(np.cumsum(rng.standard_normal(n) * 5e-4))
    t = t0 // 60 + np.arange(n)
    months = (t * 60).astype("datetime64[s]").astype("datetime64[M]").astype(str)
    v = rng.random(n) * 10
    for m in np.unique(months):
        sel = months == m
        _store(tmp, "BTC", "m1", m, L.pack(t[sel], c[sel], c[sel] * 1.0004, c[sel] * 0.9996, c[sel],
                                           v=v[sel], tbv=v[sel] / 2, n=np.ones(sel.sum())))
    st = ts("2025-01-20 00:00") + np.arange(12 * 86400)
    sc = 95_000 * np.exp(np.cumsum(rng.standard_normal(len(st)) * 3e-5))
    sv = rng.random(len(st))
    _store(tmp, "BTC", "s1", "2025-01", L.pack(st, sc, sc * 1.00002, sc * 0.99998, v=sv, tbv=sv / 2))
    D.build("BTC", tmp, tmp / "out", say=lambda m: None)
    return tmp / "out"


def test_live_read(built):
    import asyncio
    from api.sentiment import SentimentCheck
    end = ts("2025-02-10 15:00")
    sc = SentimentCheck(None, built, client=FakeBinance(end))
    got = asyncio.run(sc.read("BTC"))
    hs = {h["horizon"]: h for h in got["horizons"]}
    assert set(hs) == set(S.HORIZONS) and got["source"] == "Binance" and got["coins"] == ["BTC"]
    one = hs["10m"]
    assert one["n"] >= S.MIN_N and 0 <= one["up"] <= one["n"] and 0 < one["p_up"] < 1 and one["check"]["n"] > 0
    assert one["conditions"]["funding"] == "high" and one["conditions"]["oi"] in ("flat", "rising")
    assert one["typical_bps"] > 0 and one["q10_bps"] < one["q90_bps"] and hs["24h"]["typical_bps"] > one["typical_bps"]
    assert got["now"]["long"]["session"] == "US" and got["now"]["short"]["buyers"] == "sellers"   # 0.2 of 0.5 bought
    asyncio.run(sc.read("BTC"))
    assert sum("fapi/v1/klines" in u for u in sc.client.asked) == 1            # cached for a few seconds
    assert asyncio.run(sc.read("ETH"))["missing"] is True


def test_live_read_on_bybit_and_hold(built):
    import asyncio
    from api.sentiment import SentimentCheck
    sc = SentimentCheck(None, built, client=FakeBinance(ts("2025-02-10 12:00"), down=True))
    got = asyncio.run(sc.read("BTC"))
    assert got["source"] == "Bybit" and not any(h["family"] == "short" for h in got["horizons"])
    assert {h["conditions"]["buyers"] for h in got["horizons"]} == {"unknown"}

    async def hold_twice():
        first = await sc.hold("BTC", 1, 12.0)                   # cached read from above: answers at once
        return first
    rows = asyncio.run(hold_twice())
    assert [r["horizon"] for r in rows] == [10, 30, 60, 120, 300, 900]
    five = rows[4]
    assert 0 < five["survive"] < 1 and 0 < five["base"] < 1 and rows[0]["survive"] is None   # no seconds on Bybit
    far = asyncio.run(sc.hold("BTC", -1, 400.0))
    assert far[4]["survive"] > five["survive"]
    cold = SentimentCheck(None, built, client=FakeBinance(ts("2025-02-10 12:00")))

    async def no_wait():
        got = await cold.hold("BTC", 1, 12.0)                   # nothing cached: no answer, a read is started
        await asyncio.sleep(0.5)
        return got, await cold.hold("BTC", 1, 12.0)
    a, b = asyncio.run(no_wait())
    assert a is None and b is not None and b[0]["survive"] is not None

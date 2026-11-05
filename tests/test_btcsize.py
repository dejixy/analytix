"""BTC estimate v2 (engine/btcsize.py): realised vol, the size forecast, the event studies, and the Strategy and
Deribit readers it relies on."""
import asyncio
import math
import random
from datetime import date, timedelta

import httpx
import numpy as np

from engine import btcmodel as M
from engine import btcsize as S
from ingestion import flows as F

H = M.HOUR


async def _nosleep(_s):
    return None


def test_realised_vol_counts_whole_hours_scales_gaps_and_refuses_thin_windows():
    t0 = M.ms_of("2025-01-06")
    btc = [(t0 + k * H, 100 * math.exp(0.01 * (k % 2))) for k in range(49)]    # ±1% every hour
    hrs = S.Hours(btc)
    assert abs(hrs.rv(t0, t0 + 24 * H) - math.sqrt(24) * 1.0) < 1e-6
    gappy = [x for i, x in enumerate(btc) if i not in (5, 6)]                   # two prices missing: three moves gone
    v = S.Hours(gappy).rv(t0, t0 + 24 * H)
    assert v is not None and abs(v - math.sqrt(24)) < 0.4
    assert S.Hours(btc[:10] + btc[30:]).rv(t0, t0 + 24 * H) is None             # under 80% of the hours
    assert hrs.rv(t0, t0 + 60 * H) is None and hrs.rv(t0 - H, t0 + 5 * H) is None   # off the end of the data
    assert S.weekend_share(M.ms_of("2025-01-11", 14)) == 1.0                    # Saturday 14:00 → Sunday 14:00
    assert S.weekend_share(M.ms_of("2025-01-10", 14)) == 14 / 24                # Friday 14:00: 10 h of Friday


def _world(n_days=520, flow_effect=0.35, seed=5):
    """US flow days and hourly BTC whose daily volatility follows its own slow regime (so the past forecasts it)
    and, when flow_effect > 0, rises after unusually large flow days in either direction."""
    rnd = random.Random(seed)
    d0 = date(2024, 2, 5)
    days = [d0 + timedelta(days=i) for i in range(int(n_days * 1.45)) if (d0 + timedelta(days=i)).weekday() < 5][:n_days]
    etf = [{"date": d.isoformat(), "total": rnd.gauss(0, 300), "funds": {}} for d in days]
    boost = {M.ms_of(x["date"], M.ENTRY_H) + M.DAY: flow_effect * abs(x["total"]) / 300 for x in etf}
    t = M.ms_of(days[0].isoformat()) - 40 * M.DAY
    end = M.ms_of(days[-1].isoformat()) + 4 * M.DAY
    px, lv, btc = 60_000.0, math.log(2.0), []
    starts = sorted(boost)
    j = 0
    while t <= end:
        if t % M.DAY == 0:
            lv = 0.9 * lv + 0.1 * math.log(2.0) + rnd.gauss(0, 0.12)            # the slow regime, daily
        while j < len(starts) and starts[j] + 24 * H <= t:
            j += 1
        b = boost[starts[j]] if j < len(starts) and starts[j] < t <= starts[j] + 24 * H else 0.0
        sd = math.exp(lv) * (1 + b) / math.sqrt(24)
        px *= math.exp(rnd.gauss(0, sd) / 100)
        btc.append((t, px))
        t += H
    mstr = [(M.ms_of(d.isoformat(), 21), 300.0 * (1 + 0.01 * rnd.gauss(0, 1))) for d in days]
    dat = [{"filed": d.isoformat(), "bought": 100.0 + 50 * rnd.random()} for d in days if d.weekday() == 0]
    return etf, dat, btc, mstr


def _size(world):
    etf, dat, btc, mstr = world
    rows = M.build_rows(etf, dat, btc, mstr)
    srows = S.size_rows(rows, btc)
    every = S.OWN + S.FLOWS
    done = S.complete(srows, every)
    naive = S.walk(done, (), 120)
    own = S.walk(done, S.OWN, 120)
    both = S.walk(done, every, 120)
    return srows, done, naive, own, both


def test_the_size_forecast_finds_a_planted_flow_effect_and_not_a_missing_one():
    srows, done, naive, own, both = _size(_world())
    assert len(done) > 300 and all(r["x"]["rv30"] is not None for r in done)
    s_own = S.size_scores(own, naive)
    assert s_own["corr"] > 0.3 and s_own["r2"] > 0.05                      # the regime: its own past forecasts it
    s = S.size_scores(both, naive, own)
    assert s["t_gain"] > 2 and s["gain"] > 0                               # and the planted flow effect is found
    h1, h2 = S.size_halves(both, naive, own)
    assert h1["gain"] > 0 and h2["gain"] > 0
    q = S.quintiles(both)
    assert len(q) == 5 and q[-1]["rv"] > 1.3 * q[0]["rv"]
    cov = S.coverage(both)
    assert 0.5 < cov < 0.85
    now = S.size_now(srows, S.OWN + S.FLOWS)
    assert now and now["est"] > 0 and 0 <= now["rank"] <= 1 and set(now["parts"]) == set(S.OWN + S.FLOWS)
    _, _, naive0, own0, both0 = _size(_world(flow_effect=0.0, seed=6))
    s0 = S.size_scores(both0, naive0, own0)
    assert s0["t_gain"] < 2                                                # nothing planted, nothing found


def test_flow_events_take_the_extremes_and_merge_runs():
    rows = [{"day": f"d{i}", "t": i * M.DAY, "raw": {"etf": v}} for i, v in
            enumerate([float(i % 10) for i in range(80)] + [50.0, 60.0, 1.0, 70.0] + [5.0] * 10 + [-40.0] + [5.0] * 10 + [80.0])]
    ev = S.flow_events(rows, window=60, min_n=60, q=0.05, gap=5)
    ins = [e["row"]["day"] for e in ev if e["side"] == 1]
    outs = [e["row"]["day"] for e in ev if e["side"] == -1]
    assert ins == ["d80", "d105"]                       # 81 and 83 are the same run as 80; 105 is far enough
    assert outs == ["d94"]
    assert all(0.95 <= e["pct"] <= 1 for e in ev)


def test_event_test_measures_moves_above_the_average_day():
    t0 = M.ms_of("2025-03-03", M.ENTRY_H)
    prices, p = [], 100.0
    jump = {t0 + k * 7 * M.DAY for k in range(20)}
    for h in range(24 * 200):
        t = t0 - 10 * M.DAY + h * H
        p *= math.exp(0.0002 * (1 if h % 2 else -1))
        if any(0 < t - j <= 72 * H for j in jump):
            p *= math.exp(0.02 / 72)                    # +2% over the 3 days after each event
        prices.append((t, p))
    px = M.Series(prices)
    rows = [{"t": t0 + k * M.DAY} for k in range(180)]
    base = S.baseline(rows, px)
    res = S.event_test(sorted(jump), px, base)
    assert res[72]["n"] == 20 and res[72]["excess"] > 1.0 and res[72]["t"] > 2 and S.passes(res)
    assert res[24]["size"] > 1


def test_dat_events_are_the_biggest_buys_by_dollars():
    btc = [(M.ms_of("2025-01-01") + k * H, 100_000.0) for k in range(24 * 200)]
    d0 = date(2025, 1, 6)
    dat = [{"filed": (d0 + timedelta(weeks=k)).isoformat(), "bought": 100.0 + k} for k in range(20)]
    dat[15]["bought"] = 5_000.0
    ev = S.dat_events(dat, btc, q=0.8, min_n=10)
    assert ev[0]["day"] == dat[10]["filed"] and dat[15]["filed"] in [e["day"] for e in ev]
    assert all(e["usd"] > 0 for e in ev)


def test_strategy_table_from_october_2026_with_the_period_in_the_header():
    t = F.html_text(
        "<p>On October 12, 2026, Strategy announced updates with respect to its bitcoin holdings:</p>"
        "<p>ATM Update</p><table><tr><td>During Period October 5, 2026 to October 11, 2026</td></tr>"
        "<tr><td>MSTR Stock</td><td>Shares Sold</td><td>Notional Value</td></tr><tr><td>STRC</td><td>1,000</td><td>$0.1</td></tr></table>"
        "<p>BTC Update</p><table><tr><td>During Period October 5, 2026 to October 11, 2026</td><td></td><td></td>"
        "<td>As of October 11, 2026</td></tr>"
        "<tr><td>BTC Purchased (1)</td><td>Aggregate Purchase Price (in millions) (2)</td><td>Average Purchase Price (2)</td>"
        "<td>Aggregate BTC Holdings</td><td>Aggregate Purchase Price (in billions) (2)</td><td>Average Purchase Price (2)</td></tr>"
        "<tr><td>250</td><td>$</td><td>21.5</td><td>$</td><td>86,194</td><td>848,250</td><td>$</td><td>64.00</td><td>$</td>"
        "<td>75,444</td></tr></table><p>(1) $21.0 million in bitcoin purchases were made using net proceeds from MSTR Stock "
        "sales and $0.5 million in bitcoin purchases were made using USD Cash.</p>")
    assert F.parse_purchase(t) == {"bought": 250.0, "held": 848250.0, "period_end": "2026-10-11"}
    assert " 250 $ 21.5 $ 86,194 " in t
    none = t.replace(" 250 $ 21.5 $ 86,194 ", " — $ — $ — ")
    assert F.parse_purchase(none)["bought"] == 0.0


def test_strategy_reads_older_submission_pages_and_retries_unreachable_filings(tmp_path, monkeypatch):
    monkeypatch.setattr(F.asyncio, "sleep", _nosleep)
    table = ("<table><tr><td>Period</td><td>BTC Acquired</td><td>Aggregate Purchase Price (in millions)</td>"
             "<td>Average Purchase Price</td><td>Aggregate BTC Holdings</td></tr><tr><td>{a} - {b}</td>"
             "<td>{n}</td><td>$1.0</td><td>$100,000</td><td>{h}</td></tr></table>")
    recent = {"form": ["8-K", "4", "8-K"], "filingDate": ["2026-10-13", "2026-10-10", "2025-06-02"],
              "accessionNumber": ["0001-26-000003", "0001-26-000002", "0001-25-000001"],
              "primaryDocument": ["a.htm", "f4.xml", "b.htm"]}
    older = {"form": ["8-K", "8-K"], "filingDate": ["2024-12-02", "2023-05-01"],
             "accessionNumber": ["0001-24-000009", "0001-23-000001"], "primaryDocument": ["c.htm", "d.htm"]}
    flaky = {"n": 0}

    def handler(req):
        u = str(req.url)
        if u.endswith("CIK0001050446.json"):
            return httpx.Response(200, json={"filings": {"recent": recent, "files": [
                {"name": "CIK0001050446-submissions-001.json", "filingFrom": "2015-01-01", "filingTo": "2025-01-31"}]}})
        if u.endswith("submissions-001.json"):
            return httpx.Response(200, json=older)
        if u.endswith("index.json"):
            return httpx.Response(404)
        if u.endswith("a.htm"):
            flaky["n"] += 1
            if flaky["n"] == 1:
                return httpx.Response(503)                                       # the first time, the SEC is busy
            return httpx.Response(200, text=table.format(a="10/6/2026", b="10/12/2026", n="300", h="848,550"))
        if u.endswith("b.htm"):
            return httpx.Response(200, text=table.format(a="5/26/2025", b="6/1/2025", n="1,045", h="581,000"))
        if u.endswith("c.htm"):
            return httpx.Response(200, text=table.format(a="11/25/2024", b="12/1/2024", n="15,400", h="402,100"))
        return httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    cache = tmp_path / "s.json"
    got = asyncio.run(F.fetch_strategy("2024-01-01", cache=cache))
    assert [x["filed"] for x in got] == ["2024-12-02", "2025-06-02"]             # the older page read; 2023 left out
    assert "0001-26-000003" not in cache.read_text()                            # the busy one isn't remembered as empty
    got = asyncio.run(F.fetch_strategy("2024-01-01", cache=cache))
    assert [x["filed"] for x in got] == ["2024-12-02", "2025-06-02", "2026-10-13"] and got[-1]["bought"] == 300.0


def test_dvol_pages_backwards_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(F.asyncio, "sleep", _nosleep)
    now = 1_790_000_000_000 // H * H
    monkeypatch.setattr(F, "time_ms", lambda: now)
    start = now - 1500 * H
    calls = []

    def handler(req):
        a, b = int(req.url.params["start_timestamp"]), int(req.url.params["end_timestamp"])
        calls.append((a, b))
        hours = [t for t in range(b // H * H - H, a - 1, -H)][:1000]
        data = [[t, 50, 51, 49, 50 + (t // H) % 5] for t in reversed(hours)]
        cont = hours[-1] if len(hours) == 1000 else None
        return httpx.Response(200, json={"result": {"data": data, "continuation": cont}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    got = asyncio.run(F.fetch_dvol(start, tmp_path))
    assert len(calls) == 2 and got[0][0] == start + H and got[-1][0] == now      # stamped at each hour's end
    assert all(got[i][0] < got[i + 1][0] for i in range(len(got) - 1))
    calls.clear()
    got2 = asyncio.run(F.fetch_dvol(start, tmp_path))
    assert len(calls) == 1 and calls[0][0] >= now - 3 * H and got2 == got       # only the newest hours again

    def down(req):
        return httpx.Response(503)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(down)}))
    assert asyncio.run(F.fetch_dvol(start, tmp_path)) == got                    # the cache when Deribit is down
    (tmp_path / "empty").mkdir()
    assert asyncio.run(F.fetch_dvol(start, tmp_path / "empty")) == []                # no cache, no Deribit: nothing

"""The BTC estimate (engine/btcmodel.py, ingestion/flows.py): parsers, timing, the walk-forward fit."""
import math
import random
from datetime import date, timedelta

import numpy as np

from engine import btcmodel as M
from ingestion import flows as F

FARSIDE = """<html><body><table>
<tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
<tr><td>11 Jan 2024</td><td>111.7</td><td>227.0</td><td>(95.1)</td><td>243.6</td></tr>
<tr><td>12 Jan 2024</td><td>386.0</td><td>-</td><td>(484.1)</td><td>(98.1)</td></tr>
<tr><td>15 Jan 2024</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>
<tr><td>Total</td><td>497.7</td><td>227.0</td><td>(579.2)</td><td>145.5</td></tr>
<tr><td>Average</td><td>248.9</td><td>113.5</td><td>(289.6)</td><td>72.8</td></tr>
</table></body></html>"""


def test_farside_table_and_saved_files(tmp_path):
    rows = F.parse_farside(FARSIDE)
    assert [r["date"] for r in rows] == ["2024-01-11", "2024-01-12"]           # no holiday, no summary rows
    assert rows[1]["total"] == -98.1 and rows[1]["funds"] == {"IBIT": 386.0, "GBTC": -484.1}
    (tmp_path / "f.html").write_text(FARSIDE)
    assert F.read_etf_file(tmp_path / "f.html") == rows
    (tmp_path / "f.csv").write_text("Date,Total\n2024-01-11,243.6\n12 Jan 2024,(98.1)\nbad,1\n")
    assert [(r["date"], r["total"]) for r in F.read_etf_file(tmp_path / "f.csv")] == [("2024-01-11", 243.6), ("2024-01-12", -98.1)]


def test_strategy_filings_old_sentences_and_new_table():
    old = F.html_text("<p>during the period between August 2, 2022 and September 19, 2022, MicroStrategy acquired "
                      "approximately 301 bitcoins for approximately $6.0 million in cash.</p><p>As of September 19, 2022, "
                      "MicroStrategy, together with its subsidiaries, held an aggregate of approximately 130,000 bitcoins</p>")
    assert F.parse_purchase(old) == {"bought": 301.0, "held": 130000.0, "period_end": "2022-09-19"}
    new = F.html_text("<table><tr><td>Period</td><td>BTC Acquired</td><td>Aggregate Purchase Price (in millions)</td>"
                      "<td>Average Purchase Price</td><td>Aggregate BTC Holdings</td></tr><tr><td>10/27/2025 - 11/2/2025</td>"
                      "<td>397</td><td>$45.6</td><td>$114,771</td><td>641,205</td><td>$47.49</td></tr></table>")
    assert F.parse_purchase(new) == {"bought": 397.0, "held": 641205.0, "period_end": "2025-11-02"}
    none = F.html_text("<p>Strategy did not purchase any bitcoin during the period. As of June 1, 2025, Strategy "
                       "held an aggregate of approximately 580,250 bitcoins.</p>")
    assert F.parse_purchase(none)["bought"] == 0.0
    assert F.parse_purchase(F.html_text("<p>Annual meeting results.</p>")) is None


def test_zscores_never_see_the_present_or_the_future():
    raw = [1.0 + 0.1 * (i % 3) for i in range(30)] + [100.0] + [1.0] * 5
    z = M.zscore(raw, window=60, min_n=20)
    assert z[:20] == [None] * 20 and z[30] > 100                          # the spike against a quiet past
    assert z[31] < 0 and z[31] > -1                                       # and it only enters the windows after it
    assert M.zscore([5.0] * 30)[25] == 0.0                                # a flat past: no spread, 0


def _world(n_days=420, effect=0.6, seed=3):
    """US flow days with a planted effect: the next 24 h move from 14:00 UTC the day after is effect × the day's
    flow z-score (plus noise); BTC hourly prices built to match; Strategy flat; one purchase a week."""
    rnd = random.Random(seed)
    d0 = date(2024, 3, 4)
    days = [d0 + timedelta(days=i) for i in range(int(n_days * 1.45)) if (d0 + timedelta(days=i)).weekday() < 5][:n_days]
    etf = [{"date": d.isoformat(), "total": rnd.gauss(100, 300), "funds": {}} for d in days]
    moves = {}
    for x in etf:
        moves[M.ms_of(x["date"], M.ENTRY_H) + M.DAY] = effect * (x["total"] - 100) / 300 + rnd.gauss(0, 1.5)
    t, px, btc = M.ms_of(days[0].isoformat()) - 3 * M.DAY, 60_000.0, []
    end = M.ms_of(days[-1].isoformat()) + 4 * M.DAY
    starts = sorted(moves)
    j = 0
    while t <= end:
        while j < len(starts) and starts[j] < t - 24 * M.HOUR:
            j += 1
        drift = sum(moves[s] for s in starts[j:j + 2] if s < t <= s + 24 * M.HOUR) / 24
        px *= math.exp(drift / 100)
        btc.append((t, px))
        t += M.HOUR
    mstr = [(M.ms_of(d.isoformat(), 21), 300.0) for d in days]
    dat = [{"filed": d.isoformat(), "bought": 500.0} for d in days if d.weekday() == 0]
    return etf, dat, btc, mstr


def test_the_walk_forward_finds_a_planted_flow_effect_and_not_a_missing_one():
    etf, dat, btc, mstr = _world()
    rows = M.build_rows(etf, dat, btc, mstr)
    r = rows[100]
    assert r["t"] == M.ms_of(r["day"], M.ENTRY_H) + M.DAY and r["raw"]["dat"] > 0 and r["raw"]["taker"] is None
    done = M.usable(rows, ("etf", "etf5", "mom"))
    tune, held = M.split(done)
    assert not held and len(tune) == int(len(done) * 0.8)
    s = M.scores(M.walk_forward(tune, ("etf", "etf5", "mom"), 0.1))
    assert s["n"] > 100 and s["ic"] > 0.2 and s["t_ic"] > 2
    etf0, dat0, btc0, mstr0 = _world(effect=0.0, seed=4)
    rows0 = M.usable(M.build_rows(etf0, dat0, btc0, mstr0), ("etf", "etf5", "mom"))
    s0 = M.scores(M.walk_forward(rows0, ("etf", "etf5", "mom"), 0.1))
    assert abs(s0["ic"]) < 0.15                                            # nothing planted, nothing found
    now = M.estimate_now(rows, ("etf", "etf5", "mom"), 0.1)
    assert abs(now["est"] - now["intercept"] - sum(now["parts"].values())) < 1e-9 and 0 < now["p_up"] < 1
    assert now["coef"]["etf"] > 0.2 and "move next 24h" in now["equation"]


def test_book_terms_count_only_when_recorded_and_reactive_reads_same_day():
    etf, dat, btc, mstr = _world(n_days=80)
    last = M.ms_of(etf[-1]["date"], M.ENTRY_H) + M.DAY
    book = {last - k * M.HOUR: {"taker": 2e6, "imb": 0.2} for k in range(1, 25)}
    rows = M.build_rows(etf, dat, btc, mstr, book)
    assert rows[-1]["raw"]["taker"] == 48.0 and abs(rows[-1]["raw"]["book"] - 0.2) < 1e-12
    assert all(r["raw"]["taker"] is None for r in rows[:-1])
    rc = M.reactive(etf, btc)
    assert set(rc) == {"day before", "same day", "next day"}
    assert np.isfinite(list(rc.values())).all()


def test_yahoo_retries_falls_back_to_its_cache_and_stooq_parses(tmp_path, monkeypatch):
    import asyncio
    import httpx
    calls = []
    good = {"chart": {"result": [{"timestamp": [1_700_000_000], "indicators": {"quote": [{"close": [42.0]}]}}]}}

    def handler(req):
        calls.append(str(req.url))
        if "stooq" in str(req.url):
            return httpx.Response(200, text="Date,Open,High,Low,Close,Volume\n2026-11-02,1,2,0.5,300.5,10\n")
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=good)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    from ingestion import flows as FL
    monkeypatch.setattr(FL.asyncio, "sleep", _nosleep)
    rows = asyncio.run(FL.fetch_yahoo("MSTR", cache=tmp_path))
    assert rows == [(1_700_000_000_000, 42.0)] and "query2" in calls[1]       # retried on the second host
    calls.clear()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(lambda r: httpx.Response(429))}))
    assert asyncio.run(FL.fetch_yahoo("MSTR", cache=tmp_path)) == [(1_700_000_000_000, 42.0)]   # the cached reply
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    assert asyncio.run(FL.fetch_stooq("mstr.us")) == [(M.ms_of("2026-11-02"), 300.5)]


async def _nosleep(_s):
    return None


def test_tftc_days_in_millions_without_holidays():
    j = {"units": "USD", "days": [
        {"date": "2024-01-12", "netFlowUsd": 203000000, "perEtfUsd": {"IBIT": 386000000, "GBTC": -484100000.00000006}},
        {"date": "2024-01-11", "netFlowUsd": 655300000, "perEtfUsd": {"IBIT": 111700000}},
        {"date": "2024-11-28", "netFlowUsd": 0, "perEtfUsd": None},
        {"date": "2024-11-29", "netFlowUsd": 320000000, "perEtfUsd": None}]}
    rows = F.parse_tftc(j)
    assert [r["date"] for r in rows] == ["2024-01-11", "2024-01-12", "2024-11-29"]
    assert rows[1]["total"] == 203.0 and rows[1]["funds"] == {"IBIT": 386.0, "GBTC": -484.1} and rows[2]["funds"] == {}


def test_strategy_2026_table_with_spelled_out_periods_and_dashes():
    t = F.html_text(
        "<p>Strategy announced updates with respect to its bitcoin holdings during the periods and as of the dates indicated below</p>"
        "<table><tr><td>Period</td><td>BTC Acquired</td><td>Aggregate Purchase Price (in millions)</td><td>Average Purchase Price</td>"
        "<td>Aggregate BTC Holdings (1)</td><td>Aggregate Purchase Price (in billions)</td><td>Average Purchase Price</td></tr>"
        "<tr><td>March 30, 2026 to March 31, 2026</td><td>—</td><td>$</td><td>—</td><td>$</td><td>—</td><td>762,099</td>"
        "<td>$</td><td>57.69</td><td>$</td><td>75,694</td></tr>"
        "<tr><td>April 1, 2026 to April 5, 2026</td><td>4,871</td><td>$</td><td>329.9</td><td>$</td><td>67,718</td><td>766,970</td>"
        "<td>$</td><td>58.02</td><td>$</td><td>75,644</td></tr></table>"
        "<p>ATM sales during the period from April 1, 2026 to April 5, 2026: 1,234,567 shares for $400.1 million</p>")
    assert F.parse_purchase(t) == {"bought": 4871.0, "held": 766970.0, "period_end": "2026-04-05"}

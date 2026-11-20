"""Sentiment Check's long history: packing, Binance's file formats, the month/day update and Bybit's stand-in."""
import hashlib
import io
import zipfile
from datetime import datetime, timezone

import numpy as np
import pytest

from ingestion import longhist as L

NOW = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)


def _zip(name: str, text: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        zf.writestr(name, text)
    return b.getvalue()


def _klines(t0_s: int, n: int, step_s: int, unit: str, header: bool, price=100.0) -> str:
    rows = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"] if header else []
    for i in range(n):
        t = (t0_s + i * step_s) * (1_000_000 if unit == "us" else 1000)
        c = price + i * 0.01
        rows.append(f"{t},{c - 0.005},{c + 0.02},{c - 0.03},{c},{1.5 + i},{t + 999},0,{7 + i},{0.5 + i},0,0")
    return "\n".join(rows) + "\n"


def test_packing_round_trip():
    t = np.array([100, 101, 102, 104])
    c = np.array([91234.56, 91234.57, 91230.01, 91299.99])
    z = L.pack(t, c, c + 1.5, c - 2.25, c - 0.5, v=np.array([1.0, 2, 3, 4]), n=np.array([1, 2, 3, 4]))
    u = L.unpack(z)
    assert (u["t"] == t).all() and np.allclose(u["c"], c) and np.allclose(u["h"], c + 1.5) and np.allclose(u["l"], c - 2.25)
    assert np.allclose(u["o"], c - 0.5) and u["v"].dtype == np.float32 and list(u["n"]) == [1, 2, 3, 4]


def test_reading_binance_files():
    t0 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    fut = L.unpack(L.read_klines(_zip("x.csv", _klines(t0, 3, 60, "ms", header=True)), 60))
    assert list(fut["t"]) == [t0 // 60, t0 // 60 + 1, t0 // 60 + 2] and np.allclose(fut["c"], [100, 100.01, 100.02])
    assert np.allclose(fut["o"], [99.995, 100.005, 100.015]) and list(fut["n"]) == [7, 8, 9] and np.allclose(fut["tbv"], [0.5, 1.5, 2.5])
    t1 = int(datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp())
    spot = L.unpack(L.read_klines(_zip("y.csv", _klines(t1, 4, 1, "us", header=False)), 1))
    assert list(spot["t"]) == [t1, t1 + 1, t1 + 2, t1 + 3] and "o" not in spot and "n" not in spot   # microseconds read right
    met = L.read_metrics(_zip("m.csv", "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
                              "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
                              "2024-01-01 00:05:00,BTCUSDT,80000.5,3400000000,1,1,1,1\n"))
    assert met["t"][0] == (t0 + 300) // 60 and met["oi"][0] == 80000.5
    fund = L.read_funding(_zip("f.csv", f"calc_time,funding_interval_hours,last_funding_rate\n{t0 * 1000},8,0.0001\n"))
    assert fund["t"][0] == t0 // 60 and fund["rate"][0] == 0.0001


class Site:
    """Binance's file site, by URL: {url: bytes}; checksums made on the fly, anything else a 404."""

    def __init__(self, files: dict, bad: set = frozenset()):
        self.files, self.bad, self.asked = files, bad, []

    def get(self, url, timeout=None, params=None):
        self.asked.append(url)
        if url.endswith(".CHECKSUM"):
            data = self.files.get(url[: -len(".CHECKSUM")])
            body = "" if data is None else ("0" * 64 if url[:-9] in self.bad else hashlib.sha256(data).hexdigest()) + "  f.zip"
            return _Reply(200 if data is not None else 404, body.encode())
        data = self.files.get(url)
        return _Reply(200 if data is not None else 404, data or b"")


class _Reply:
    def __init__(self, status, content):
        self.status_code, self.content = status, content
        self.text = content.decode(errors="ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(self.status_code)

    def json(self):
        import json
        return json.loads(self.text)


def test_months_then_days_then_joined(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "YEARS", {**L.YEARS, "m1": 0.17})            # three months: Sep, Oct, Nov 2026
    def murl(m):
        return L._url("m1", "BTCUSDT", m, daily=False)
    def durl(d):
        return L._url("m1", "BTCUSDT", d, daily=True)
    t = lambda d: int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    files = {murl("2026-09"): _zip("a.csv", _klines(t("2026-09-01"), 5, 60, "ms", True))}
    for d in L.days_of("2026-10", NOW):                                   # October only as days (not published yet)
        files[durl(d)] = _zip("d.csv", _klines(t(d), 2, 60, "ms", True))
    for d in L.days_of("2026-11", NOW)[:-1]:                              # November so far, but not yesterday yet
        files[durl(d)] = _zip("d.csv", _klines(t(d), 2, 60, "ms", True))
    f = L.Fetcher(Site(files), pause_s=0)
    said = []
    L.update_binance(f, "BTC", "m1", tmp_path, NOW, say=said.append)
    names = sorted(p.name for p in (tmp_path / "BTC" / "m1").iterdir())
    assert "2026-09.npz" in names and "2026-10.npz" in names                       # October joined from its 31 days
    assert not any(n.startswith("2026-10-") for n in names) and "2026-11-18.npz" in names and "2026-11-19.none" not in names   # yesterday: asked again next run
    j = L.load("BTC", "m1", tmp_path)
    assert len(j["t"]) == 5 + 31 * 2 + 18 * 2 and (np.diff(j["t"]) > 0).all()
    f.client.asked.clear()
    L.update_binance(f, "BTC", "m1", tmp_path, NOW, say=said.append)                # again: nothing new is fetched
    assert not [u for u in f.client.asked if "/monthly/" in u and "2026-09" in u]
    with pytest.raises(IOError):                                                    # a corrupted download is refused
        L.Fetcher(Site(files, bad={murl("2026-09")}), pause_s=0).get(murl("2026-09"))


def test_bybit_stand_in():
    class Api:
        def get(self, url, params=None, timeout=None):
            start = params["start"]
            rows = [[str(start + i * 60_000), "40", "41", "39", "40.5", "100", "0"] for i in range(3)][::-1]  # newest first
            return _Reply(200, __import__("json").dumps({"result": {"list": rows}}).encode())
    got = L.unpack(L.bybit_m1(L.Fetcher(Api(), pause_s=0), "HYPEUSDT", 1_800_000_000, 1_800_000_000 + 180))
    assert list(got["t"]) == [30_000_000, 30_000_001, 30_000_002] and np.isnan(got["tbv"]).all() and np.allclose(got["c"], 40.5)

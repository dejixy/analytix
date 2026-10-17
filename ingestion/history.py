"""
Months of hourly history for the low-frequency lab (scripts/lowFreqLab.py): candles and funding from Hyperliquid
(the venue you trade; it keeps about the last 5,000 candles, so about 7 months of hours) and from Binance USD-M
futures (years, for a longer check). Cached as CSV under data/history and topped up on each fetch, so later runs
only download what's new.

    hl_BTC_1h.csv          t_ms (bar open), open, high, low, close, volume
    hl_BTC_funding.csv     t_ms, rate (per hour, as a fraction; positive = longs pay shorts)
    bn_BTC_1h.csv          the same from Binance (symbol BTCUSDT)
    bn_BTC_funding.csv     Binance funding (every 8 hours) as rate per 8 hours

Hyperliquid's info endpoint and Binance's REST API are both public. Requests are paced well inside their limits.
"""
import csv
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from config import HL_INFO_URL, ROOT

log = logging.getLogger("analytix.history")

HISTORY_DIR = ROOT / "data" / "history"
BN_URL = "https://fapi.binance.com"
HOUR_MS = 3_600_000
PAUSE_S = 0.3


@dataclass
class Bars:
    """One coin's hourly bars, oldest first, with each bar's funding (per hour, positive = longs pay)."""
    coin: str
    source: str
    t: np.ndarray            # bar open (ms)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    funding: np.ndarray      # per hour; 0 where unknown

    def __len__(self) -> int:
        return len(self.t)


# ── the cache ───────────────────────────────────────────────────────────────
def _read(path: Path) -> list[list[float]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return [[float(x) for x in row] for row in csv.reader(fh) if row and not row[0].startswith("t")]


def _write(path: Path, header: list[str], rows: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([int(r[0])] + [repr(float(x)) for x in r[1:]])
    tmp.replace(path)


def _merge(old: list[list[float]], new: list[list[float]], drop_last_open: bool) -> list[list[float]]:
    by = {int(r[0]): r for r in old}
    for r in new:
        by[int(r[0])] = r
    rows = [by[k] for k in sorted(by)]
    if drop_last_open and rows and rows[-1][0] + HOUR_MS > time.time() * 1000:
        rows = rows[:-1]                                     # the bar still forming
    return rows


# ── fetchers ────────────────────────────────────────────────────────────────
class Fetcher:
    def __init__(self, client, hl_url: str = HL_INFO_URL, bn_url: str = BN_URL, pause_s: float = PAUSE_S):
        self.client = client                                 # an httpx.Client (or anything with .post/.get)
        self.hl_url = hl_url
        self.bn_url = bn_url
        self.pause_s = pause_s

    def _post(self, body: dict):
        r = self.client.post(self.hl_url, json=body, timeout=30)
        r.raise_for_status()
        time.sleep(self.pause_s)
        return r.json()

    def _get(self, path: str, params: dict):
        r = self.client.get(self.bn_url + path, params=params, timeout=30)
        r.raise_for_status()
        time.sleep(self.pause_s)
        return r.json()

    def hl_candles(self, coin: str, since_ms: int, until_ms: int) -> list[list[float]]:
        out, start = [], since_ms
        while start < until_ms:
            end = min(until_ms, start + 2000 * HOUR_MS)
            got = self._post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start,
                                                                "endTime": end}}) or []
            out += [[int(b["t"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])] for b in got]
            start = end
        return out

    def hl_funding(self, coin: str, since_ms: int, until_ms: int) -> list[list[float]]:
        out, start = [], since_ms
        while start < until_ms:
            got = self._post({"type": "fundingHistory", "coin": coin, "startTime": start, "endTime": until_ms}) or []
            rows = [[int(x["time"]), float(x["fundingRate"])] for x in got]
            if not rows:
                break
            out += rows
            last = max(r[0] for r in rows)
            if last + 1 <= start:
                break
            start = last + 1
        return out

    def bn_candles(self, symbol: str, since_ms: int, until_ms: int) -> list[list[float]]:
        out, start = [], since_ms
        while start < until_ms:
            got = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "startTime": start,
                                                "endTime": until_ms, "limit": 1500}) or []
            if not got:
                break
            out += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in got]
            start = int(got[-1][0]) + HOUR_MS
        return out

    def bn_funding(self, symbol: str, since_ms: int, until_ms: int) -> list[list[float]]:
        out, start = [], since_ms
        while start < until_ms:
            got = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": start, "endTime": until_ms,
                                                     "limit": 1000}) or []
            if not got:
                break
            out += [[int(x["fundingTime"]), float(x["fundingRate"])] for x in got]
            start = int(got[-1]["fundingTime"]) + 1
        return out


def update(coins: list[str], hl_days: float, bn_days: float, folder: Path = HISTORY_DIR, client=None,
           say=print) -> None:
    """Top up the cache: Hyperliquid for hl_days back, Binance for bn_days back (0 = skip). A source that can't be
    reached is skipped with a note, not fatal."""
    import httpx
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": "analytix-lowfreq/1"})
    f = Fetcher(client)
    now = int(time.time() * 1000)
    try:
        for coin in coins:
            jobs = []
            if hl_days:
                jobs += [("hl", "1h", f.hl_candles, coin, hl_days), ("hl", "funding", f.hl_funding, coin, hl_days)]
            if bn_days:
                sym = f"{coin}USDT"
                jobs += [("bn", "1h", f.bn_candles, sym, bn_days), ("bn", "funding", f.bn_funding, sym, bn_days)]
            for src, kind, fn, arg, days in jobs:
                path = folder / f"{src}_{coin}_{kind}.csv"
                old = _read(path)
                since = int(old[-1][0]) + 1 if old else now - int(days * 86_400_000)
                try:
                    new = fn(arg, since, now)
                except Exception as exc:                     # blocked, unknown symbol, rate limited
                    say(f"  {src} {coin} {kind}: couldn't fetch ({str(exc)[:120]}); using what's cached ({len(old)} rows)")
                    continue
                rows = _merge(old, new, drop_last_open=kind == "1h")
                _write(path, ["t_ms", "open", "high", "low", "close", "volume"] if kind == "1h" else ["t_ms", "rate"], rows)
                say(f"  {src} {coin} {kind}: {len(rows):,} rows (+{len(rows) - len(old):,})")
    finally:
        if own:
            client.close()


def load(coin: str, source: str = "hl", folder: Path = HISTORY_DIR) -> Bars | None:
    """The cached bars for one coin, with funding spread over the hours it covers. None if nothing is cached."""
    rows = _read(folder / f"{source}_{coin}_1h.csv")
    if len(rows) < 2:
        return None
    a = np.array(rows)
    t = a[:, 0].astype(np.int64)
    fund = np.zeros(len(t))
    fr = _read(folder / f"{source}_{coin}_funding.csv")
    if fr:
        ft = np.array([int(r[0]) for r in fr], dtype=np.int64)
        rate = np.array([r[1] for r in fr])
        if source == "bn":
            # an 8-hour rate charged at its time: spread over the 8 hours before it
            for k, (tt, r) in enumerate(zip(ft, rate)):
                lo, hi = np.searchsorted(t, tt - 8 * HOUR_MS), np.searchsorted(t, tt)
                fund[lo:hi] = r / 8
        else:
            # Hyperliquid charges each hour; the payment at time T covers the hour before it
            idx = np.rint((ft - HOUR_MS - t[0]) / HOUR_MS).astype(np.int64)      # the nearest bar
            ok = (idx >= 0) & (idx < len(t))
            ok[ok] &= np.abs(t[idx[ok]] - (ft[ok] - HOUR_MS)) < HOUR_MS // 2
            fund[idx[ok]] = rate[ok]
    return Bars(coin, source, t, a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], fund)

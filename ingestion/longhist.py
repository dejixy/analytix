"""
Years of market history for Sentiment Check (engine/sentiment.py), from Binance's free bulk files
(data.binance.vision), with Bybit's public API standing in for any coin Binance doesn't list.

    kind      what                                                   back     one file per
    m1        USD-M futures 1-minute candles: o h l c, volume,       5 years  month
              taker buy volume, trades
    s1        spot 1-second candles: c h l, volume, taker buy vol    3 years  month
    oi        USD-M open interest every 5 minutes (and its value)    5 years  month (built from daily files)
    funding   USD-M funding rates                                    5 years  month

Kept under data/longhist/<COIN>/<kind>/<YYYY-MM>.npz. Prices are stored as whole 1/10,000ths, as each step from the last
(they compress to a few bytes a row): a month of 1-second candles is about 25 MB, five years of everything for one coin
about 1 GB. The month in progress is kept day by day (<YYYY-MM-DD>.npz) until Binance publishes the whole month, then
replaced. A month or day Binance doesn't have (before a listing) leaves a small .none marker so it isn't asked again
for a week.

Each file is downloaded, checked against Binance's published SHA-256, read, saved, and the download deleted, so the
disk never holds more than one zip at a time. Run it again any time: it only fetches what's missing.

HYPE: if Binance's futures files don't have HYPEUSDT, Bybit's linear perpetual is used instead (1-minute candles, open
interest and funding; Bybit gives no taker buy split, so those are NaN, and no 1-second candles).
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from config import ROOT

BASE = "https://data.binance.vision/data"
BYBIT = "https://api.bybit.com"
FOLDER = ROOT / "data" / "longhist"
SCALE = 10_000                                     # prices kept in whole 1/10,000ths
YEARS = {"m1": 5, "s1": 3, "oi": 5, "funding": 5}
NONE_AGAIN_S = 7 * 86400                           # a missing month is asked about again after a week
PAUSE_S = 0.05


# ── packing ──────────────────────────────────────────────────────────────────
def pack(t: np.ndarray, c: np.ndarray, h: np.ndarray, l: np.ndarray, o: np.ndarray | None = None, **extra) -> dict:
    """Candles as small arrays: times and prices as steps from the one before (they compress well), highs and lows
    (and opens) as offsets from the close."""
    t = np.asarray(t, dtype=np.int64)
    ct = np.round(np.asarray(c, dtype=np.float64) * SCALE).astype(np.int64)
    out = {"dt": np.diff(t, prepend=0), "dc": np.diff(ct, prepend=0),
           "hc": (np.round(np.asarray(h, dtype=np.float64) * SCALE).astype(np.int64) - ct).astype(np.int32),
           "cl": (ct - np.round(np.asarray(l, dtype=np.float64) * SCALE).astype(np.int64)).astype(np.int32)}
    if o is not None:
        out["oc"] = (np.round(np.asarray(o, dtype=np.float64) * SCALE).astype(np.int64) - ct).astype(np.int32)
    for k, v in extra.items():
        out[k] = np.asarray(v, dtype=np.float32 if np.asarray(v).dtype.kind == "f" else np.int32)
    return out


def unpack(z) -> dict:
    """pack()'s arrays back into t, c, h, l (o) and the extras, as plain numbers."""
    t = np.cumsum(z["dt"])
    ct = np.cumsum(z["dc"])
    out = {"t": t, "c": ct / SCALE, "h": (ct + z["hc"]) / SCALE, "l": (ct - z["cl"]) / SCALE}
    if "oc" in z:
        out["o"] = (ct + z["oc"]) / SCALE
    for k in z:
        if k not in ("dt", "dc", "hc", "cl", "oc"):
            out[k] = np.asarray(z[k])
    return out


def save(path: Path, arrays: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, path)


# ── reading Binance's files ──────────────────────────────────────────────────
def _csv_member(data: bytes) -> tuple[io.StringIO, bool]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        text = zf.read(name).decode()
    first = text.split("\n", 1)[0]
    return io.StringIO(text), bool(first) and not first[0].isdigit()


def read_klines(data: bytes, unit_s: int) -> dict:
    """A kline zip → packed arrays. Times become whole seconds (unit_s=1) or minutes (unit_s=60); Binance's spot files
    give microseconds from 2025 and milliseconds before, which is told apart by size."""
    buf, header = _csv_member(data)
    a = np.loadtxt(buf, delimiter=",", skiprows=1 if header else 0, usecols=(0, 1, 2, 3, 4, 5, 8, 9), ndmin=2)
    if not len(a):
        return {}
    t = a[:, 0]
    t = t / 1e6 if t[0] > 1e14 else t / 1e3                         # → seconds
    t = np.round(t / unit_s).astype(np.int64)
    return pack(t, a[:, 4], a[:, 2], a[:, 3], a[:, 1] if unit_s == 60 else None,
                v=a[:, 5], tbv=a[:, 7], **({"n": a[:, 6].astype(np.int64)} if unit_s == 60 else {}))


def read_metrics(data: bytes) -> dict:
    """A daily metrics zip → minutes, open interest (coins) and its value (USD)."""
    buf, _ = _csv_member(data)
    t, oi, val = [], [], []
    for r in csv.DictReader(buf):
        try:
            dt = datetime.strptime(r["create_time"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            t.append(int(dt.timestamp()) // 60)
            oi.append(float(r["sum_open_interest"]))
            val.append(float(r["sum_open_interest_value"]))
        except (KeyError, ValueError):
            continue
    return {"t": np.array(t, dtype=np.int64), "oi": np.array(oi), "oi_usd": np.array(val)}


def read_funding(data: bytes) -> dict:
    buf, _ = _csv_member(data)
    t, rate = [], []
    for r in csv.DictReader(buf):
        try:
            t.append(int(float(r["calc_time"])) // 60_000)
            rate.append(float(r["last_funding_rate"]))
        except (KeyError, ValueError):
            continue
    return {"t": np.array(t, dtype=np.int64), "rate": np.array(rate)}


def _cat(parts: list[dict], keys_packed: bool) -> dict:
    """Days of one month joined (packed candles are unpacked, joined and packed again)."""
    parts = [p for p in parts if p]
    if not parts:
        return {}
    if not keys_packed:
        return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    u = [unpack(p) for p in parts]
    j = {k: np.concatenate([x[k] for x in u]) for k in u[0]}
    extra = {k: v for k, v in j.items() if k not in ("t", "c", "h", "l", "o")}
    return pack(j["t"], j["c"], j["h"], j["l"], j.get("o"), **extra)


# ── fetching ─────────────────────────────────────────────────────────────────
class Fetcher:
    """Downloads with a check against Binance's .CHECKSUM files. get() returns the bytes, or None if there's no such
    file (404)."""

    def __init__(self, client, pause_s: float = PAUSE_S):
        self.client, self.pause_s = client, pause_s
        self.bytes = 0

    def get(self, url: str, check: bool = True) -> bytes | None:
        r = self.client.get(url, timeout=120)
        time.sleep(self.pause_s)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.content
        self.bytes += len(data)
        if check:
            c = self.client.get(url + ".CHECKSUM", timeout=30)
            if c.status_code == 200 and c.text.split():
                want = c.text.split()[0].strip().lower()
                if hashlib.sha256(data).hexdigest() != want:
                    raise IOError(f"checksum mismatch for {url.rsplit('/', 1)[-1]}")
        return data

    def json(self, url: str, params: dict):
        r = self.client.get(url, params=params, timeout=60)
        time.sleep(self.pause_s)
        r.raise_for_status()
        return r.json()


def months(years: float, now: datetime | None = None) -> list[str]:
    """Every month from `years` back to the current one, as YYYY-MM, oldest first (5 years: 61 months)."""
    now = now or datetime.now(timezone.utc)
    last = now.year * 12 + now.month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(last - round(years * 12), last + 1)]


def days_of(month: str, until: datetime) -> list[str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return [f"{month}-{d:02d}" for d in range(1, last + 1)
            if datetime(y, m, d, tzinfo=timezone.utc) + timedelta(days=1) <= until]


def _none_fresh(path: Path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < NONE_AGAIN_S


URLS = {
    "m1": ("futures/um", "klines/{sym}/1m/{sym}-1m-{when}.zip"),
    "s1": ("spot", "klines/{sym}/1s/{sym}-1s-{when}.zip"),
    "oi": ("futures/um", "metrics/{sym}/{sym}-metrics-{when}.zip"),
    "funding": ("futures/um", "fundingRate/{sym}/{sym}-fundingRate-{when}.zip"),
}


def _url(kind: str, sym: str, when: str, daily: bool) -> str:
    market, tail = URLS[kind]
    return f"{BASE}/{market}/{'daily' if daily else 'monthly'}/{tail.format(sym=sym, when=when)}"


def _read(kind: str, data: bytes) -> dict:
    if kind == "m1":
        return read_klines(data, 60)
    if kind == "s1":
        return read_klines(data, 1)
    if kind == "oi":
        return read_metrics(data)
    return read_funding(data)


def update_binance(f: Fetcher, coin: str, kind: str, folder: Path = FOLDER, now: datetime | None = None,
                   say=print) -> int:
    """Fetch what's missing of one coin's kind. Returns how many files were added."""
    now = now or datetime.now(timezone.utc)
    sym = f"{coin}USDT"
    out = folder / coin / kind
    added = 0
    this_month = now.strftime("%Y-%m")
    for month in months(YEARS[kind], now):
        path, none = out / f"{month}.npz", out / f"{month}.none"
        if path.exists() or (_none_fresh(none) and month != this_month):
            continue
        whole = None
        if kind != "oi" and month != this_month:                     # metrics only come as daily files
            whole = f.get(_url(kind, sym, month, daily=False))
        if whole is not None:
            arrays = _read(kind, whole)
            if arrays:
                save(path, arrays)
                added += 1
                for d in out.glob(f"{month}-*.npz"):                 # the days it replaces
                    d.unlink()
                say(f"  {coin} {kind} {month}: {_rows(arrays):,} rows")
            continue
        got = 0                                                      # no whole month yet: day by day
        for day in days_of(month, now):
            dpath, dnone = out / f"{day}.npz", out / f"{day}.none"
            if dpath.exists() or _none_fresh(dnone):
                got += dpath.exists()
                continue
            data = f.get(_url(kind, sym, day, daily=True))
            if data is None:
                if now - datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) > timedelta(days=3):
                    dnone.parent.mkdir(parents=True, exist_ok=True)    # a recent day may just not be published yet
                    dnone.touch()
                continue
            arrays = _read(kind, data)
            if arrays:
                save(dpath, arrays)
                got += 1
                added += 1
        finished = month != this_month and got == len(days_of(month, now)) and got > 0
        if finished:                                                 # a whole past month from its days: join them
            parts = [dict(np.load(p)) for p in sorted(out.glob(f"{month}-*.npz"))]
            save(path, _cat(parts, keys_packed=kind in ("m1", "s1")))
            for d in out.glob(f"{month}-*.npz"):
                d.unlink()
            say(f"  {coin} {kind} {month}: joined from {got} days")
        elif got == 0 and month != this_month:
            none.parent.mkdir(parents=True, exist_ok=True)
            none.touch()
        elif got:
            say(f"  {coin} {kind} {month}: {got} days so far")
    return added


def _rows(arrays: dict) -> int:
    return len(arrays.get("dt", arrays.get("t", [])))


# ── Bybit, for a coin Binance doesn't list ────────────────────────────────────
def bybit_m1(f: Fetcher, sym: str, since_s: int, until_s: int) -> dict:
    """Bybit linear 1-minute candles (no taker buy split: NaN)."""
    rows, start = {}, since_s * 1000
    while start < until_s * 1000:
        end = min(until_s * 1000, start + 1000 * 60_000)
        got = f.json(f"{BYBIT}/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "1",
                                                   "start": start, "end": end - 1, "limit": 1000})
        for k in (got.get("result") or {}).get("list") or []:
            rows[int(k[0]) // 60_000] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        start = end
    if not rows:
        return {}
    t = np.array(sorted(rows), dtype=np.int64)
    a = np.array([rows[x] for x in t])
    return pack(t, a[:, 3], a[:, 1], a[:, 2], a[:, 0], v=a[:, 4], tbv=np.full(len(t), np.nan), n=np.zeros(len(t), dtype=np.int64))


def bybit_oi(f: Fetcher, sym: str, since_s: int, until_s: int) -> dict:
    out, cursor = {}, None
    while True:
        params = {"category": "linear", "symbol": sym, "intervalTime": "5min", "startTime": since_s * 1000,
                  "endTime": until_s * 1000, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        got = (f.json(f"{BYBIT}/v5/market/open-interest", params).get("result") or {})
        for x in got.get("list") or []:
            out[int(x["timestamp"]) // 60_000] = float(x["openInterest"])
        cursor = got.get("nextPageCursor")
        if not cursor or not got.get("list"):
            break
    t = np.array(sorted(out), dtype=np.int64)
    return {"t": t, "oi": np.array([out[x] for x in t]), "oi_usd": np.full(len(t), np.nan)} if len(t) else {}


def bybit_funding(f: Fetcher, sym: str, since_s: int, until_s: int) -> dict:
    out, end = {}, until_s * 1000
    while end > since_s * 1000:
        got = (f.json(f"{BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": sym,
                                                               "startTime": since_s * 1000, "endTime": end,
                                                               "limit": 200}).get("result") or {}).get("list") or []
        if not got:
            break
        for x in got:
            out[int(x["fundingRateTimestamp"]) // 60_000] = float(x["fundingRate"])
        oldest = min(int(x["fundingRateTimestamp"]) for x in got)
        if oldest >= end:
            break
        end = oldest - 1
    t = np.array(sorted(out), dtype=np.int64)
    return {"t": t, "rate": np.array([out[x] for x in t])} if len(t) else {}


def update_bybit(f: Fetcher, coin: str, kind: str, folder: Path = FOLDER, now: datetime | None = None,
                 say=print, years: float = 2) -> int:
    """One coin's kind from Bybit, month by month (the month in progress is fetched again each run)."""
    now = now or datetime.now(timezone.utc)
    sym, out, added = f"{coin}USDT", folder / coin / kind, 0
    fn = {"m1": bybit_m1, "oi": bybit_oi, "funding": bybit_funding}.get(kind)
    if fn is None:
        return 0
    this_month = now.strftime("%Y-%m")
    for month in months(years, now):
        path, none = out / f"{month}.npz", out / f"{month}.none"
        if (path.exists() and month != this_month) or _none_fresh(none):
            continue
        y, m = map(int, month.split("-"))
        a = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        b = min(int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp()), int(now.timestamp()))
        arrays = fn(f, sym, a, b)
        if arrays:
            save(path, arrays)
            added += 1
            say(f"  {coin} {kind} {month}: {_rows(arrays):,} rows (Bybit)")
        elif month != this_month:
            none.parent.mkdir(parents=True, exist_ok=True)
            none.touch()
    return added


# ── the whole update ──────────────────────────────────────────────────────────
def binance_lists(f: Fetcher, coin: str, now: datetime | None = None) -> bool:
    """Whether Binance's futures files have this coin (last month's 1-minute candles, or yesterday's)."""
    now = now or datetime.now(timezone.utc)
    last = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if f.client.get(_url("m1", f"{coin}USDT", last, daily=False) + ".CHECKSUM", timeout=30).status_code == 200:
        return True
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return f.client.get(_url("m1", f"{coin}USDT", day, daily=True) + ".CHECKSUM", timeout=30).status_code == 200


def update(coins: list[str], kinds: tuple = ("m1", "oi", "funding", "s1"), folder: Path = FOLDER, client=None,
           say=print) -> None:
    import httpx
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": "analytix-sentiment/1"}, follow_redirects=True)
    f = Fetcher(client)
    try:
        for coin in coins:
            listed = binance_lists(f, coin)
            say(f"{coin}: {'Binance' if listed else 'Bybit (not on Binance futures)'}")
            for kind in kinds:
                t0 = time.time()
                try:
                    if listed:
                        n = update_binance(f, coin, kind, folder, say=say)
                    else:
                        n = update_bybit(f, coin, kind, folder, say=say)
                except Exception as exc:                     # noqa: BLE001 - one kind down, the others still go
                    say(f"  {coin} {kind}: stopped ({type(exc).__name__}: {str(exc)[:160]}); run again to carry on")
                    continue
                say(f"  {coin} {kind}: {n} new files in {time.time() - t0:.0f}s ({f.bytes / 1e9:.2f} GB fetched so far)")
    finally:
        if own:
            client.close()


def load(coin: str, kind: str, folder: Path = FOLDER, since_s: int | None = None) -> dict:
    """One coin's kind, every saved month and day joined in time order (candles unpacked). Times are minutes for m1,
    oi and funding, seconds for s1."""
    out = folder / coin / kind
    files = sorted(out.glob("*.npz"), key=lambda p: p.stem)
    parts = []
    for p in files:
        z = dict(np.load(p))
        parts.append(unpack(z) if "dc" in z else z)
    if not parts:
        return {}
    j = {k: np.concatenate([x[k] for x in parts if k in x]) for k in parts[0]}
    order = np.argsort(j["t"], kind="stable")
    j = {k: v[order] for k, v in j.items()}
    keep = np.concatenate([[True], np.diff(j["t"]) > 0])           # a day and its month overlapping: once
    if since_s is not None:
        unit = 1 if kind == "s1" else 60
        keep &= j["t"] >= since_s // unit
    return {k: v[keep] for k, v in j.items()}

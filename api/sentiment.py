"""
Sentiment Check service: reads the market's conditions right now (Binance, the same source as the tables) and looks
them up in the five-year tables built by scripts/sentimentData.py (engine/sentiment.py has the method).

    on request    the last day of 1-minute candles and the last 16 minutes of 1-second candles, open interest's last
                  hour and the last funding rate (cached 15 s), turned into the same conditions the tables were built
                  on, using the 30-day normals saved beside the tables
    tables        data/sentiment/<coin>.npz and .json, re-read when the files change
    Hold Clock    how often, like now, price went as far as a bust within each hold time (hold())

Bybit stands in when Binance doesn't answer or doesn't list the coin (no taker flow there, so that condition reads
"unknown" and is dropped from the lookup).
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import numpy as np

from engine import calendar as CAL
from engine import releases as RL
from engine import sentiment as S

log = logging.getLogger("analytix.sentiment")

FOLDER = Path(__file__).resolve().parents[1] / "data" / "sentiment"
FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
LIVE_S = 15.0
STALE_S = 120.0                  # the hold clock uses a read up to this old while a new one comes in
QUIET_S = 60
HOLD = {10: "10s", 30: "30s", 60: "1m", 120: "2m", 300: "5m", 900: "15m"}


class SentimentCheck:
    def __init__(self, runtime=None, folder: Path = FOLDER, client=None):
        self.rt = runtime
        self.folder = Path(folder)
        self.client = client
        self._tables: dict[str, tuple[float, dict, dict]] = {}
        self._live: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._pending: set[str] = set()

    @property
    def live_ok(self) -> bool:
        return self.client is not None or (self.rt is None or getattr(self.rt, "mode", "live") == "live")

    def coins(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.npz")) if self.folder.exists() else []

    # ── tables ─────────────────────────────────────────────────────────────
    def tables(self, coin: str) -> tuple[dict, dict] | None:
        npz, meta = self.folder / f"{coin}.npz", self.folder / f"{coin}.json"
        if not npz.exists() or not meta.exists():
            return None
        stamp = max(npz.stat().st_mtime, meta.stat().st_mtime)
        hit = self._tables.get(coin)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        try:
            t, m = S.load_tables(npz), json.loads(meta.read_text())
        except Exception as exc:                      # noqa: BLE001 - a half-written file: keep the last good one
            log.warning("sentiment tables for %s unreadable: %s", coin, exc)
            return (hit[1], hit[2]) if hit else None
        self._tables[coin] = (stamp, t, m)
        return t, m

    # ── live data ──────────────────────────────────────────────────────────
    def _http(self):
        if self.client is None:
            import httpx
            self.client = httpx.Client(headers={"User-Agent": "analytix-sentiment/1"}, timeout=10)
        return self.client

    def _get(self, url: str, params: dict):
        r = self._http().get(url, params=params)
        r.raise_for_status()
        return r.json()

    def _binance(self, coin: str) -> dict:
        sym, now_ms = f"{coin}USDT", int(time.time() * 1000)
        out = {}
        k = self._get(f"{FAPI}/fapi/v1/klines", {"symbol": sym, "interval": "1m", "limit": 1500})
        k = [x for x in k if int(x[6]) < now_ms]                   # finished candles only
        out["m1"] = {"t": np.array([int(x[0]) // 60000 for x in k]), "c": np.array([float(x[4]) for x in k]),
                     "h": np.array([float(x[2]) for x in k]), "l": np.array([float(x[3]) for x in k]),
                     "v": np.array([float(x[5]) for x in k]), "tbv": np.array([float(x[9]) for x in k])}
        try:
            k = self._get(f"{SPOT}/api/v3/klines", {"symbol": sym, "interval": "1s", "limit": 1000})
            k = [x for x in k if int(x[6]) < now_ms]
            out["s1"] = {"t": np.array([int(x[0]) // 1000 for x in k]), "c": np.array([float(x[4]) for x in k]),
                         "h": np.array([float(x[2]) for x in k]), "l": np.array([float(x[3]) for x in k]),
                         "v": np.array([float(x[5]) for x in k]), "tbv": np.array([float(x[9]) for x in k])}
        except Exception:                                          # noqa: BLE001 - no spot market: no short horizons
            pass
        try:
            oi = self._get(f"{FAPI}/futures/data/openInterestHist", {"symbol": sym, "period": "5m", "limit": 14})
            out["oi"] = {"t": np.array([int(x["timestamp"]) // 60000 for x in oi]),
                         "oi": np.array([float(x["sumOpenInterest"]) for x in oi])}
        except Exception:                                          # noqa: BLE001
            pass
        try:
            f = self._get(f"{FAPI}/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
            out["funding"] = {"t": np.array([int(x["fundingTime"]) // 60000 for x in f]),
                              "rate": np.array([float(x["fundingRate"]) for x in f])}
        except Exception:                                          # noqa: BLE001
            pass
        out["source"] = "Binance"
        return out

    def _bybit(self, coin: str) -> dict:
        sym, now_ms = f"{coin}USDT", int(time.time() * 1000)
        rows = self._get(f"{BYBIT}/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "1", "limit": 1000})
        k = sorted((x for x in rows["result"]["list"] if int(x[0]) + 60_000 <= now_ms), key=lambda x: int(x[0]))
        out = {"m1": {"t": np.array([int(x[0]) // 60000 for x in k]), "c": np.array([float(x[4]) for x in k]),
                      "h": np.array([float(x[2]) for x in k]), "l": np.array([float(x[3]) for x in k]),
                      "v": np.array([float(x[5]) for x in k]), "tbv": np.full(len(k), np.nan)}, "source": "Bybit"}
        try:
            oi = self._get(f"{BYBIT}/v5/market/open-interest", {"category": "linear", "symbol": sym,
                                                                 "intervalTime": "5min", "limit": 14})
            lst = sorted(oi["result"]["list"], key=lambda x: int(x["timestamp"]))
            out["oi"] = {"t": np.array([int(x["timestamp"]) // 60000 for x in lst]),
                         "oi": np.array([float(x["openInterest"]) for x in lst])}
            f = self._get(f"{BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": sym, "limit": 1})
            lst = f["result"]["list"]
            out["funding"] = {"t": np.array([int(x["fundingRateTimestamp"]) // 60000 for x in lst]),
                              "rate": np.array([float(x["fundingRate"]) for x in lst])}
        except Exception:                                          # noqa: BLE001
            pass
        return out

    def fetch(self, coin: str) -> dict:
        try:
            return self._binance(coin)
        except Exception as exc:                                   # noqa: BLE001
            log.info("sentiment: Binance didn't answer for %s (%s); trying Bybit", coin, exc)
            return self._bybit(coin)

    async def live(self, coin: str) -> dict:
        hit = self._live.get(coin)
        if hit and time.time() - hit[0] < LIVE_S:
            return hit[1]
        async with self._lock:
            hit = self._live.get(coin)
            if hit and time.time() - hit[0] < LIVE_S:
                return hit[1]
            data = await asyncio.to_thread(self.fetch, coin)
            self._live[coin] = (time.time(), data)
            return data

    # ── the read ───────────────────────────────────────────────────────────
    @staticmethod
    def _now(fam: dict | None, d: dict | None, step: int, short: bool, oi, fund) -> S.Prepared | None:
        if not fam or not d or len(d.get("t", [])) < 10:
            return None
        s = S.to_grid(d["t"], d["c"], d["h"], d["l"], d["v"], d["tbv"], step, QUIET_S if short else 0)
        return S.prepare(s, S.raw(s, short), S.Normals.from_json(fam.get("normals")).latest(), oi, fund)

    def compute(self, coin: str, tables: dict, meta: dict, data: dict) -> dict:
        """Every horizon's like-now read, the conditions behind it and how well this kind of read held up."""
        rel = RL.times_s([t for (t, _), imp in CAL.LIVE.items() if imp == "High"])
        oi, fund = data.get("oi"), data.get("funding")
        fams = {"long": self._now(meta.get("long"), data.get("m1"), 60, False, oi, fund),
                "short": self._now(meta.get("short"), data.get("s1"), 1, True, oi, fund)}
        out = {"coin": coin, "source": data.get("source"), "built": meta.get("built"), "now": {}, "horizons": [],
               "history": {f: {"from": (meta.get(f) or {}).get("from"), "to": (meta.get(f) or {}).get("to")}
                           for f in ("long", "short") if meta.get(f)}}
        for fam, p in fams.items():
            if p is None:
                continue
            i = len(p.s) - 1
            b = S.bins_at(p, [i], 3600 if fam == "long" else 60, rel)[0]
            out["now"][fam] = {"at": int(p.s.t[i]) * 1000, "price": float(p.s.c[i]),
                               **{f: S.LABELS[f][v] if v < S.NBINS[f] else "unknown"
                                  for f, v in zip(S.FEATURES, b) if f not in ("move", "news")}}
        for h, sec in S.HORIZONS.items():
            fam = "short" if h in S.SHORT else "long"
            p, t = fams[fam], tables.get(h)
            if p is None or t is None:
                continue
            i = len(p.s) - 1
            b = S.bins_at(p, [i], sec, rel)[0]
            scale = S.scale_at(p, i, sec)
            if not np.isfinite(scale) or scale <= 0:
                continue
            r = S.read(t, b, scale)
            if not r.get("n"):
                continue
            r.update({"seconds": sec, "family": fam, "move": _label("move", b), "news": _label("news", b),
                      "conditions": {f: _label(f, b) for f in S.FEATURES},
                      "check": (meta.get("checks") or {}).get(h), "drivers": S.drivers(t, top=3)})
            out["horizons"].append(r)
        return out

    async def read(self, coin: str) -> dict:
        got = self.tables(coin)
        if got is None:
            return {"coin": coin, "missing": True, "coins": self.coins()}
        if not self.live_ok:
            return {"coin": coin, "offline": True, "coins": self.coins()}
        data = await self.live(coin)
        out = await asyncio.to_thread(self.compute, coin, got[0], got[1], data)
        out["coins"] = self.coins()
        return out

    async def hold(self, coin: str, side: int, dist_bps: float) -> list | None:
        """For the Hold Clock: per hold time, how often (like now, over the years) price went dist_bps against the
        position, as a survival share, with the plain rate and the cases behind it."""
        got = self.tables(coin)
        if got is None or not self.live_ok:
            return None
        tables, meta = got
        hit = self._live.get(coin)
        if not hit or time.time() - hit[0] >= LIVE_S:              # the hold clock never waits on Binance
            if coin not in self._pending:
                self._pending.add(coin)
                asyncio.get_running_loop().create_task(self._refresh(coin))
            if not hit or time.time() - hit[0] > STALE_S:
                return None
        try:
            return await asyncio.to_thread(self._hold, tables, meta, hit[1], side, dist_bps)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("sentiment: hold read failed (%s)", exc)
            return None

    async def _refresh(self, coin: str) -> None:
        try:
            await self.live(coin)
        except Exception as exc:                                   # noqa: BLE001
            log.info("sentiment: no live read for %s (%s)", coin, exc)
        finally:
            self._pending.discard(coin)

    def _hold(self, tables, meta, data, side, dist_bps) -> list:
        rel = RL.times_s([t for (t, _), imp in CAL.LIVE.items() if imp == "High"])
        oi, fund = data.get("oi"), data.get("funding")
        fams = {"long": self._now(meta.get("long"), data.get("m1"), 60, False, oi, fund),
                "short": self._now(meta.get("short"), data.get("s1"), 1, True, oi, fund)}
        out = []
        for sec, h in HOLD.items():
            p, t = fams["short" if h in S.SHORT else "long"], tables.get(h)
            if p is None or t is None:
                out.append({"horizon": sec, "survive": None})
                continue
            i = len(p.s) - 1
            b = S.bins_at(p, [i], sec, rel)[0]
            c = S.chance_past(t, b, dist_bps, S.scale_at(p, i, sec), side)
            if c is None or not np.isfinite(c["chance"]):
                out.append({"horizon": sec, "survive": None})
                continue
            r = S.read(t, b, S.scale_at(p, i, sec))
            lean = r["held_up"].get("held") and r["p_up"] is not None
            out.append({"horizon": sec, "survive": 1 - c["chance"], "base": 1 - c["base"], "n": c["n"],
                        "kept": c["kept"], "p_up": r["p_up"] if lean else None, "base_p_up": r["base_p_up"]})
        return out


def _label(f: str, b) -> str:
    v = int(b[S.FEATURES.index(f)])
    return S.LABELS[f][v] if v < S.NBINS[f] else "unknown"

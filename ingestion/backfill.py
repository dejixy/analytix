"""
Backfills price history for the long windows from Hyperliquid's REST API.

    candleSnapshot   {"type": "candleSnapshot", "req": {"coin", "interval", "startTime", "endTime"}}
                     → [{"t": open ms, "T": close ms, "o","h","l","c": str, "v": base volume str, "n": trades}]
                     Only the most recent 5000 candles are served, so 5m candles reach back ~17 days.
    fundingHistory   {"type": "fundingHistory", "coin", "startTime"} → [{"fundingRate": str, "time": ms}]

There is no public history of trades by side, book depth or open interest, so
backfilled bars carry price, volume and funding only (source="candle").
"""
import logging
import time
from typing import Any

import httpx

from config import BACKFILL_INTERVAL, HL_INFO_URL
from models.barModel import Bar

log = logging.getLogger("analytix.backfill")

_INTERVAL_S = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def candles_to_bars(candles: list[dict[str, Any]], funding: list[dict[str, Any]] | None = None) -> list[Bar]:
    """Convert raw candles (+ hourly funding) into price-only bars, oldest first."""
    rates = sorted((int(f["time"]), float(f["fundingRate"])) for f in (funding or []))
    bars: list[Bar] = []
    j, current = 0, None
    for c in sorted(candles, key=lambda c: int(c["t"])):
        t = int(c["t"])
        while j < len(rates) and rates[j][0] <= t:
            current = rates[j][1]
            j += 1
        span = _INTERVAL_S.get(str(c.get("i", BACKFILL_INTERVAL)), (int(c["T"]) - t + 1) // 1000)
        close = float(c["c"])
        bars.append(Bar(
            timestamp=t, span_s=max(60, span), open=float(c["o"]), high=float(c["h"]), low=float(c["l"]),
            close=close, source="candle", volume_usd=float(c.get("v", 0) or 0) * close, funding=current,
        ))
    return bars


async def backfill(coin: str, seconds: int, url: str = HL_INFO_URL, interval: str = BACKFILL_INTERVAL) -> list[Bar]:
    end = int(time.time() * 1000)
    start = end - seconds * 1000
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"type": "candleSnapshot",
                                         "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}})
        r.raise_for_status()
        candles = r.json()
        funding: list[dict[str, Any]] = []
        try:
            f = await client.post(url, json={"type": "fundingHistory", "coin": coin, "startTime": start - 3600_000})
            f.raise_for_status()
            funding = f.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("funding history for %s unavailable: %s", coin, exc)
    bars = candles_to_bars(candles, funding)
    log.info("backfilled %d %s candles for %s", len(bars), interval, coin)
    return bars


async def fetch_candles(coin: str, interval: str, n: int = 5000, url: str = HL_INFO_URL) -> list[dict[str, Any]]:
    """The most recent `n` candles (Hyperliquid serves up to 5000), raw rows."""
    span = _INTERVAL_S.get(interval, 3600) * 1000
    end = int(time.time() * 1000)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"type": "candleSnapshot",
                                         "req": {"coin": coin, "interval": interval, "startTime": end - n * span,
                                                 "endTime": end}})
        r.raise_for_status()
        return r.json()


async def fetch_max_leverage(url: str = HL_INFO_URL) -> dict[str, float]:
    """Each perp's max leverage from Hyperliquid's `meta` (it sets the maintenance margin: half the initial
    margin at max leverage)."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"type": "meta"})
        r.raise_for_status()
        return {a["name"]: float(a["maxLeverage"]) for a in r.json().get("universe", []) if a.get("maxLeverage")}

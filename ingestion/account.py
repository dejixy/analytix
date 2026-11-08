"""
Read-only wallet data from Hyperliquid's public info API. Anyone's positions and open orders are public
there, so position watch needs only an address: no keys, and no way to trade.

    clearinghouseState   open positions, entry, liquidation price, margin, account value   (weight 2)
    frontendOpenOrders   resting orders, including stop-loss and take-profit triggers       (weight 20)
    metaAndAssetCtxs     every perp's price, index, funding, open interest and 24h volume    (weight 20)

Hyperliquid allows 1200 weight a minute per IP, so a few wallets polled every 20 seconds (orders every
minute) stay far inside it.
"""
import asyncio
import re
from typing import Any

import httpx

from config import HL_INFO_URL

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def valid_address(address: str) -> bool:
    return bool(ADDRESS.match(address or ""))


async def _info(body: dict, url: str = HL_INFO_URL) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json()


async def fetch_account(address: str, url: str = HL_INFO_URL) -> dict:
    return await _info({"type": "clearinghouseState", "user": address}, url)


async def fetch_open_orders(address: str, url: str = HL_INFO_URL) -> list[dict]:
    return await _info({"type": "frontendOpenOrders", "user": address}, url)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


async def fetch_contexts(url: str = HL_INFO_URL) -> dict[str, dict]:
    """Every listed perp in one call: {coin: {"mark", "oracle", "funding" (hourly rate), "oi" (coins),
    "day_volume" (USD, last 24h), "prev_day" (price 24h ago), "max_leverage"}}."""
    meta, ctxs = await _info({"type": "metaAndAssetCtxs"}, url)
    out = {}
    for a, c in zip(meta.get("universe", []), ctxs):
        if a.get("isDelisted") or not a.get("name"):
            continue
        out[a["name"]] = {"mark": _num(c.get("markPx")) or None, "oracle": _num(c.get("oraclePx")) or None,
                          "funding": _num(c.get("funding")) or 0.0, "oi": _num(c.get("openInterest")) or 0.0,
                          "day_volume": _num(c.get("dayNtlVlm")) or 0.0, "prev_day": _num(c.get("prevDayPx")) or None,
                          "max_leverage": _num(a.get("maxLeverage")) or None}
    return out


async def fetch_spot_prices(url: str = HL_INFO_URL) -> dict[str, dict]:
    """Spot markets quoted in USDC, by base token: {"HYPE": {"px", "day_volume" (USD, 24h), "pair"}}; the busiest
    pair when a token has several. (spotMetaAndAssetCtxs, weight 20)"""
    meta, ctxs = await _info({"type": "spotMetaAndAssetCtxs"}, url)
    names = {t.get("index"): t.get("name") for t in meta.get("tokens", [])}
    by_coin = {c.get("coin"): c for c in ctxs if isinstance(c, dict)}
    out: dict[str, dict] = {}
    for u in meta.get("universe", []):
        toks = u.get("tokens") or []
        if len(toks) != 2 or names.get(toks[1]) != "USDC":
            continue
        base, c = names.get(toks[0]), by_coin.get(u.get("name"))
        if not base or not c:
            continue
        px = _num(c.get("midPx")) or _num(c.get("markPx"))
        vol = _num(c.get("dayNtlVlm")) or 0.0
        if px and (base not in out or vol > out[base]["day_volume"]):
            out[base] = {"px": px, "day_volume": vol, "pair": u.get("name")}
    return out


async def fetch_funding_history(coin: str, start_ms: int, end_ms: int | None = None, url: str = HL_INFO_URL,
                                pause_s: float = 0.0) -> list[tuple[int, float]]:
    """[(time ms, hourly rate)] for a perp from start_ms, paging Hyperliquid's 500 rows a reply (weight 20 each;
    pause_s between pages keeps a long history inside the 1200-a-minute budget)."""
    out: list[tuple[int, float]] = []
    t = start_ms
    for _ in range(40):
        body = {"type": "fundingHistory", "coin": coin, "startTime": t}
        if end_ms:
            body["endTime"] = end_ms
        rows = await _info(body, url) or []
        got = [(int(x["time"]), float(x["fundingRate"])) for x in rows if x.get("time") is not None]
        if not got:
            break
        out += got
        last = max(r[0] for r in got)
        if last + 1 <= t or len(got) < 500:
            break
        t = last + 1
        if pause_s:
            await asyncio.sleep(pause_s)
    return sorted(dict(out).items())


async def fetch_market(url: str = HL_INFO_URL) -> dict[str, dict]:
    """{coin: {"mark", "funding" (hourly rate), "max_leverage"}} for every perp."""
    return {k: {"mark": v["mark"], "funding": v["funding"], "max_leverage": v["max_leverage"]}
            for k, v in (await fetch_contexts(url)).items()}

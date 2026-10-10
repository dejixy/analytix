"""
Read-only wallet data from Hyperliquid's public info API. Anyone's positions and open orders are public
there, so position watch needs only an address: no keys, and no way to trade.

    clearinghouseState   open positions, entry, liquidation price, margin, account value   (weight 2)
    frontendOpenOrders   resting orders, including stop-loss and take-profit triggers       (weight 20)
    metaAndAssetCtxs     every perp's mark price, funding rate and max leverage, one call    (weight 20)

Hyperliquid allows 1200 weight a minute per IP, so a few wallets polled every 20 seconds (orders every
minute) stay far inside it.
"""
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


async def fetch_market(url: str = HL_INFO_URL) -> dict[str, dict]:
    """{coin: {"mark", "funding" (hourly rate), "max_leverage"}} for every perp."""
    meta, ctxs = await _info({"type": "metaAndAssetCtxs"}, url)
    out = {}
    for a, c in zip(meta.get("universe", []), ctxs):
        try:
            out[a["name"]] = {"mark": float(c.get("markPx") or 0) or None, "funding": float(c.get("funding") or 0),
                              "max_leverage": float(a.get("maxLeverage") or 0) or None}
        except (TypeError, ValueError):
            continue
    return out

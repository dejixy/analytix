"""
Read-only data for Hyperliquid's prediction markets (HIP-4 outcome markets), from the public info API.

    outcomeMeta          every live market and question, with who listed it (venue)        one call a minute
    l2Book               one market's book, "#<10 * outcome + side>"                       weight 2
    recentTrades         one market's latest trades, with both wallets                     weight 20
    candleSnapshot       one market's odds history (same call as for perps)                weight 20+
    metaAndAssetCtxs     with "dex": prices of a builder's perps (trade.xyz's "xyz:SILVER")   weight 20

Live odds, volume and open contracts come from the WebSocket instead (l2Book and activeSpotAssetCtx per
market): see api/outcomes.py.
"""
from typing import Any

from config import HL_INFO_URL
from ingestion.account import _info, _num


async def fetch_outcome_meta(url: str = HL_INFO_URL) -> dict:
    return await _info({"type": "outcomeMeta"}, url)


async def fetch_book(coin: str, url: str = HL_INFO_URL) -> dict:
    return await _info({"type": "l2Book", "coin": coin}, url)


async def fetch_trades(coin: str, url: str = HL_INFO_URL) -> list[dict[str, Any]]:
    return await _info({"type": "recentTrades", "coin": coin}, url)


async def fetch_dex_marks(dex: str, url: str = HL_INFO_URL) -> dict[str, float]:
    """{"xyz:SILVER": mark, ...} for one builder-deployed perp dex."""
    meta, ctxs = await _info({"type": "metaAndAssetCtxs", "dex": dex}, url)
    out = {}
    for a, c in zip(meta.get("universe", []), ctxs):
        name = a.get("name")
        mark = _num(c.get("markPx"))
        if name and mark:
            out[name if ":" in name else f"{dex}:{name}"] = mark
    return out


def trade_row(raw: dict) -> dict | None:
    """A raw outcome trade → {"t", "px", "sz", "side", "buyer", "seller", "tid"}. `users` is [buyer, seller]."""
    px, sz = _num(raw.get("px")), _num(raw.get("sz"))
    if px is None or sz is None or "time" not in raw:
        return None
    users = raw.get("users") or [None, None]
    buyer, seller = (users + [None, None])[:2]
    return {"t": int(raw["time"]), "px": px, "sz": sz, "side": raw.get("side"), "tid": raw.get("tid"),
            "buyer": buyer.lower() if isinstance(buyer, str) else None,
            "seller": seller.lower() if isinstance(seller, str) else None}

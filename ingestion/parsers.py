"""
Raw Hyperliquid WebSocket messages → typed models.

Live, replayed and synthetic sessions all pass through these functions, so a
recorded session exercises exactly the same code path as the live feed.

Wire formats (https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions):
  trades          {"channel": "trades", "data": [WsTrade, ...]}
  l2Book          {"channel": "l2Book", "data": {coin, time, levels: [bids, asks]}}
  activeAssetCtx  {"channel": "activeAssetCtx", "data": {coin, ctx: {funding, openInterest, ...}}}
Numbers arrive as strings ("3012.5"); float() handles both.
"""
import sys
from typing import Any

from config import LIQUIDATOR_ADDRESSES
from models.bookModel import BookLevel, OrderBook
from models.contextModel import AssetContext
from models.tradeModel import Trade, TradeSide
from state import Event


def _wallet(addr: Any) -> str | None:
    # Interned: an hour of trades repeats the same few thousand addresses, so they share one string each.
    return sys.intern(addr.lower()) if isinstance(addr, str) and addr else None


def parse_trade(raw: dict[str, Any]) -> Trade:
    # `users` is [buyer, seller]; `side` is the aggressor's ("B" bought from the ask, "A" sold into the bid),
    # so the taker is users[0] on "B" and users[1] on "A". The taker is always kept (who is aggressive is
    # the point); both wallets only when liquidator matching needs them.
    users = raw.get("users") or [None, None]
    side = TradeSide(raw["side"])
    taker = _wallet(users[0] if side is TradeSide.BUY else users[1]) if len(users) == 2 else None
    both = LIQUIDATOR_ADDRESSES and len(users) == 2
    return Trade(
        timestamp=int(raw["time"]),
        price=float(raw["px"]),
        size=float(raw["sz"]),
        side=side,
        tid=int(raw.get("tid", 0)),
        hash=str(raw.get("hash", "")),
        buyer=_wallet(users[0]) if both else None,
        seller=_wallet(users[1]) if both else None,
        taker=taker,
    )


def _levels(raw_levels: list[dict[str, Any]]) -> tuple[BookLevel, ...]:
    return tuple(
        BookLevel(price=float(l["px"]), size=float(l["sz"]), orders=int(l.get("n", 0)))
        for l in raw_levels
    )


def parse_book(raw: dict[str, Any]) -> OrderBook:
    bids, asks = raw["levels"]
    return OrderBook(timestamp=int(raw["time"]), bids=_levels(bids), asks=_levels(asks))


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_asset_ctx(raw: dict[str, Any]) -> AssetContext:
    ctx = raw["ctx"]
    mid = ctx.get("midPx")
    return AssetContext(
        timestamp=0,  # stamped by MarketState
        funding=_f(ctx.get("funding")),
        open_interest=_f(ctx.get("openInterest")),
        mark_price=_f(ctx.get("markPx")),
        oracle_price=_f(ctx.get("oraclePx")),
        mid_price=_f(mid) if mid is not None else None,
        day_volume=_f(ctx.get("dayNtlVlm")),
        prev_day_price=_f(ctx.get("prevDayPx")),
    )


def parse_message(msg: dict[str, Any], coin: str) -> list[Event]:
    """Returns zero or more events. Unknown channels (pong, subscriptionResponse) → []."""
    channel = msg.get("channel")
    data = msg.get("data")
    if data is None:
        return []
    if channel == "trades":
        return [parse_trade(t) for t in data if t.get("coin") == coin]
    if channel == "l2Book" and data.get("coin") == coin:
        return [parse_book(data)]
    if channel == "activeAssetCtx" and data.get("coin") == coin and "ctx" in data:
        return [parse_asset_ctx(data)]
    return []


def coin_of(msg: dict[str, Any]) -> str | None:
    """Which coin a raw message belongs to, so the runtime can route it to one pipeline."""
    data = msg.get("data")
    if isinstance(data, list):
        return data[0].get("coin") if data and isinstance(data[0], dict) else None
    if isinstance(data, dict):
        return data.get("coin")
    return None


def subscriptions(coin: str) -> list[dict[str, Any]]:
    return [
        {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}},
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": coin}},
    ]

"""
A one-off, read-only check of Hyperliquid's HIP-4 outcome markets (prediction markets), to see exactly what
the live API returns before building an Outcomes view on it. It places no orders and needs no keys.

    python -m scripts.probeOutcomes            # prints a short report and saves the raw replies to a file

What it asks Hyperliquid:
  1. outcomeMeta: every listed outcome market and question (and whether it says who deployed it)
  2. the order books of a few markets, Yes and No sides (#<10*outcome+side>)
  3. recent trades on those books (do they carry wallets, like perp trades?)
  4. price history (candleSnapshot) for one side
  5. spotMetaAndAssetCtxs: whether outcome tokens show up with volume or supply there
  6. 20 seconds of the live WebSocket for one market: trades, book and anything else it sends
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

from config import HL_INFO_URL, HL_WS_URL

OUT = Path("/tmp/analytix-outcome-probe.json")
raw: dict = {}


def show(title: str) -> None:
    print(f"\n=== {title} ===")


def short(x, n: int = 600) -> str:
    s = json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + f"… ({len(s)} chars)"


def keys_of(rows) -> list:
    seen = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            for k in r:
                seen[k] = True
    return list(seen)


async def info(client: httpx.AsyncClient, body: dict):
    try:
        r = await client.post(HL_INFO_URL, json=body, timeout=20)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def main() -> None:
    async with httpx.AsyncClient() as client:
        # 1. the market list
        show("1. outcomeMeta (the market list)")
        status, meta = await info(client, {"type": "outcomeMeta"})
        raw["outcomeMeta"] = meta
        if status != 200 or not isinstance(meta, dict):
            print("failed:", status, short(meta))
            return
        outcomes, questions = meta.get("outcomes") or [], meta.get("questions") or []
        print(f"top-level keys: {list(meta)}")
        print(f"{len(outcomes)} outcomes, {len(questions)} questions")
        print(f"outcome fields: {keys_of(outcomes)}")
        print(f"question fields: {keys_of(questions)}")
        classes = {}
        for o in outcomes:
            d = dict(p.split(":", 1) for p in str(o.get("description", "")).split("|") if ":" in p)
            classes[d.get("class", "?")] = classes.get(d.get("class", "?"), 0) + 1
        print(f"market types (class): {classes}")
        names = {}
        for o in outcomes:
            names[o.get("name", "?")] = names.get(o.get("name", "?"), 0) + 1
        print(f"market names (most common): {sorted(names.items(), key=lambda kv: -kv[1])[:12]}")
        for o in (outcomes[:2] + outcomes[-2:] if len(outcomes) > 4 else outcomes):
            print("  outcome:", short(o, 400))
        for q in questions[:3]:
            print("  question:", short(q, 500))
        deployer_like = [k for k in keys_of(outcomes) + keys_of(questions)
                         if any(w in k.lower() for w in ("deploy", "builder", "owner", "creator", "dex"))]
        print(f"fields that might name the deployer: {deployer_like or 'none'}")

        # 2 + 3. books and trades for a few markets
        sample = outcomes[:2] + outcomes[-2:] if len(outcomes) > 4 else outcomes   # oldest and newest markets
        picks = list(dict.fromkeys(o["outcome"] for o in sample if isinstance(o.get("outcome"), int)))
        live_coin = None
        best_volume = -1
        for oid in picks:
            for side in (0, 1):
                coin = f"#{10 * oid + side}"
                show(f"2. order book {coin} (outcome {oid}, {'Yes' if side == 0 else 'No'})")
                status, book = await info(client, {"type": "l2Book", "coin": coin})
                raw[f"l2Book {coin}"] = book
                if isinstance(book, dict):
                    lv = book.get("levels") or [[], []]
                    print(f"keys: {list(book)} · bids {len(lv[0])} levels, asks {len(lv[1]) if len(lv) > 1 else 0}")
                    print("  best bid:", short(lv[0][:1]), "· best ask:", short(lv[1][:1] if len(lv) > 1 else []))
                else:
                    print("failed:", status, short(book))
                show(f"3. recent trades {coin}")
                status, trades = await info(client, {"type": "recentTrades", "coin": coin})
                raw[f"recentTrades {coin}"] = trades
                if isinstance(trades, list):
                    print(f"{len(trades)} trades · fields: {keys_of(trades)}")
                    for t in trades[:2]:
                        print("  ", short(t, 400))
                    vol = sum(float(t.get("px", 0)) * float(t.get("sz", 0)) for t in trades if isinstance(t, dict))
                    if vol > best_volume:
                        best_volume, live_coin = vol, coin
                else:
                    print("failed:", status, short(trades))

        # 4. price history
        if live_coin:
            show(f"4. candleSnapshot {live_coin} (1h, last 3 days)")
            now = int(time.time() * 1000)
            status, candles = await info(client, {"type": "candleSnapshot", "req": {
                "coin": live_coin, "interval": "1h", "startTime": now - 3 * 86_400_000, "endTime": now}})
            raw[f"candles {live_coin}"] = candles
            if isinstance(candles, list):
                print(f"{len(candles)} candles · fields: {keys_of(candles)}")
                print("  last:", short(candles[-1:], 300))
            else:
                print("failed:", status, short(candles))

        # 5. do outcome tokens show up in the spot listing (volume, supply)?
        show("5. spotMetaAndAssetCtxs: outcome tokens in the spot listing?")
        status, spot = await info(client, {"type": "spotMetaAndAssetCtxs"})
        if isinstance(spot, list) and len(spot) == 2 and isinstance(spot[0], dict):
            tokens = spot[0].get("tokens") or []
            universe = spot[0].get("universe") or []
            hits_t = [t for t in tokens if str(t.get("name", "")).startswith(("+", "#"))]
            hits_u = [(u, c) for u, c in zip(universe, spot[1]) if str(u.get("name", "")).startswith(("+", "#"))]
            raw["spot outcome tokens"] = hits_t[:10]
            raw["spot outcome pairs"] = hits_u[:10]
            print(f"{len(tokens)} spot tokens, {len(hits_t)} look like outcome tokens; "
                  f"{len(universe)} spot pairs, {len(hits_u)} look like outcome pairs")
            for t in hits_t[:2]:
                print("  token:", short(t, 300))
            for u, c in hits_u[:2]:
                print("  pair:", short(u, 200), "ctx:", short(c, 300))
        else:
            print("failed or unexpected:", status, short(spot, 200))

    # 6. the live feed
    if live_coin:
        show(f"6. WebSocket for {live_coin}, 20 seconds")
        subs = [{"type": "trades", "coin": live_coin}, {"type": "l2Book", "coin": live_coin},
                {"type": "bbo", "coin": live_coin}, {"type": "activeAssetCtx", "coin": live_coin}]
        seen: dict = {}
        try:
            async with websockets.connect(HL_WS_URL, open_timeout=10, max_size=2**22) as ws:
                for s in subs:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                end = time.time() + 20
                while time.time() < end:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    ch = msg.get("channel", "?")
                    seen.setdefault(ch, []).append(msg)
        except Exception as exc:
            print("websocket failed:", f"{type(exc).__name__}: {exc}")
        raw["websocket"] = {ch: msgs[:5] for ch, msgs in seen.items()}
        for ch, msgs in seen.items():
            print(f"channel {ch}: {len(msgs)} messages")
            print("  first:", short(msgs[0], 500))

    OUT.write_text(json.dumps(raw, indent=1, default=str))
    print(f"\nRaw replies saved to {OUT}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

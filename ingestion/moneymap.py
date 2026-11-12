"""
Where money is being made in crypto, from public sources (for the Analyzer page, api/analyzer.py):

    DefiLlama      fees users paid to every protocol over 24 h, 7 d and 30 d, with its category and chains (free,
                   no key): where the activity is
    Hyperliquid    the perps leaderboard: every trader's profit, return and volume by day, week, month and all time
                   (free, no key)
    Birdeye        Solana's top traders by profit (ANALYTIX_BIRDEYE_KEY, a free key from birdeye.so)
    Polymarket     the prediction-market profit leaderboard (free, no key)
    Dune           any saved query that returns wallets with a profit column (ANALYTIX_DUNE_KEY, a free key from
                   dune.com; the queries are listed in data/analyzer_dune.json), for NFTs, arbitrage, other chains

Every parser takes the raw reply and is forgiving about shapes: scripts/analyzerProbe.py prints what each source
really returns on the server.
"""
import os

import httpx

LLAMA_FEES = "https://api.llama.fi/overview/fees"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BIRDEYE_TRADERS = "https://public-api.birdeye.so/trader/gainers-losers"
POLYMARKET_PROFIT = "https://lb-api.polymarket.com/profit"
DUNE_RESULTS = "https://api.dune.com/api/v1/query/{qid}/results"
UA = {"User-Agent": "Analytix analyzer"}
WINDOWS = ("day", "week", "month", "allTime")


def num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


# ── DefiLlama ────────────────────────────────────────────────────────────────
def parse_llama(payload: dict) -> list[dict]:
    """[{name, category, chains, d1, d7, d30, by_chain {chain: 24 h fees}}] for protocols with any fees."""
    out = []
    for p in (payload or {}).get("protocols") or []:
        d1, d7, d30 = num(p.get("total24h")), num(p.get("total7d")), num(p.get("total30d"))
        if not (d1 or d7 or d30):
            continue
        chains = [c for c in p.get("chains") or [] if isinstance(c, str)]
        proper = {c.lower(): c for c in chains}
        by = {}
        for chain, v in (p.get("breakdown24h") or {}).items():
            total = sum(x for x in (num(y) for y in v.values()) if x) if isinstance(v, dict) else num(v)
            if total:
                by[proper.get(str(chain).lower(), str(chain).title())] = total
        if not by and len(chains) == 1 and d1:
            by = {chains[0]: d1}
        out.append({"name": p.get("displayName") or p.get("name") or "?", "category": p.get("category") or "",
                    "chains": chains, "d1": d1 or 0.0, "d7": d7 or 0.0, "d30": d30 or 0.0, "by_chain": by})
    return out


async def fetch_llama() -> list[dict]:
    async with httpx.AsyncClient(timeout=60, headers=UA) as c:
        r = await c.get(LLAMA_FEES, params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"})
        r.raise_for_status()
        return parse_llama(r.json())


# ── Hyperliquid's leaderboard ────────────────────────────────────────────────
def parse_hl_leaderboard(payload: dict) -> list[dict]:
    """[{wallet, name, value, day/week/month/allTime: {pnl, roi, vlm}}]."""
    out = []
    for r in (payload or {}).get("leaderboardRows") or []:
        perf = {}
        for item in r.get("windowPerformances") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
                perf[item[0]] = {"pnl": num(item[1].get("pnl")) or 0.0, "roi": num(item[1].get("roi")) or 0.0,
                                 "vlm": num(item[1].get("vlm")) or 0.0}
        if r.get("ethAddress") and perf:
            out.append({"wallet": r["ethAddress"], "name": r.get("displayName") or "", "value": num(r.get("accountValue")) or 0.0,
                        **perf})
    return out


async def fetch_hl_leaderboard() -> list[dict]:
    async with httpx.AsyncClient(timeout=120, headers=UA) as c:
        r = await c.get(HL_LEADERBOARD)
        r.raise_for_status()
        return parse_hl_leaderboard(r.json())


# ── Birdeye (Solana traders) ─────────────────────────────────────────────────
def parse_birdeye(payload: dict) -> list[dict]:
    items = ((payload or {}).get("data") or {}).get("items") or []
    out = []
    for x in items:
        pnl = num(x.get("pnl"))
        if x.get("address") and pnl is not None:
            out.append({"wallet": x["address"], "pnl": pnl, "vlm": num(x.get("volume")) or 0.0,
                        "trades": int(num(x.get("trade_count")) or 0)})
    return out


async def fetch_birdeye(window: str = "1W", key: str | None = None, limit: int = 10, pages: int = 3) -> list[dict]:
    key = key or os.getenv("ANALYTIX_BIRDEYE_KEY", "")
    if not key:
        raise RuntimeError("no key: set ANALYTIX_BIRDEYE_KEY (free at birdeye.so)")
    out = []
    async with httpx.AsyncClient(timeout=30, headers={**UA, "X-API-KEY": key, "x-chain": "solana", "accept": "application/json"}) as c:
        for page in range(pages):
            r = await c.get(BIRDEYE_TRADERS, params={"type": window, "sort_by": "PnL", "sort_type": "desc",
                                                    "offset": page * limit, "limit": limit})
            r.raise_for_status()
            got = parse_birdeye(r.json())
            out += got
            if len(got) < limit:
                break
    return out


# ── Polymarket ───────────────────────────────────────────────────────────────
def parse_polymarket(payload) -> list[dict]:
    rows = payload if isinstance(payload, list) else (payload or {}).get("data") or []
    out = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        pnl = num(x.get("amount") if x.get("amount") is not None else x.get("pnl"))
        wallet = x.get("proxyWallet") or x.get("address") or x.get("user")
        if wallet and pnl is not None:
            out.append({"wallet": wallet, "name": x.get("name") or x.get("pseudonym") or "", "pnl": pnl})
    return out


async def fetch_polymarket(window: str = "7d", limit: int = 50) -> list[dict]:
    async with httpx.AsyncClient(timeout=30, headers=UA) as c:
        r = await c.get(POLYMARKET_PROFIT, params={"window": window, "limit": limit})
        r.raise_for_status()
        return parse_polymarket(r.json())


# ── Dune ─────────────────────────────────────────────────────────────────────
WALLET_COLS = ("wallet", "address", "trader", "user", "account", "taker", "maker", "searcher", "bot")
PNL_COLS = ("pnl_usd", "profit_usd", "pnl", "profit", "realized_pnl_usd", "realized_pnl", "revenue_usd", "profit_usd_7d")


def parse_dune(payload: dict) -> list[dict]:
    """A saved query's latest rows as [{wallet, pnl, name}], finding the wallet and profit columns by their names."""
    rows = (((payload or {}).get("result") or {}).get("rows")) or []
    if not rows:
        return []
    keys = {k.lower(): k for k in rows[0]}
    w = next((keys[k] for k in WALLET_COLS if k in keys), None)
    p = next((keys[k] for k in PNL_COLS if k in keys), None)
    if not w or not p:
        raise ValueError(f"no wallet/profit column in {sorted(keys)}")
    label = next((keys[k] for k in ("label", "name", "ens") if k in keys), None)
    out = []
    for r in rows:
        pnl = num(r.get(p))
        if r.get(w) and pnl is not None:
            out.append({"wallet": str(r[w]), "pnl": pnl, "name": str(r.get(label) or "") if label else ""})
    return sorted(out, key=lambda x: -x["pnl"])


async def fetch_dune(query_id: int | str, key: str | None = None, limit: int = 200) -> list[dict]:
    key = key or os.getenv("ANALYTIX_DUNE_KEY", "")
    if not key:
        raise RuntimeError("no key: set ANALYTIX_DUNE_KEY (free at dune.com)")
    async with httpx.AsyncClient(timeout=60, headers={**UA, "X-Dune-API-Key": key}) as c:
        r = await c.get(DUNE_RESULTS.format(qid=query_id), params={"limit": limit})
        r.raise_for_status()
        return parse_dune(r.json())

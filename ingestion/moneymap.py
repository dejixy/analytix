"""
Where money is being made in crypto, from public sources (for the Analyzer page, api/analyzer.py):

    DefiLlama      fees users paid to every protocol over 24 h, 7 d and 30 d, the revenue the protocol kept, and
                   trading volume (spot, perps, options), with its category and chains (free, no key); and one
                   protocol's daily history
    GeckoTerminal  a chain's busiest pools by 24 h volume: what is actually trading there (free, no key)
    Hyperliquid    the perps leaderboard: every trader's profit, return and volume by day, week, month and all time
                   (free, no key)
    Birdeye        Solana's top traders by profit (ANALYTIX_BIRDEYE_KEY, a free key from birdeye.so)
    Polymarket     the prediction-market profit leaderboard (free, no key)
    Dune           any saved query that returns wallets with a profit column (ANALYTIX_DUNE_KEY, a free key from
                   dune.com; the queries are listed in data/analyzer_dune.json), for NFTs, arbitrage, other chains

Every parser takes the raw reply and is forgiving about shapes: scripts/analyzerProbe.py prints what each source
really returns on the server.
"""
import asyncio
import os

import httpx

LLAMA_FEES = "https://api.llama.fi/overview/fees"
LLAMA_VOLUME = ("https://api.llama.fi/overview/dexs", "https://api.llama.fi/overview/derivatives",
                "https://api.llama.fi/overview/options")
LLAMA_SUMMARY = "https://api.llama.fi/summary/{kind}/{slug}"
LLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
LLAMA_STABLE_CHART = "https://stablecoins.llama.fi/stablecoincharts/{chain}"
EXTRA_TYPES = {"revenue": "dailyRevenue", "holders": "dailyHoldersRevenue"}   # (earnings after incentives isn't served)
GECKO_POOLS = "https://api.geckoterminal.com/api/v2/networks/{network}/pools"
GECKO_NETWORK = {"Solana": "solana", "TON": "ton", "Base": "base", "Ethereum": "eth", "BSC": "bsc", "Avalanche": "avax",
                 "Arbitrum": "arbitrum", "Polygon": "polygon_pos", "Tron": "tron", "Sui": "sui-network", "Aptos": "aptos",
                 "Hyperliquid": "hyperevm", "Hyperliquid L1": "hyperevm", "OP Mainnet": "optimism", "Optimism": "optimism",
                 "Unichain": "unichain", "Sonic": "sonic", "Berachain": "berachain", "Monad": "monad", "Plasma": "plasma",
                 "Linea": "linea", "Mantle": "mantle", "Sei": "sei-evm", "Ronin": "ronin", "Abstract": "abstract",
                 "Starknet": "starknet-alpha", "zkSync Era": "zksync", "Blast": "blast", "Scroll": "scroll"}
METRICS = ("fees", "revenue", "volume")
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BIRDEYE_TRADERS = "https://public-api.birdeye.so/trader/gainers-losers"
POLYMARKET_PROFIT = "https://data-api.polymarket.com/v2/leaderboard"     # lb-api.polymarket.com was retired
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
        d1, d7, d30 = (max(0.0, num(p.get(k)) or 0.0) for k in ("total24h", "total7d", "total30d"))   # no negative fees
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
        name = p.get("displayName") or p.get("name") or "?"
        out.append({"name": name, "category": p.get("category") or "", "slug": p.get("slug") or slug_of(p.get("name") or name),
                    "parent": p.get("parentProtocol") or "", "id": str(p.get("defillamaId") or p.get("id") or ""),
                    "chains": chains, "d1": d1 or 0.0, "d7": d7 or 0.0, "d30": d30 or 0.0, "by_chain": by})
    return out


def slug_of(name: str) -> str:
    return "-".join(str(name).lower().split())


_LEAN = {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}


async def fetch_llama() -> dict[str, list[dict]]:
    """{"fees": [...], "revenue": [...], "volume": [...]}: the same protocol rows measured three ways. Volume joins
    spot, perps and options venues; a part that can't be read is left empty rather than failing the rest."""
    out: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=60, headers=UA) as c:
        r = await c.get(LLAMA_FEES, params=_LEAN)
        r.raise_for_status()
        out["fees"] = parse_llama(r.json())
        for key, data_type in EXTRA_TYPES.items():      # revenue; earnings (after token incentives); what holders get
            try:
                r = await c.get(LLAMA_FEES, params={**_LEAN, "dataType": data_type})
                out[key] = parse_llama(r.json()) if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                out[key] = []
        vol: dict[str, dict] = {}
        for url in LLAMA_VOLUME:
            try:
                r = await c.get(url, params=_LEAN)
                for p in parse_llama(r.json()) if r.status_code == 200 else []:
                    vol.setdefault(p["name"], p)
            except (httpx.HTTPError, ValueError):
                continue
        out["volume"] = list(vol.values())
    return out


def parse_protocols(payload) -> dict:
    """Each protocol's TVL, market cap and token from DefiLlama's protocol list, by name, by slug and by parent
    (a parent's TVL is its versions' sum; its market cap is the token's, the same on every version)."""
    out: dict[str, dict] = {}
    for p in payload if isinstance(payload, list) else []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        row = {"tvl": num(p.get("tvl")) or 0.0, "mcap": num(p.get("mcap")), "symbol": p.get("symbol") if p.get("symbol") not in (None, "-") else ""}
        out[p["name"]] = row
        if p.get("id") is not None:
            out.setdefault("id:" + str(p["id"]), row)
        if p.get("slug"):
            out.setdefault("slug:" + p["slug"], row)
        par = p.get("parentProtocol")
        if par:
            agg = out.setdefault("parent:" + par, {"tvl": 0.0, "mcap": None, "symbol": ""})
            agg["tvl"] += row["tvl"]
            agg["mcap"] = max(agg["mcap"] or 0.0, row["mcap"] or 0.0) or None
            agg["symbol"] = agg["symbol"] or row["symbol"]
    return out


async def fetch_protocols() -> dict:
    async with httpx.AsyncClient(timeout=90, headers=UA) as c:
        r = await c.get(LLAMA_PROTOCOLS)
        r.raise_for_status()
        return parse_protocols(r.json())


def parse_stable_chart(payload) -> dict | None:
    """A chain's dollar stablecoin supply now and its change over 7 and 30 days."""
    rows = []
    for x in payload if isinstance(payload, list) else []:
        v = num(((x or {}).get("totalCirculatingUSD") or {}).get("peggedUSD"))
        if v is not None:
            rows.append(v)
    if len(rows) < 2:
        return None
    now = rows[-1]
    back = lambda n: rows[-1 - n] if len(rows) > n else None
    chg = lambda old: (now - old) if old is not None else None
    return {"supply": now, "d7": chg(back(7)), "d30": chg(back(30)),
            "d7_pct": (now / back(7) - 1) if back(7) else None, "d30_pct": (now / back(30) - 1) if back(30) else None}


async def fetch_stables(chains: list[str]) -> dict[str, dict]:
    out = {}
    async with httpx.AsyncClient(timeout=40, headers=UA) as c:
        for chain in chains:
            try:
                r = await c.get(LLAMA_STABLE_CHART.format(chain=chain))
                got = parse_stable_chart(r.json()) if r.status_code == 200 else None
                if got:
                    out[chain] = got
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.4)
    return out


def parse_history(payload: dict, days: int = 90) -> list[list[float]]:
    """A protocol's daily values, [[ms, value]], the last `days`."""
    rows = []
    for x in (payload or {}).get("totalDataChart") or []:
        if isinstance(x, (list, tuple)) and len(x) >= 2 and num(x[0]) is not None and num(x[1]) is not None:
            rows.append([int(num(x[0])) * 1000, num(x[1])])
    return rows[-days:]


async def fetch_history(slug: str, metric: str = "fees") -> list[list[float]]:
    """One protocol's daily fees, revenue or volume for the last 90 days."""
    tries = [("fees", {"dataType": "dailyRevenue"})] if metric == "revenue" else \
        [("dexs", {}), ("derivatives", {}), ("options", {})] if metric == "volume" else [("fees", {})]
    async with httpx.AsyncClient(timeout=40, headers=UA) as c:
        for kind, params in tries:
            r = await c.get(LLAMA_SUMMARY.format(kind=kind, slug=slug), params=params)
            if r.status_code == 200:
                rows = parse_history(r.json())
                if rows:
                    return rows
    return []


# ── GeckoTerminal: what is trading on a chain ────────────────────────────────
def parse_pools(payload: dict) -> list[dict]:
    out = []
    for x in (payload or {}).get("data") or []:
        a = x.get("attributes") or {}
        vol = num((a.get("volume_usd") or {}).get("h24"))
        if not a.get("name") or not vol:
            continue
        tx = (a.get("transactions") or {}).get("h24") or {}
        dex = (((x.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or ""
        out.append({"name": a["name"], "dex": dex.replace("-", " ").replace("_", " "), "address": a.get("address") or "",
                    "vol": vol, "chg": num((a.get("price_change_percentage") or {}).get("h24")),
                    "trades": int((num(tx.get("buys")) or 0) + (num(tx.get("sells")) or 0)),
                    "buyers": int(num(tx.get("buyers")) or 0), "liquidity": num(a.get("reserve_in_usd")),
                    "fdv": num(a.get("fdv_usd"))})
    return sorted(out, key=lambda p: -p["vol"])


async def fetch_pools(chain: str) -> tuple[str, list[dict]]:
    """(network id, a chain's 20 busiest pools by 24 h volume)."""
    network = GECKO_NETWORK.get(chain) or slug_of(chain)
    async with httpx.AsyncClient(timeout=30, headers={**UA, "accept": "application/json"}) as c:
        r = await c.get(GECKO_POOLS.format(network=network), params={"page": 1, "sort": "h24_volume_usd_desc"})
        r.raise_for_status()
        return network, parse_pools(r.json())


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
        pnl = next((num(x[k]) for k in ("amount", "pnl", "profit", "realizedPnl") if x.get(k) is not None), None)
        wallet = x.get("proxyWallet") or x.get("address") or x.get("user") or x.get("wallet")
        if wallet and pnl is not None:
            out.append({"wallet": wallet, "name": x.get("name") or x.get("userName") or x.get("pseudonym") or "", "pnl": pnl})
    return out


async def fetch_polymarket(window: str = "7d", limit: int = 50) -> list[dict]:
    async with httpx.AsyncClient(timeout=30, headers=UA) as c:
        period = {"1d": "DAY", "7d": "WEEK", "30d": "MONTH"}.get(window, "WEEK")
        r = await c.get(POLYMARKET_PROFIT, params={"timePeriod": period, "orderBy": "PNL", "category": "OVERALL", "limit": limit})
        r.raise_for_status()
        return sorted(parse_polymarket(r.json()), key=lambda x: -x["pnl"])


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

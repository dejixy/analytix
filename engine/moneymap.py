"""
The money map: where crypto's money is being made right now, by category and chain (the Analyzer page). Pure
functions; ingestion/moneymap.py reads the sources and api/analyzer.py keeps them fresh.

    activity    fees users paid to every protocol (DefiLlama), grouped into categories (perps, spot trading, memes
                and launchpads, trading bots, NFTs, lending, staking and yield, prediction markets, MEV, ...) and by
                chain; stablecoins, gas and bridges are kept apart as infrastructure. Fees are what users pay to take part, so they show where the action is and what the venues
                earn; they are not traders' profit
    heat        a category's last 24 h of fees against its own daily average over 30 days:
                    hot      24 h at least HOT x its 30-day daily average, and the 7-day average above it too
                    cooling  24 h at most COOL x its 30-day daily average
    profit      where wallets' profit is public (Hyperliquid perps, Solana traders, Polymarket, Dune queries): the
                biggest winners, and for a whole leaderboard how the crowd did (the share in profit, what winners
                made against what losers lost, how much of the winnings the top 10 took)
"""
CATEGORIES = [
    ("perps", "Perps", ("derivatives",)),
    ("spot", "Spot trading", ("dexs", "dexes", "dex aggregator")),
    ("memes", "Memes and launchpads", ("launchpad", "meme")),
    ("bots", "Trading bots and terminals", ("trading app", "telegram bot")),
    ("nfts", "NFTs", ("nft marketplace", "nft launchpad", "nft lending", "nft")),
    ("prediction", "Prediction markets", ("prediction market",)),
    ("lending", "Lending", ("lending", "cdp")),
    ("yield", "Staking and yield", ("liquid staking", "yield", "restaking", "liquid restaking", "yield aggregator",
                                    "staking pool", "basis trading", "farm", "rwa")),
    ("options", "Options", ("options", "options vault")),
    ("mev", "MEV and arbitrage", ("mev",)),
    ("stables", "Stablecoins", ("stablecoin issuer",)),
    ("bridges", "Bridges", ("bridge", "cross chain bridge", "cross chain")),
    ("chains", "Chains (gas)", ("chain", "rollup")),
]
LABEL = {k: name for k, name, _ in CATEGORIES} | {"other": "Other"}
_BY = {c: k for k, _, cats in CATEGORIES for c in cats}
WATCH_CHAINS = ("Solana", "Hyperliquid L1", "Base", "Ethereum", "Bitcoin", "Avalanche", "Arbitrum", "BSC", "Robinhood")
CHAIN_SHORT = {"Hyperliquid L1": "Hyperliquid", "Hyperliquid": "Hyperliquid"}
INFRA = ("stables", "chains", "bridges")     # what the rails earn, not ways to make money: listed after the rest
HOT, COOL = 1.25, 0.75
MIN_DAILY_USD = 50_000            # a category smaller than this isn't flagged either way


def category_of(llama_category: str) -> str:
    return _BY.get((llama_category or "").strip().lower(), "other")


def heat(d1: float, d7: float, d30: float) -> dict:
    """24 h against the 30-day daily average, with the flag."""
    avg30, avg7 = d30 / 30 if d30 else 0.0, d7 / 7 if d7 else 0.0
    ratio = d1 / avg30 if avg30 else None
    flag = ""
    if ratio is not None and max(d1, avg30) >= MIN_DAILY_USD:
        flag = "hot" if ratio >= HOT and avg7 >= avg30 else "cooling" if ratio <= COOL else ""
    return {"ratio": round(ratio, 2) if ratio is not None else None, "flag": flag, "avg30": avg30, "avg7": avg7}


def money_map(protocols: list[dict], top: int = 12, per_chain: int = 15) -> dict:
    """One measure (fees, revenue or volume) as a map: {"categories": [...by 24 h], "chains": [...], "grid":
    {category: {chain: 24 h}}, "on_chain": {chain: its biggest protocols}, "total": {...}}."""
    cats: dict[str, dict] = {}
    chain_tot: dict[str, float] = {}
    grid: dict[str, dict[str, float]] = {}
    on_chain: dict[str, list] = {}
    for p in protocols:
        k = category_of(p["category"])
        c = cats.setdefault(k, {"key": k, "name": LABEL[k], "d1": 0.0, "d7": 0.0, "d30": 0.0, "n": 0, "protocols": []})
        c["d1"] += p["d1"]
        c["d7"] += p["d7"]
        c["d30"] += p["d30"]
        c["n"] += 1
        c["protocols"].append(p)
        for chain, v in p["by_chain"].items():
            chain = CHAIN_SHORT.get(chain, chain)
            chain_tot[chain] = chain_tot.get(chain, 0.0) + v
            grid.setdefault(k, {})[chain] = grid.get(k, {}).get(chain, 0.0) + v
            on_chain.setdefault(chain, []).append({"name": p["name"], "slug": p.get("slug", ""), "category": k,
                                                   "d1": v, "ratio": heat(p["d1"], p["d7"], p["d30"])["ratio"]})
    out = []
    for c in cats.values():
        best = sorted(c["protocols"], key=lambda p: -p["d1"])[:top]
        c["protocols"] = [{"name": p["name"], "slug": p.get("slug", ""), "d1": p["d1"], "d7": p["d7"], "d30": p["d30"],
                           "chains": p["chains"][:4],
                           "by_chain": {CHAIN_SHORT.get(a, a): b for a, b in sorted(p["by_chain"].items(), key=lambda kv: -kv[1])[:8]},
                           **{k: v for k, v in heat(p["d1"], p["d7"], p["d30"]).items() if k in ("ratio", "flag")}}
                          for p in best]
        c.update(heat(c["d1"], c["d7"], c["d30"]))
        c["infra"] = c["key"] in INFRA
        out.append(c)
    out.sort(key=lambda c: (c["infra"], -c["d1"]))
    want = [CHAIN_SHORT.get(c, c) for c in WATCH_CHAINS]
    rest = sorted((c for c in chain_tot if c not in want), key=lambda c: -chain_tot[c])[:3]
    chains = [c for c in want if chain_tot.get(c)] + rest
    return {"categories": out, "chains": [{"name": c, "d1": chain_tot[c]} for c in chains],
            "chain_totals": {c: v for c, v in sorted(chain_tot.items(), key=lambda kv: -kv[1])[:40]},
            "on_chain": {c: sorted(rows, key=lambda r: -r["d1"])[:per_chain] for c, rows in on_chain.items()
                         if chain_tot[c] >= 1},
            "grid": {k: {c: v for c, v in row.items() if c in chains} for k, row in grid.items()},
            "total": {k: sum(c[k] for c in out if not c["infra"]) for k in ("d1", "d7", "d30")}}


def crowd(rows: list[dict], window: str, min_vlm: float = 1.0) -> dict:
    """How a whole leaderboard did over a window: traders who traded, the share in profit, what winners made and
    losers lost, and the top 10's share of the winnings."""
    act = [r[window] for r in rows if window in r and r[window]["vlm"] >= min_vlm]
    wins = sorted((x["pnl"] for x in act if x["pnl"] > 0), reverse=True)
    lost = sum(x["pnl"] for x in act if x["pnl"] < 0)
    won = sum(wins)
    return {"traders": len(act), "in_profit": len(wins) / len(act) if act else None, "won": won, "lost": lost,
            "net": won + lost, "top10_share": sum(wins[:10]) / won if won else None,
            "volume": sum(x["vlm"] for x in act)}


def top_traders(rows: list[dict], window: str, n: int = 25, min_vlm: float = 1.0) -> list[dict]:
    act = [r for r in rows if window in r and r[window]["vlm"] >= min_vlm]
    best = sorted(act, key=lambda r: -r[window]["pnl"])[:n]
    return [{"wallet": r["wallet"], "name": r.get("name", ""), "pnl": r[window]["pnl"], "roi": r[window]["roi"],
             "vlm": r[window]["vlm"], "value": r.get("value")} for r in best]


def board(rows: list[dict], n: int = 25) -> dict:
    """A plain winners list (wallet, profit): the top n and their total."""
    best = sorted(rows, key=lambda r: -r["pnl"])[:n]
    return {"rows": best, "total": sum(r["pnl"] for r in best if r["pnl"] > 0), "n": len(rows)}


# ── the screener ─────────────────────────────────────────────────────────────
YEAR = 365 / 30                    # 30 days of anything, as a yearly run rate
SCREEN_MIN_REV = 10_000            # dollars of revenue a day (30-day average) to be screened at all
SHARE_GAIN = 1.10                  # "gaining share": this week's share of its category at least 10% above the month's


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def screen(data: dict[str, list[dict]], meta: dict | None = None) -> dict:
    """Every protocol with real revenue, measured the ways that separate durable money from noise:

        share      its share of its category's fees this week against its share over the month (above 1 = gaining)
        take       revenue / volume: how much of each traded dollar it keeps (venues with volume only)
        earnings   revenue after token incentives, 30 days (DefiLlama's earnings; None where it isn't published)
        holders    the share of revenue that reaches token holders (buybacks, burns, staking payouts)
        turnover   volume / TVL a day; yield_tvl = yearly revenue / TVL
        ps, pf     market cap / yearly revenue and / yearly fees, and ps_vs = ps against its category's median
        setup      the classic shape, fixed in advance: gaining share, earnings not negative, and a price-to-sales
                   at or below its category's median

    {"rows": [...], "medians": {category: ps}, "categories": {category: share-of-all-fees now and over the month}}."""
    meta = meta or {}
    by = {k: {p["name"]: p for p in v} for k, v in data.items()}
    fees = data.get("fees") or []
    cat7: dict[str, float] = {}
    cat30: dict[str, float] = {}
    for p in fees:
        k = category_of(p["category"])
        cat7[k] = cat7.get(k, 0.0) + p["d7"]
        cat30[k] = cat30.get(k, 0.0) + p["d30"]
    rows = []
    for p in fees:
        k = category_of(p["category"])
        if k in INFRA:
            continue
        rev = by.get("revenue", {}).get(p["name"])
        if not rev or rev["d30"] / 30 < SCREEN_MIN_REV:
            continue
        vol = by.get("volume", {}).get(p["name"])
        earn = by.get("earnings", {}).get(p["name"])
        hold = by.get("holders", {}).get(p["name"])
        m = meta.get(p["name"]) or meta.get("slug:" + p.get("slug", "")) or meta.get("parent:" + p.get("parent", "")) or {}
        s7 = p["d7"] / cat7[k] if cat7.get(k) else None
        s30 = p["d30"] / cat30[k] if cat30.get(k) else None
        tvl, mcap = m.get("tvl") or None, m.get("mcap") or None
        rows.append({
            "name": p["name"], "slug": p.get("slug", ""), "category": k, "symbol": m.get("symbol", ""),
            "fees30": p["d30"], "rev30": rev["d30"], "rev1": rev["d1"], "ratio": heat(p["d1"], p["d7"], p["d30"])["ratio"],
            "share": s7, "share_mom": (s7 / s30) if s7 and s30 else None,
            "take": rev["d30"] / vol["d30"] if vol and vol["d30"] else None,
            "earn30": earn["d30"] if earn else None,
            "holders": min(1.0, hold["d30"] / rev["d30"]) if hold and rev["d30"] else None,
            "tvl": tvl, "turnover": (vol["d30"] / 30) / tvl if vol and tvl else None,
            "yield_tvl": rev["d30"] * YEAR / tvl if tvl else None,
            "mcap": mcap, "ps": mcap / (rev["d30"] * YEAR) if mcap else None, "pf": mcap / (p["d30"] * YEAR) if mcap and p["d30"] else None})
    medians = {k: _median([r["ps"] for r in rows if r["category"] == k and r["ps"]]) for k in {r["category"] for r in rows}}
    for r in rows:
        med = medians.get(r["category"])
        r["ps_vs"] = r["ps"] / med if r["ps"] and med else None
        r["setup"] = bool(r["share_mom"] and r["share_mom"] >= SHARE_GAIN and r["ps_vs"] is not None and r["ps_vs"] <= 1.0
                          and (r["earn30"] is None or r["earn30"] >= 0))
    rows.sort(key=lambda r: (not r["setup"], -(r["share_mom"] or 0)))
    tot7, tot30 = sum(v for k, v in cat7.items() if k not in INFRA), sum(v for k, v in cat30.items() if k not in INFRA)
    cats = {k: {"share": cat7[k] / tot7 if tot7 else None, "share_mom": (cat7[k] / tot7) / (cat30[k] / tot30) if tot7 and tot30 and cat30.get(k) else None}
            for k in cat7 if k not in INFRA}
    return {"rows": rows, "medians": medians, "categories": cats}


def capital(maps: dict[str, dict], stables: dict[str, dict]) -> list[dict]:
    """Per chain: stablecoin supply and its 7- and 30-day change (capital arriving or leaving), what its apps earn
    against what the chain itself earns in gas (who is capturing the value), and fees over 24 h."""
    rev = (maps.get("revenue") or maps.get("fees") or {})
    grid = rev.get("grid") or {}
    out = []
    for chain in [c["name"] for c in rev.get("chains") or []]:
        gas = (grid.get("chains") or {}).get(chain, 0.0)
        apps = sum(row.get(chain, 0.0) for k, row in grid.items() if k not in INFRA)
        st = stables.get(chain) or stables.get({"Hyperliquid": "Hyperliquid L1"}.get(chain, chain)) or {}
        if not st and apps + gas < 10_000:
            continue
        out.append({"chain": chain, "apps": apps, "gas": gas, "capture": apps / gas if gas else None,
                    "fees": ((maps.get("fees") or {}).get("chain_totals") or {}).get(chain), **{f"st_{k}": v for k, v in st.items()}})
    return sorted(out, key=lambda r: -(r.get("st_supply") or 0))

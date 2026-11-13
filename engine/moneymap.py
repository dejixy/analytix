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

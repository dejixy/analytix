"""The Analyzer: source parsers (ingestion/moneymap.py), the money map (engine/moneymap.py), the service."""
import asyncio
from types import SimpleNamespace

import pytest

from api.analyzer import AnalyzerService
from engine import moneymap as M
from ingestion import moneymap as S

LLAMA = {"protocols": [
    {"name": "Hyperliquid Perps", "category": "Derivatives", "chains": ["Hyperliquid L1"], "total24h": 3_000_000,
     "total7d": 14_000_000, "total30d": 60_000_000, "breakdown24h": {"hyperliquid l1": {"Hyperliquid Perps": 3_000_000}}},
    {"name": "pump.fun", "category": "Launchpad", "chains": ["Solana"], "total24h": 2_000_000, "total7d": 7_000_000,
     "total30d": 24_000_000},
    {"name": "Uniswap", "category": "Dexs", "chains": ["Ethereum", "Base"], "total24h": 1_000_000, "total7d": 9_000_000,
     "total30d": 60_000_000, "breakdown24h": {"ethereum": {"v3": 500_000, "v4": 100_000}, "base": {"v3": 400_000}}},
    {"name": "Tiny", "category": "Something New", "chains": ["Base"], "total24h": 10, "total7d": 70, "total30d": 300},
    {"name": "Dead", "category": "Dexs", "chains": ["Base"], "total24h": None, "total7d": 0, "total30d": 0},
]}


def test_llama_protocols_become_a_map_by_category_and_chain():
    ps = S.parse_llama(LLAMA)
    assert [p["name"] for p in ps] == ["Hyperliquid Perps", "pump.fun", "Uniswap", "Tiny"]
    assert ps[2]["by_chain"] == {"Ethereum": 600_000, "Base": 400_000} and ps[1]["by_chain"] == {"Solana": 2_000_000}
    m = M.money_map(ps)
    assert [c["key"] for c in m["categories"]] == ["perps", "memes", "spot", "other"]
    perps, memes, spot, other = m["categories"]
    assert perps["flag"] == "hot" and perps["ratio"] == 1.5                 # 3.0M against 2.0M a day, week above too
    assert memes["flag"] == "hot" and memes["ratio"] == 2.5
    assert spot["flag"] == "cooling" and spot["ratio"] == 0.5
    assert other["flag"] == ""                                              # too small to flag either way
    assert m["grid"]["perps"] == {"Hyperliquid": 3_000_000} and m["grid"]["spot"] == {"Ethereum": 600_000, "Base": 400_000}
    assert [c["name"] for c in m["chains"]] == ["Solana", "Hyperliquid", "Base", "Ethereum"]   # his chains first
    assert m["total"]["d1"] == 6_000_010 and perps["protocols"][0]["name"] == "Hyperliquid Perps"
    infra = M.money_map(ps + [{"name": "Tether", "category": "Stablecoin Issuer", "chains": ["Tron"], "d1": 9e7, "d7": 6e8,
                               "d30": 2.7e9, "by_chain": {"Tron": 9e7}}])
    assert infra["categories"][-1]["key"] == "stables" and infra["categories"][-1]["infra"]      # the rails go last
    assert infra["total"]["d1"] == 6_000_010                                                    # and stay out of totals
    assert M.category_of("NFT Marketplace") == "nfts" and M.category_of("") == "other"
    assert M.heat(100, 0, 0)["ratio"] is None


HL = {"leaderboardRows": [
    {"ethAddress": "0xa", "accountValue": "1000", "displayName": "ace", "windowPerformances": [
        ["day", {"pnl": "100", "roi": "0.1", "vlm": "5000"}], ["week", {"pnl": "900", "roi": "0.9", "vlm": "9000"}]]},
    {"ethAddress": "0xb", "accountValue": "500", "windowPerformances": [
        ["day", {"pnl": "-300", "roi": "-0.3", "vlm": "8000"}], ["week", {"pnl": "50", "roi": "0.1", "vlm": "100"}]]},
    {"ethAddress": "0xc", "accountValue": "9", "windowPerformances": [["day", {"pnl": "0", "roi": "0", "vlm": "0"}]]},
    {"accountValue": "1"},
]}


def test_leaderboard_crowd_and_top_traders():
    rows = S.parse_hl_leaderboard(HL)
    assert [r["wallet"] for r in rows] == ["0xa", "0xb", "0xc"] and rows[0]["day"]["pnl"] == 100.0
    c = M.crowd(rows, "day")
    assert c == {"traders": 2, "in_profit": 0.5, "won": 100.0, "lost": -300.0, "net": -200.0, "top10_share": 1.0, "volume": 13000.0}
    top = M.top_traders(rows, "week", n=1)
    assert top == [{"wallet": "0xa", "name": "ace", "pnl": 900.0, "roi": 0.9, "vlm": 9000.0, "value": 1000.0}]
    assert M.crowd([], "day")["in_profit"] is None


def test_other_boards_parse_forgivingly():
    assert S.parse_birdeye({"data": {"items": [{"address": "So1", "pnl": 12.5, "volume": 100, "trade_count": 3}, {"pnl": 1}]}}) == \
        [{"wallet": "So1", "pnl": 12.5, "vlm": 100.0, "trades": 3}]
    assert S.parse_polymarket([{"proxyWallet": "0xp", "amount": 77, "pseudonym": "Zed"}, "junk"]) == [{"wallet": "0xp", "name": "Zed", "pnl": 77.0}]
    d = S.parse_dune({"result": {"rows": [{"Trader": "0x1", "profit_usd": 5, "label": "x"}, {"Trader": "0x2", "profit_usd": 50}]}})
    assert [r["wallet"] for r in d] == ["0x2", "0x1"] and d[1]["name"] == "x"
    with pytest.raises(ValueError):
        S.parse_dune({"result": {"rows": [{"foo": 1}]}})
    assert S.parse_dune({}) == []
    b = M.board([{"wallet": "a", "pnl": 5.0}, {"wallet": "b", "pnl": -1.0}, {"wallet": "c", "pnl": 9.0}], n=2)
    assert [r["wallet"] for r in b["rows"]] == ["c", "a"] and b["total"] == 14.0 and b["n"] == 3


def test_the_service_keeps_each_source_apart(tmp_path):
    svc = AnalyzerService(SimpleNamespace(mode="live"), tmp_path / "dune.json", tmp_path / "screen.jsonl")

    async def protos():
        return {"Hyperliquid Perps": {"tvl": 5e9, "mcap": 3e9, "symbol": "HYPE"}}

    async def stables(chains):
        return {"Solana": {"supply": 1.2e10, "d7": 3e8, "d30": 9e8, "d7_pct": 0.026, "d30_pct": 0.08}}
    svc.fetch_protocols, svc.fetch_stables = protos, stables

    async def fees():
        ps = S.parse_llama(LLAMA)
        return {"fees": ps, "revenue": ps[:2], "volume": []}

    async def hl():
        return S.parse_hl_leaderboard(HL)

    async def sol(window):
        raise RuntimeError("no key: set ANALYTIX_BIRDEYE_KEY (free at birdeye.so)")

    async def poly(window):
        return [{"wallet": "0xp", "name": "Zed", "pnl": 77.0}]

    async def dune(qid):
        return [{"wallet": "0xn", "pnl": 3.0, "name": ""}]

    svc.fetch = {"fees": fees, "capital": None, "hyperliquid": hl, "solana": sol, "polymarket": poly, "dune": dune}

    async def run():
        for k in svc.fetch:
            await svc.refresh(k)
    asyncio.run(run())
    p = svc.public()
    assert p["map"]["categories"][0]["key"] == "perps" and p["hyperliquid"]["windows"]["day"]["crowd"]["traders"] == 2
    assert [b["title"] for b in p["boards"]] == ["Polymarket, 7 days"]
    assert p["screen"]["priced"] and p["screen_days"] == 1 and (tmp_path / "screen.jsonl").read_text().count("\n") == 1
    sol = next(c for c in p["capital"] if c["chain"] == "Solana")
    assert sol["st_supply"] == 1.2e10 and sol["st_d7"] == 3e8 and p["status"]["DefiLlama stablecoins"]["ok"]
    assert set(p["maps"]) == {"fees", "revenue"} and len(p["maps"]["revenue"]["categories"]) == 2    # an empty measure is left out

    async def pools(chain):
        return "solana", [{"name": "BONK / SOL", "vol": 5.0}]

    async def hist(slug, metric):
        raise RuntimeError("boom")
    svc.fetch_pools, svc.fetch_history = pools, hist
    ch = asyncio.run(svc.chain("Solana", "fees"))
    assert ch["protocols"][0]["name"] == "pump.fun" and ch["total"] == 2_000_000 and ch["pools"]["rows"][0]["name"] == "BONK / SOL"
    assert "boom" in asyncio.run(svc.protocol("pump.fun", "fees"))["error"]                       # shown, not raised

    async def thin():
        return {"fees": S.parse_llama(LLAMA), "revenue": [], "volume": []}                        # revenue throttled this time
    svc.fetch["fees"] = thin
    asyncio.run(svc.refresh("fees"))
    assert "revenue" in svc.public()["maps"]                                                      # the last good read stays
    st = p["status"]
    assert st["DefiLlama fees"]["ok"] and st["Birdeye (Solana traders)"]["ok"] is False and "no key" in st["Birdeye (Solana traders)"]["error"]
    assert "no queries" in st["Dune queries"]["error"]
    (tmp_path / "dune.json").write_text('[{"id": 42, "title": "NFT traders, 7 days", "category": "nfts", "chain": "Ethereum"}]')
    asyncio.run(svc.refresh("dune"))
    assert svc.public()["boards"][-1]["title"] == "NFT traders, 7 days" and svc.public()["status"]["Dune queries"]["ok"]


def test_pools_and_history_parse():
    pools = S.parse_pools({"data": [
        {"attributes": {"name": "A / SOL", "address": "x1", "volume_usd": {"h24": "100"}, "price_change_percentage": {"h24": "-5"},
                        "transactions": {"h24": {"buys": 3, "sells": 2, "buyers": 2}}, "reserve_in_usd": "50", "fdv_usd": "9"},
         "relationships": {"dex": {"data": {"id": "pump-fun"}}}},
        {"attributes": {"name": "B / SOL", "volume_usd": {"h24": "900"}}}, {"attributes": {"name": "dead", "volume_usd": {}}}]})
    assert [p["name"] for p in pools] == ["B / SOL", "A / SOL"]
    assert {k: pools[1][k] for k in ("name", "dex", "address", "vol", "chg", "trades", "buyers", "liquidity", "fdv")} == \
        {"name": "A / SOL", "dex": "pump fun", "address": "x1", "vol": 100.0, "chg": -5.0, "trades": 5, "buyers": 2,
         "liquidity": 50.0, "fdv": 9.0}
    assert S.parse_history({"totalDataChart": [[1, 5], ["x", 1], [2, 7]]}, days=1) == [[2000, 7.0]]
    ps = S.parse_llama(LLAMA)
    assert ps[1]["slug"] == "pump.fun" and S.slug_of("Jito MEV") == "jito-mev"
    m = M.money_map(ps)
    assert m["on_chain"]["Base"][0]["name"] == "Uniswap" and m["chain_totals"]["Solana"] == 2_000_000
    assert m["categories"][2]["protocols"][0]["by_chain"] == {"Ethereum": 600_000, "Base": 400_000}


def test_the_screen_finds_the_classic_setup_and_only_that():
    def row(name, cat, d7, d30, d1=None):
        return {"name": name, "category": cat, "slug": S.slug_of(name), "parent": "", "chains": [], "d1": d1 if d1 is not None else d7 / 7,
                "d7": d7, "d30": d30, "by_chain": {}}
    # two perps venues: A is gaining share of the category and cheap; B is losing share and dear
    fees = [row("A", "Derivatives", 7e6, 20e6), row("B", "Derivatives", 7e6, 40e6), row("Gas", "Chain", 1e6, 4e6)]
    rev = [row("A", "Derivatives", 3.5e6, 10e6), row("B", "Derivatives", 3.5e6, 20e6), row("Tiny", "Derivatives", 10, 100)]
    vol = [row("A", "Derivatives", 7e9, 20e9), row("B", "Derivatives", 7e9, 40e9)]
    earn = [row("A", "Derivatives", 2e6, 6e6), row("B", "Derivatives", -1e6, -5e6)]
    hold = [row("A", "Derivatives", 1e6, 5e6)]
    meta = {"A": {"tvl": 1e9, "mcap": 6e8, "symbol": "AAA"}, "slug:b": {"tvl": 2e9, "mcap": 5e9, "symbol": "BBB"}}
    sc = M.screen({"fees": fees, "revenue": rev, "volume": vol, "earnings": earn, "holders": hold}, meta)
    a, b = sc["rows"]
    assert (a["name"], a["setup"], b["name"], b["setup"]) == ("A", True, "B", False)                # and gas isn't screened
    assert abs(a["share"] - 0.5) < 1e-9 and abs(a["share_mom"] - 0.5 / (20 / 60)) < 1e-9 and b["share_mom"] < 1
    assert abs(a["take"] - 10e6 / 20e9) < 1e-12 and a["holders"] == 0.5 and a["earn30"] == 6e6 and b["earn30"] == -5e6
    assert abs(a["ps"] - 6e8 / (10e6 * 365 / 30)) < 1e-9 and abs(a["yield_tvl"] - 10e6 * 365 / 30 / 1e9) < 1e-12
    assert a["ps_vs"] < 1 < b["ps_vs"] and abs(a["turnover"] - (20e9 / 30) / 1e9) < 1e-9
    assert sc["categories"]["perps"]["share"] == 1.0 and "chains" not in sc["categories"]
    no_price = M.screen({"fees": fees, "revenue": rev}, {})
    assert not any(r["setup"] for r in no_price["rows"]) and no_price["rows"][0]["ps"] is None      # no price, no setup
    assert S.parse_stable_chart([{"totalCirculatingUSD": {"peggedUSD": 100 + k}} for k in range(31)]) == \
        {"supply": 130.0, "d7": 7.0, "d30": 30.0, "d7_pct": 130 / 123 - 1, "d30_pct": 130 / 100 - 1}
    pm = S.parse_protocols([{"name": "Uni V3", "slug": "uni-v3", "tvl": 5, "mcap": 9, "symbol": "UNI", "parentProtocol": "parent#uni"},
                            {"name": "Uni V2", "tvl": 2, "mcap": 9, "symbol": "UNI", "parentProtocol": "parent#uni"}])
    assert pm["parent:parent#uni"] == {"tvl": 7.0, "mcap": 9.0, "symbol": "UNI"} and pm["slug:uni-v3"]["tvl"] == 5.0


def test_assets_in_motion_by_kind(tmp_path):
    dexes = [{"id": "raydium", "name": "Raydium"}, {"id": "raydium-clmm", "name": "Raydium (CLMM)"}, {"id": "orca", "name": "Orca"},
             {"id": "pump-fun", "name": "Pump.fun"}, {"id": "pumpswap", "name": "PumpSwap"}, {"id": "uniswap_v3", "name": "Uniswap V3"},
             {"id": "uniswap_v2", "name": "Uniswap V2"}]
    assert M.match_dexes("Raydium", dexes) == ["raydium", "raydium-clmm"]
    assert M.match_dexes("Uniswap V3", dexes) == ["uniswap_v3"] and M.match_dexes("pump.fun", dexes) == ["pump-fun", "pumpswap"]
    assert M.match_dexes("Aave", dexes) == [] and M.match_dexes("Or", dexes) == []
    y = S.parse_yields({"data": [{"project": "aave-v3", "chain": "Ethereum", "symbol": "USDC", "tvlUsd": 9e8, "apy": 4.2, "apyPct7D": -0.6},
                                 {"project": "aave-v3", "chain": "Base", "symbol": "WETH", "tvlUsd": 2e8, "apy": 1.9},
                                 {"project": "morpho", "chain": "Base", "symbol": "USDC", "tvlUsd": 5e8}, {"project": "x", "tvlUsd": 0}]})
    t = M.yields_table(y, "aave", "Aave")
    assert [r["name"] for r in t["rows"]] == ["USDC", "WETH"] and t["rows"][0]["apy_7d"] == -0.6 and M.yields_table(y, "compound", "C") is None
    pt = M.perps_table({"BTC": {"mark": 100.0, "prev_day": 80.0, "day_volume": 9e9, "funding": 1e-5, "oi": 1000.0},
                        "DEAD": {"mark": 1.0, "day_volume": 0}})
    assert pt["rows"] == [{"name": "BTC", "vol": 9e9, "chg": 25.0, "funding": 1e-5 * 24 * 365 * 100, "oi": 100_000.0}]
    pm = S.parse_poly_markets([{"question": "Rain?", "slug": "rain", "volume24hr": 5, "outcomePrices": "[\"0.62\", \"0.38\"]",
                                "oneDayPriceChange": -0.04, "liquidityNum": 9}, {"slug": "no-question"}])
    mt = M.markets_table(pm)
    assert mt["rows"][0]["yes"] == 62.0 and mt["rows"][0]["chg"] == -4.0 and mt["rows"][0]["url"].endswith("/market/rain")
    dt = M.dune_table({"id": 7, "title": "TON collections", "explorer": "https://tonviewer.com/"},
                      S.parse_dune_table({"execution_ended_at": "2026-11-15T10:00:00Z", "result": {
                          "rows": [{"collection_address": "0:AB", "volume_ton": 5.5, "volume_change": 12.0, "sales_24h": 3}],
                          "metadata": {"column_names": ["collection_address", "volume_ton", "volume_change", "sales_24h"]}}}))
    assert [c["fmt"] for c in dt["columns"]] == ["addr", "ton", "chg", "auto"] and dt["as_of"].startswith("2026-11-15")

    (tmp_path / "dune.json").write_text('[{"id": 7, "title": "TON collections", "assets": true, "protocols": ["fragment"], "chains": ["TON"], "refresh_h": 6}]')
    svc = AnalyzerService(SimpleNamespace(mode="live", scanner=SimpleNamespace(contexts={"ETH": {"mark": 3000.0, "day_volume": 5e9}})),
                          tmp_path / "dune.json")
    ran = []

    async def table(qid):
        return {"columns": ["collection_address", "volume_ton"], "rows": [{"collection_address": "0:AB", "volume_ton": 5.5}],
                "ended": "2020-01-01T00:00:00Z"}

    async def run(qid):
        ran.append(qid)

    async def dx(network):
        return dexes

    async def pools(network, dex):
        return [{"name": f"X / SOL on {dex}", "dex": dex, "address": "a" + dex, "vol": 10.0 if dex == "raydium" else 30.0, "chg": 1.0,
                 "trades": 5, "buyers": 2, "liquidity": 1.0, "fdv": 1.0}]

    async def yl():
        return y
    svc.fetch_dune_table, svc.run_dune, svc.fetch_dexes, svc.fetch_dex_pools, svc.fetch_yields = table, run, dx, pools, yl

    async def go():
        await svc.refresh("dune")
        await svc.refresh("dune")
        return (await svc.assets("fragment", "Fragment", "nfts", "TON"), await svc.assets("raydium", "Raydium", "spot", "Solana"),
                await svc.assets("aave", "Aave", "lending", "Ethereum"), await svc.assets("hyperliquid-perps", "Hyperliquid Perps", "perps", ""),
                await svc.assets("opensea", "OpenSea", "nfts", "Ethereum"), await svc.chain("TON"))
    frag, ray, aave, hl, sea, ton = asyncio.run(go())
    assert ran == [7]                                                             # stale result: asked to run again, once
    assert frag["tables"][0]["title"] == "TON collections" and not frag["notes"]
    assert [r["name"] for r in ray["tables"][0]["rows"]] == ["X / SOL on raydium-clmm", "X / SOL on raydium"]
    assert aave["tables"][0]["rows"][0]["name"] == "USDC" and hl["tables"][0]["rows"][0]["name"] == "ETH"
    assert not sea["tables"] and "Dune query" in sea["notes"][0]
    assert ton["tables"][0]["title"] == "TON collections"


def test_heating_up_is_a_fixed_rule_on_the_last_hour(tmp_path):
    def pool(name, vol, vol_h1, buyers, sellers, liq=200_000, chg=5.0, addr=None):
        return {"name": name, "dex": "pumpswap", "address": addr or name, "vol": vol, "vol_h1": vol_h1, "chg": chg, "chg_h1": 3.0,
                "buyers_h1": buyers, "sellers_h1": sellers, "liquidity": liq, "fdv": 5e6, "price": 0.01, "trades": 10, "buyers": 5}
    hot = pool("DOG / SOL", 2_400_000, 300_000, 400, 250)                       # 3x pace, more buyers, price flat: early
    assert M.heat_of(hot)["pace"] == 3.0 and M.heat_of(hot)["early"] and round(M.heat_of(hot)["buy_share"], 1) == 61.5
    ran = pool("CAT / SOL", 2_400_000, 300_000, 400, 250, chg=180.0)            # same heat, but it already ran: not early
    assert M.heat_of(ran) and not M.heat_of(ran)["early"]
    assert M.heat_of(pool("SLOW / SOL", 2_400_000, 100_000, 400, 250)) is None  # only the day's average pace
    assert M.heat_of(pool("DUMP / SOL", 2_400_000, 300_000, 200, 300)) is None  # sellers outnumber buyers
    assert M.heat_of(pool("THIN / SOL", 2_400_000, 300_000, 400, 250, liq=10_000)) is None
    assert M.heat_of(pool("TINY / SOL", 100_000, 30_000, 400, 250)) is None
    major = pool("SOL / USDC", 9e8, 9e7, 900, 100)
    t = M.heating_table({"Solana": ("solana", [hot, ran, major, pool("DOG / USDC", 2_400_000, 200_000, 9, 1, addr="dup")])}, "Memes")
    assert [r["name"] for r in t["rows"]] == ["DOG / SOL", "CAT / SOL"] and t["rows"][0]["mark"] == "early" and t["rows"][1]["mark"] == ""
    assert t["rows"][0]["url"].endswith("/solana/pools/DOG / SOL") and M.base_token("USELESS / SOL 0.3%") == "USELESS"
    assert len(M.heating_table({"Solana": ("solana", [major])}, "All", memes_only=False)["rows"]) == 1

    svc = AnalyzerService(SimpleNamespace(mode="live"), None, None, tmp_path / "heat.jsonl")

    async def trending(chain):
        if chain == "BSC":
            raise RuntimeError("down")
        return chain.lower(), [hot] if chain == "Solana" else []

    async def busiest(chain):
        return chain.lower(), [ran, hot]
    svc.fetch_trending, svc.fetch_pools = trending, busiest
    assert asyncio.run(svc.heating("memes")) == {"pending": True}                # nothing read yet: the page says so
    asyncio.run(svc.refresh("heating"))
    d = asyncio.run(svc.heating("memes"))
    assert d["chains"] == ["Solana", "Base", "Ethereum"] and d["errors"] == ["BSC: RuntimeError"]
    assert [r["chain"] for r in d["table"]["rows"]].count("Solana") == 2 and (tmp_path / "heat.jsonl").read_text().count("\n") == 1
    assert svc.public()["status"]["GeckoTerminal (heating up)"]["ok"]
    asyncio.run(svc.refresh("heating"))
    assert (tmp_path / "heat.jsonl").read_text().count("\n") == 1                # logged at most hourly

    async def all_down(chain):
        raise RuntimeError("429")
    svc.fetch_trending = all_down
    asyncio.run(svc.refresh("heating"))
    assert not svc.public()["status"]["GeckoTerminal (heating up)"]["ok"]
    assert len(asyncio.run(svc.heating("memes"))["table"]["rows"]) == 6          # the last good pools stay on the page

    calls = []

    class FakeClient:
        async def get(self, url, params=None):
            calls.append(url)
            return SimpleNamespace(status_code=429 if len(calls) == 1 else 200)

    async def paced():
        S._gecko.update(lock=None, last=0.0)
        real = S.asyncio.sleep
        slept = []

        async def nap(x):
            slept.append(x)
        S.asyncio.sleep = nap
        try:
            r = await S.gecko_get(FakeClient(), "u")
            await S.gecko_get(FakeClient(), "v")
        finally:
            S.asyncio.sleep = real
        return r, slept
    r, slept = asyncio.run(paced())
    assert r.status_code == 200 and len(calls) == 3 and 20.0 in slept and any(0 < x <= S.GECKO_GAP_S for x in slept)
    full = S.parse_pools({"data": [{"attributes": {"name": "A / SOL", "address": "x", "volume_usd": {"h24": "100", "h1": "9"},
                                                   "price_change_percentage": {"h24": "1", "h1": "2", "h6": "3"},
                                                   "transactions": {"h1": {"buys": 7, "sells": 2, "buyers": 5, "sellers": 1}},
                                                   "base_token_price_usd": "0.5", "pool_created_at": "2026-11-15T00:00:00Z"}}]})[0]
    assert (full["vol_h1"], full["chg_h1"], full["buyers_h1"], full["sellers_h1"], full["price"]) == (9.0, 2.0, 5, 1, 0.5)

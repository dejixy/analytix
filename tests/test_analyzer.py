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
    assert pools[1] == {"name": "A / SOL", "dex": "pump fun", "address": "x1", "vol": 100.0, "chg": -5.0, "trades": 5, "buyers": 2,
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

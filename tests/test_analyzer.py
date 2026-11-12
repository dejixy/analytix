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
    svc = AnalyzerService(SimpleNamespace(mode="live"), tmp_path / "dune.json")

    async def fees():
        return S.parse_llama(LLAMA)

    async def hl():
        return S.parse_hl_leaderboard(HL)

    async def sol(window):
        raise RuntimeError("no key: set ANALYTIX_BIRDEYE_KEY (free at birdeye.so)")

    async def poly(window):
        return [{"wallet": "0xp", "name": "Zed", "pnl": 77.0}]

    async def dune(qid):
        return [{"wallet": "0xn", "pnl": 3.0, "name": ""}]

    svc.fetch = {"fees": fees, "hyperliquid": hl, "solana": sol, "polymarket": poly, "dune": dune}

    async def run():
        for k in svc.fetch:
            await svc.refresh(k)
    asyncio.run(run())
    p = svc.public()
    assert p["map"]["categories"][0]["key"] == "perps" and p["hyperliquid"]["windows"]["day"]["crowd"]["traders"] == 2
    assert [b["title"] for b in p["boards"]] == ["Polymarket, 7 days"]
    st = p["status"]
    assert st["DefiLlama fees"]["ok"] and st["Birdeye (Solana traders)"]["ok"] is False and "no key" in st["Birdeye (Solana traders)"]["error"]
    assert "no queries" in st["Dune queries"]["error"]
    (tmp_path / "dune.json").write_text('[{"id": 42, "title": "NFT traders, 7 days", "category": "nfts", "chain": "Ethereum"}]')
    asyncio.run(svc.refresh("dune"))
    assert svc.public()["boards"][-1]["title"] == "NFT traders, 7 days" and svc.public()["status"]["Dune queries"]["ok"]

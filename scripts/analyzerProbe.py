"""
What each of the Analyzer's sources really returns from this server: reachable or not, how many rows parsed, and a
sample of the raw reply when nothing parsed (so the reader can be fixed to the real shape).

    bash scripts/run.sh analyzer        (or: python -m scripts.analyzerProbe)

Keys go in ~/.analytix-env:   export ANALYTIX_BIRDEYE_KEY=...   export ANALYTIX_DUNE_KEY=...
"""
import asyncio
import json
import os
import sys

import httpx

from engine import moneymap as M
from ingestion import moneymap as S


async def raw(url: str, params=None, headers=None):
    async with httpx.AsyncClient(timeout=90, headers={**S.UA, **(headers or {})}) as c:
        r = await c.get(url, params=params)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:300]


def sample(j) -> str:
    return json.dumps(j, default=str)[:600]


async def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    print("1. DefiLlama fees")
    try:
        code, j = await raw(S.LLAMA_FEES, {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"})
        ps = S.parse_llama(j) if isinstance(j, dict) else []
        print(f"   HTTP {code}, {len(ps)} protocols with fees")
        if ps:
            m = M.money_map(ps)
            for c in m["categories"][:14]:
                print(f"   {c['name']:<28}{c['d1'] / 1e6:>9.2f}M 24h   x{c['ratio'] if c['ratio'] is not None else '-'} of normal   {c['flag']}")
            print("   chains: " + ", ".join(f"{c['name']} {c['d1'] / 1e6:.2f}M" for c in m["chains"]))
            cats = sorted({p["category"] for p in ps if M.category_of(p["category"]) == "other"})
            print(f"   categories filed under Other: {', '.join(cats)[:700]}")
            print(f"   with a chain split: {sum(1 for p in ps if p['by_chain'])} of {len(ps)}")
            first = (j.get('protocols') or [{}])[0]
            print(f"   one protocol's keys: {sorted(first)[:40]}")
            print(f"   its breakdown24h: {sample(first.get('breakdown24h'))[:300]}")
        else:
            print("   sample: " + sample(j))
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("1b. Revenue, volume, one protocol's history, one chain's pools")
    try:
        got = await S.fetch_llama()
        print("   " + ", ".join(f"{k}: {len(v)} protocols" for k, v in got.items()))
        top = max(got["fees"], key=lambda p: p["d1"]) if got.get("fees") else None
        if top:
            h = await S.fetch_history(top["slug"], "fees")
            print(f"   history of {top['name']} (slug {top['slug']}): {len(h)} days")
        for chain in ("TON", "Solana"):
            code, j = await raw(S.GECKO_POOLS.format(network=S.GECKO_NETWORK[chain]), {"page": 1, "sort": "h24_volume_usd_desc"})
            rows = S.parse_pools(j) if isinstance(j, dict) else []
            print(f"   pools on {chain}: HTTP {code}, {len(rows)} pools" + (f", busiest {rows[0]['name']} ${rows[0]['vol'] / 1e6:.1f}M" if rows else "; sample: " + sample(j)))
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("1c. The screener's inputs: earnings after incentives, holders' revenue, TVL and market caps, stablecoins")
    try:
        got = await S.fetch_llama()
        print("   " + ", ".join(f"{k}: {len(v)}" for k, v in got.items()))
        meta = await S.fetch_protocols()
        sc = M.screen(got, meta)
        rows = sc["rows"]
        print(f"   {len(rows)} protocols screened; with a market cap {sum(1 for r in rows if r['mcap'])}, with TVL "
              f"{sum(1 for r in rows if r['tvl'])}, with earnings {sum(1 for r in rows if r['earn30'] is not None)}, "
              f"with volume {sum(1 for r in rows if r['take'] is not None)}; setups {sum(1 for r in rows if r['setup'])}")
        for r in rows[:8]:
            print(f"   {'*' if r['setup'] else ' '} {r['name'][:22]:<22} {r['category']:<10} rev/day ${r['rev30'] / 30 / 1e3:>8,.0f}K  "
                  f"share trend {r['share_mom'] or 0:.2f}x  P/S {r['ps'] or 0:>6.1f}  vs cat {r['ps_vs'] or 0:.2f}")
        st = await S.fetch_stables(["Solana", "Hyperliquid L1", "Base"])
        print("   stablecoins: " + (", ".join(f"{c} ${v['supply'] / 1e9:.1f}B ({(v['d7_pct'] or 0) * 100:+.1f}% 7d)" for c, v in st.items()) or "none read"))
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("2. Hyperliquid leaderboard")
    try:
        code, j = await raw(S.HL_LEADERBOARD)
        rows = S.parse_hl_leaderboard(j) if isinstance(j, dict) else []
        print(f"   HTTP {code}, {len(rows)} traders")
        if rows:
            for w in ("day", "week", "month"):
                c = M.crowd(rows, w)
                print(f"   {w:<6} {c['traders']:,} traded, {c['in_profit']:.0%} in profit, winners +${c['won'] / 1e6:,.1f}M, "
                      f"losers ${c['lost'] / 1e6:,.1f}M, top 10 took {c['top10_share']:.0%}")
        else:
            print("   sample: " + sample(j))
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("3. Polymarket leaderboard")
    try:
        code, j = await raw(S.POLYMARKET_PROFIT, {"timePeriod": "WEEK", "orderBy": "PNL", "category": "OVERALL", "limit": 5})
        rows = S.parse_polymarket(j)
        print(f"   HTTP {code}, {len(rows)} rows" + ("" if rows else "; sample: " + sample(j)))
        for r in rows[:3]:
            print(f"   {r['name'] or r['wallet'][:10]}: ${r['pnl']:,.0f}")
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("4. Birdeye, Solana traders")
    key = os.getenv("ANALYTIX_BIRDEYE_KEY", "")
    if not key:
        print("   no key yet: get a free one at birdeye.so, then add  export ANALYTIX_BIRDEYE_KEY=...  to ~/.analytix-env")
    else:
        try:
            code, j = await raw(S.BIRDEYE_TRADERS, {"type": "1W", "sort_by": "PnL", "sort_type": "desc", "offset": 0, "limit": 10},
                                {"X-API-KEY": key, "x-chain": "solana", "accept": "application/json"})
            rows = S.parse_birdeye(j) if isinstance(j, dict) else []
            print(f"   HTTP {code}, {len(rows)} rows" + ("" if rows else "; sample: " + sample(j)))
        except Exception as exc:                          # noqa: BLE001
            print(f"   failed: {type(exc).__name__}: {exc}")
    print("5. Dune")
    print("   key set" if os.getenv("ANALYTIX_DUNE_KEY") else
          "   no key yet: get a free one at dune.com, then add  export ANALYTIX_DUNE_KEY=...  to ~/.analytix-env")


if __name__ == "__main__":
    asyncio.run(main())

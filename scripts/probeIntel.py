"""
A one-off check of the independent sources behind the prediction-market edges, run on the server (they're
public, no keys). Prints what each source returns and how many Hyperliquid markets it can price.

    python -m scripts.probeIntel
"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

from api.intel import IntelService
from engine.intel import league_for
from engine.options import build_surface, digital_above
from engine.outcomes import build_markets
from ingestion.intel import fetch_headlines, fetch_news_volume, fetch_options, fetch_scoreboard
from ingestion.outcomes import fetch_outcome_meta


def show(title):
    print(f"\n=== {title} ===")


async def main() -> None:
    now = int(time.time() * 1000)
    show("Deribit options")
    for asset in ("BTC", "ETH", "SOL"):
        try:
            rows = await fetch_options(asset)
            surf = build_surface(asset, rows, now)
            print(f"{asset}: {len(rows)} options, {len(surf.smiles) if surf else 0} usable expiries")
            if surf:
                s = surf.smiles[min(1, len(surf.smiles) - 1)]
                f = s.forward
                print(f"  e.g. expiry {time.strftime('%d %b %H:%M', time.gmtime(s.expiry_ms / 1000))} forward {f:,.2f}, "
                      f"{len(s.points)} strikes, P(above +5%) = {digital_above(surf, f, f * 1.05, s.expiry_ms, now):.3f}")
            if rows:
                print("  first row:", json.dumps({k: rows[0].get(k) for k in ("instrument_name", "mark_iv", "underlying_price")}))
        except Exception as exc:
            print(f"{asset}: failed: {type(exc).__name__}: {exc}")

    show("ESPN scoreboards (today)")
    for sport, league in (("football", "nfl"), ("soccer", "uefa.champions"), ("soccer", "eng.1"), ("basketball", "nba")):
        try:
            games = await fetch_scoreboard(sport, league)
            print(f"{league}: {len(games)} games")
            for g in games[:2]:
                print("  ", json.dumps({k: g.get(k) for k in ("name", "state", "detail", "clock", "home_score", "away_score",
                                                               "odds", "win_prob")}, default=str)[:400])
        except Exception as exc:
            print(f"{league}: failed: {type(exc).__name__}: {exc}")

    show("GDELT news")
    q = '"Real Madrid" sourcelang:english'
    try:
        pts = await fetch_news_volume(q)
        print(f"volume: {len(pts)} points, last 2h {sum(v for t, v in pts if t > now - 7_200_000):.0f} articles")
    except Exception as exc:
        print(f"volume failed: {type(exc).__name__}: {exc}")
    await asyncio.sleep(6)
    try:
        arts = await fetch_headlines(q, 3)
        for a in arts:
            print("  ", a["title"][:100], "·", a["domain"])
    except Exception as exc:
        print(f"headlines failed: {type(exc).__name__}: {exc}")

    show("Hyperliquid prediction markets these sources can price")
    meta = await fetch_outcome_meta()
    markets, questions = build_markets(meta)
    svc = SimpleNamespace(markets=markets, questions=questions, ctx={}, quote=lambda m: (None, 0, None, 0, None),
                          underlyings=lambda: sorted({m.underlying for m in markets.values() if m.underlying}),
                          spot=lambda p: None)
    intel = IntelService(svc)
    targets = intel.sports_targets()
    comps = {}
    for t in targets:
        c = t["f"].get("competition") or "?"
        comps[c] = comps.get(c, 0) + 1
    print(f"{len(targets)} matches; competitions: {comps}")
    print("competitions with no ESPN league yet:", [c for c in comps if not league_for(c)])
    live = await intel.refresh_games()
    print(f"matched to ESPN games: {len(intel.matches)} of {len(targets)} (a game on now: {live})")
    unmatched = [t for t in targets if t["key"] not in intel.matches and league_for(t["f"].get("competition"))][:5]
    for t in unmatched:
        print("  not matched:", t["f"].get("participantA"), "vs", t["f"].get("participantB"), "·", t["f"].get("competition"),
              "·", t["start_ms"] and time.strftime("%d %b %H:%M", time.gmtime(t["start_ms"] / 1000)))
    prices = [m for m in markets.values() if m.kind in ("price", "touch")]
    print(f"price markets: {len(prices)}; on BTC/ETH/SOL (options-priced): "
          f"{sum(1 for m in prices if m.underlying in ('BTC', 'ETH', 'SOL'))}; underlyings: {sorted({m.underlying for m in prices})}")
    print("news queries:", len(intel.news_targets()), "e.g.", [t[1] for t in intel.news_targets()[:3]])


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

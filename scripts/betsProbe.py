"""
What the bets suite's server-side sources really return from this server (bash scripts/run.sh bets):
football-data's history and fixtures, The Odds API (with ANALYTIX_ODDS_API_KEY in ~/.analytix-env), and what the
live app has received from the laptop collector.
"""
import asyncio
import json
import os
import sys
import urllib.request

from engine import betting as B
from ingestion import football as F


async def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    print("1. football-data history (this takes a minute)")
    games, errors = await F.fetch_history(["E0", "SP1", "D1"], seasons=2)
    print(f"   {len(games)} games from 3 leagues x 2 seasons; errors: {errors[:4] or 'none'}")
    if games:
        with_ps = sum(1 for g in games if g.get("PSCH"))
        with_avg = sum(1 for g in games if g.get("AvgCH"))
        with_sot = sum(1 for g in games if g.get("hst") is not None)
        print(f"   with Pinnacle closing odds {with_ps}, market-average closing {with_avg}, shots on target {with_sot}")
        cal = B.calibration(games)
        for i, (lo, hi) in enumerate(B.BUCKETS):
            c = cal.get(("*", "1", i))
            if c:
                print(f"   home wins priced {1 / hi:.2f}-{1 / lo:.2f}: {c[0]} games, won {c[1] / c[0]:.1%}, prices said {c[2] / c[0]:.1%}")
    print("2. football-data coming games")
    try:
        fx = await F.fetch_fixtures()
        sh = F.sharp_from_fd(fx, 0)
        print(f"   {len(fx)} rows, {len(sh)} usable in our leagues; sources: {sorted({s['src'] for s in sh})}")
    except Exception as exc:                              # noqa: BLE001
        print(f"   failed: {type(exc).__name__}: {exc}")
    print("3. The Odds API")
    if not os.getenv("ANALYTIX_ODDS_API_KEY"):
        print("   no key yet: get a free one at the-odds-api.com, then add  export ANALYTIX_ODDS_API_KEY=...  to ~/.analytix-env")
    else:
        got, left, errs = await F.fetch_odds_api({"E0": F.LEAGUES["E0"]}, "odds")
        rows = F.parse_odds_api(got.get("E0") or [], "E0", "Premier League", 0)
        print(f"   Premier League: {len(rows)} games, sources {sorted({r['src'] for r in rows})}; credits left {left}; errors {errs or 'none'}")
    print("4. What the live app has received")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/bets", timeout=10) as r:
            d = json.loads(r.read())
        s = d["sources"]
        print(f"   books: {json.dumps(s['books'])}")
        print(f"   matched to a sharp line: {d['board_n']} prices; value now: {len(d['value'])}; history {s['history_games']} games")
        print(f"   status: {json.dumps({k: (v['ok'], v.get('error')) for k, v in s['status'].items()})}")
    except Exception as exc:                              # noqa: BLE001
        print(f"   the app didn't answer: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

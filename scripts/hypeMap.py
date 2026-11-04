"""
HYPE holder map, first version: crawl Hyperliquid's public info API for the wallets that hold HYPE, group them by
what they've done, and run the unstaking test (engine/hypemap.py has the groups and the pass rule).

    python -m scripts.hypeMap                            # about 2 hours at the default pace (it leaves room for the
                                                         # live app); a restart resumes from the cache
    python -m scripts.hypeMap --genesis 300 --traders 200   # a quick look (~15 min)

On the server, in the background (run scripts.hypeProbe first):
    cd ~/analytix && git pull && nohup sh -c 'ANALYTIX_STORE_DIR=$HOME/analytix-store nice -n 19 \\
        /opt/analytix/.venv/bin/python -m scripts.hypeMap' > /tmp/hype_map.txt 2> /tmp/hype_map.log &

Which wallets: the biggest genesis balances, every non-circulating (reserve) balance, the Assistance Fund, the busiest
HYPE traders in our recordings, anything in --labels, and Hypurrscan's holder list when it answers (--third-party).
For each: its spot balance, its staking summary, and for those with --history-min HYPE or more staked or leaving, its
staking history. The snapshot is saved (one file a day in --out-dir), so later runs can show who switched.
Labels: a JSON file of {address: "Binance"} etc., by hand, for exchanges and market makers the API can't name.
"""
import argparse
import asyncio
import json
import sys
import time
from collections import Counter, deque
from pathlib import Path

import httpx

from config import HL_INFO_URL
from engine import hypemap as H
from ingestion.backfill import fetch_candles
from scripts.hypeProbe import THIRD_PARTY, busiest_traders

WEIGHT = {"spotClearinghouseState": 2}                  # Hyperliquid's info weights; most calls are 20
DEFAULT_WEIGHT = 20


class Paced:
    """Info calls kept under `budget` weight a minute (Hyperliquid allows 1200 per IP; the live app needs some)."""

    def __init__(self, budget: int = 400, url: str = HL_INFO_URL, post=None):
        self.budget = budget
        self.url = url
        self.spent: deque = deque()
        self._post = post
        self.client = None if post else httpx.AsyncClient(timeout=30)

    async def __call__(self, body: dict):
        w = WEIGHT.get(body["type"], DEFAULT_WEIGHT)
        while True:
            now = time.monotonic()
            while self.spent and now - self.spent[0][0] > 60:
                self.spent.popleft()
            if sum(x for _, x in self.spent) + w <= self.budget:
                break
            await asyncio.sleep(max(0.05, 60 - (now - self.spent[0][0])))
        self.spent.append((time.monotonic(), w))
        for attempt in range(4):
            try:
                if self._post:
                    return await self._post(body)
                r = await self.client.post(self.url, json=body)
                if r.status_code == 429:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(3 * (attempt + 1))
        return None


class Cache:
    """One JSON line per (call, wallet), so a stopped crawl picks up where it was."""

    def __init__(self, path: Path):
        self.path = path
        self.got: dict[str, object] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    k, v = json.loads(line)
                    self.got[k] = v
                except ValueError:
                    continue
        self.fh = path.open("a")

    def __contains__(self, k):
        return k in self.got

    def put(self, k, v):
        self.got[k] = v
        self.fh.write(json.dumps([k, v]) + "\n")
        self.fh.flush()


async def crawl(info, wallets: list[str], cache: Cache, history_min: float, log=sys.stderr) -> dict[str, H.Wallet]:
    out = {a: H.Wallet(a) for a in wallets}
    steps = [("spot", "spotClearinghouseState"), ("summary", "delegatorSummary")]
    t0 = time.time()
    for key, call in steps:
        todo = [a for a in wallets if f"{key}:{a}" not in cache]
        for n, a in enumerate(todo):
            cache.put(f"{key}:{a}", await info({"type": call, "user": a}))
            if log and n % 250 == 249:
                print(f"  {key}: {n + 1}/{len(todo)} ({time.time() - t0:.0f}s)", file=log)
    for a, w in out.items():
        w.spot = H.spot_hype(cache.got.get(f"spot:{a}"))
        s = H.stake_of(cache.got.get(f"summary:{a}"))
        w.delegated, w.undelegated, w.pending = s["delegated"], s["undelegated"], s["pending"]
    deep = [a for a, w in out.items() if w.staked + w.pending >= history_min]
    todo = [a for a in deep if f"history:{a}" not in cache]
    for n, a in enumerate(todo):
        cache.put(f"history:{a}", await info({"type": "delegatorHistory", "user": a}))
        if log and n % 100 == 99:
            print(f"  history: {n + 1}/{len(todo)} ({time.time() - t0:.0f}s)", file=log)
    for a in deep:
        out[a].events = H.events_of(cache.got.get(f"history:{a}"))
    return out


def fmt(x: float) -> str:
    a = abs(x)
    return f"{x / 1e6:,.2f}M" if a >= 1e6 else f"{x / 1e3:,.1f}k" if a >= 1e3 else f"{x:,.0f}"


def report(wallets: list[H.Wallet], supply: float | None, closes: dict[int, float], now_ms: int, price: float | None,
           queue: dict | None = None) -> dict:
    found = sum(w.total for w in wallets)
    groups = H.groups(wallets, supply)
    conc = H.concentration(wallets, supply)
    flows = H.daily_flows(wallets)
    unl = H.unlocks(wallets, now_ms)
    test = H.spike_test(flows, closes)
    base = f"{supply:,.0f} circulating" if supply else "what was found"
    print(f"HYPE holder map · {time.strftime('%d %b %Y %H:%M UTC', time.gmtime(now_ms / 1000))} · {len(wallets):,} wallets "
          f"checked, {sum(1 for w in wallets if w.total > 0):,} hold HYPE · {fmt(found)} HYPE found"
          + (f" ({found / supply:.0%} of {base})" if supply else ""))
    if price:
        print(f"  HYPE ${price:,.2f}")
    print("\n1. Groups (by what they've done; shares of " + base + ")")
    print(f"  {'group':<28}{'wallets':>9}{'HYPE':>11}{'share':>8}{'staked':>9}{'leaving':>10}")
    for g in groups:
        print(f"  {g['group']:<28}{g['wallets']:>9,}{fmt(g['hype']):>11}{g['share']:>8.1%}"
              f"{(g['staked'] / g['hype'] if g['hype'] else 0):>9.0%}{fmt(g['pending']):>10}")
    tiers = Counter()
    tier_hype = Counter()
    for w in wallets:
        if w.total > 0 and H.group_of(w) not in ("Assistance Fund", "Not circulating"):
            tiers[H.tier_of(w.total)] += 1
            tier_hype[H.tier_of(w.total)] += w.total
    print("\n2. By size (Assistance Fund and reserves aside)")
    for _, name in H.TIERS:
        if tiers[name]:
            print(f"  {name:<12}{tiers[name]:>7,} wallets{fmt(tier_hype[name]):>11} HYPE")
    print("  biggest " + ", ".join(f"{n}: {v:.1%}" for n, v in conc.items()))
    staked = sum(w.staked for w in wallets)
    pending = sum(w.pending for w in wallets)
    print(f"\n3. Staking: {fmt(staked)} staked, {fmt(pending)} leaving (asked to unstake, spendable within 7 days)")
    for d, amt in unl:
        print(f"  {'date unknown' if d is None else time.strftime('%a %d %b', time.gmtime(d / 1000)):<14}{fmt(amt):>10} HYPE unlocks"
              + (f"  (${amt * price / 1e6:,.1f}M)" if price else ""))
    if queue and queue.get("n"):
        print(f"\n   Hypurrscan's whole unstaking queue: {queue['n']:,} withdrawals, {fmt(queue['hype'])} HYPE"
              + (f" (${queue['hype'] * price / 1e6:,.0f}M)" if price else "")
              + f"; its times read as {'when each was asked for' if queue['times_are'] == 'asked' else 'unlock times'}")
        for d, amt in queue["by_day"][:8]:
            print(f"  {time.strftime('%a %d %b', time.gmtime(d / 1000)):<14}{fmt(amt):>10} HYPE unlocks")
        for u, t, amt in queue.get("biggest", [])[:3]:
            print(f"  biggest: {u[:10]}… {fmt(amt)} HYPE, {time.strftime('%d %b %H:%M', time.gmtime(t / 1000))}")
    recent = list(flows.items())[-14:]
    if recent:
        print("\n4. Staking flows, last 14 days (HYPE): staked in / withdrawals started / finished")
        for d, f in recent:
            print(f"  {time.strftime('%a %d %b', time.gmtime(d / 1000)):<12}{fmt(f.get('stake', 0)):>10}"
                  f"{fmt(f.get('unstake_start', 0)):>10}{fmt(f.get('unstake_done', 0)):>10}")
    print("\n5. The test: days with many withdrawals started, then HYPE (decided before running)")
    if test.get("n_days", 0) < H.MIN_TRAIL + 5:
        print(f"  not enough history yet ({test.get('n_days', 0)} days with a threshold); rerun as days pass")
    else:
        print(f"  {test['n_spikes']} spike days of {test['n_days']}")
        print(f"  {'horizon':<10}{'after spikes':>14}{'other days':>12}{'t':>7}")
        for h in ("1d", "3d", "7d", "days 7-9"):
            r = test[h]
            print(f"  {h:<10}{r['spike']:>13.2f}%{r['other']:>11.2f}%{r['t']:>7.1f}")
        hv = test.get("halves_7d", [])
        print(f"  halves (7d, spikes minus other days): " + " / ".join(f"{x:+.2f}%" for x in hv))
        print(f"  verdict: {'PASSES' if test['passes'] else 'fails'} as a warning "
              "(needs the 7d move after spikes below other days, t <= -2, in both halves)")
    return {"groups": groups, "concentration": conc, "staked": staked, "pending": pending,
            "unlocks": [[d, a] for d, a in unl], "flows": flows, "test": test}


async def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)            # lines show as they're printed, even through tee
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--genesis", type=int, default=500, help="biggest genesis balances to check")
    ap.add_argument("--traders", type=int, default=300, help="busiest HYPE traders in the recordings to check")
    ap.add_argument("--holders", type=int, default=3000, help="biggest wallets from Hypurrscan's holder list to check")
    ap.add_argument("--history-min", type=float, default=10_000, help="HYPE staked or leaving to read a wallet's history")
    ap.add_argument("--budget", type=int, default=600, help="info weight a minute (Hyperliquid allows 1200 per IP)")
    ap.add_argument("--labels", default="", help="JSON {address: label}")
    ap.add_argument("--third-party", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--out-dir", default=str(Path.home() / "hype-holders"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    day = time.strftime("%Y%m%d", time.gmtime(now / 1000))
    info = Paced(args.budget)

    meta = await info({"type": "spotMeta"})
    tok = next((t for t in (meta or {}).get("tokens", []) if t.get("name") == "HYPE"), None)
    if not tok:
        sys.exit("spotMeta has no HYPE token: run scripts.hypeProbe and send its output")
    det = await info({"type": "tokenDetails", "tokenId": tok["tokenId"]}) or {}
    supply = H.num(det.get("circulatingSupply")) or None
    price = H.num(det.get("markPx")) or H.num(det.get("midPx")) or None
    genesis = sorted(((a, H.num(b)) for a, b in (det.get("genesis") or {}).get("userBalances") or []), key=lambda x: -x[1])
    reserves = {a.lower() for a, _ in det.get("nonCirculatingUserBalances") or []}
    labels = {k.lower(): v for k, v in json.loads(Path(args.labels).read_text()).items()} if args.labels else {}
    universe = [H.ASSISTANCE_FUND, *reserves, *(a for a, _ in genesis[:args.genesis]), *labels]
    traders = busiest_traders(args.traders)
    universe += traders
    queue = {"n": 0}
    if args.third_party:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(THIRD_PARTY[0])
                if r.status_code == 200:
                    got = H.parse_holders(r.json())[:args.holders]
                    universe += [a for a, _ in got]
                    print(f"Hypurrscan holder list: {len(got)} wallets added (biggest {got[0][1]:,.0f})" if got
                          else "Hypurrscan holder list: empty", file=sys.stderr)
                r = await c.get(THIRD_PARTY[1])
                if r.status_code == 200:
                    queue = H.parse_queue(r.json(), now)
                    universe += queue["users"]                     # whoever is leaving staking is worth a look
                    print(f"Hypurrscan unstaking queue: {queue['n']} withdrawals, {queue['hype']:,.0f} HYPE", file=sys.stderr)
        except Exception as exc:                                   # noqa: BLE001 - optional
            print(f"Hypurrscan: not used ({type(exc).__name__}: {exc})", file=sys.stderr)
    universe = list(dict.fromkeys(a.lower() for a in universe if a))
    print(f"{len(universe):,} wallets to check (genesis {min(args.genesis, len(genesis))}, reserves {len(reserves)}, "
          f"traders {len(traders)}, labels {len(labels)})", file=sys.stderr)

    cache = Cache(out_dir / f"cache_{day}.jsonl")
    found = await crawl(info, universe, cache, args.history_min)
    seen_trading = {a.lower() for a in traders}                   # the busiest HYPE traders in our recordings
    for a, w in found.items():
        w.label = labels.get(a)
        w.not_circulating = a in reserves
        w.traded_usd = 10_000.0 if a in seen_trading else 0.0
    rows = await fetch_candles("HYPE", "1d", 1000)                # since HYPE launched (staking history goes back to Dec 2024)
    closes = {int(r["t"]) // H.DAY_MS * H.DAY_MS: float(r["c"]) for r in rows if r.get("c")}
    res = report(list(found.values()), supply, closes, now, price, queue)
    snap = {"t_ms": now, "supply": supply, "price": price,
            "wallets": {a: {"spot": w.spot, "staked": w.staked, "pending": w.pending, "group": H.group_of(w)}
                        for a, w in found.items() if w.total > 0},
            **{k: v for k, v in res.items() if k != "flows"},
            "flows": {str(k): v for k, v in res["flows"].items()}}
    (out_dir / f"snapshot_{day}.json").write_text(json.dumps(snap, default=float))
    prev = sorted(p for p in out_dir.glob("snapshot_*.json") if p.stem != f"snapshot_{day}")
    if prev:
        old = json.loads(prev[-1].read_text())["wallets"]
        moved = Counter()
        for a, w in snap["wallets"].items():
            o = old.get(a)
            if o and o["group"] != w["group"]:
                moved[(o["group"], w["group"])] += 1
        if moved:
            print(f"\n6. Switched group since {prev[-1].stem[9:]}:")
            for (a, b), n in moved.most_common(10):
                print(f"  {a} → {b}: {n} wallets")
    print(f"\n(snapshot in {out_dir / f'snapshot_{day}.json'})", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())

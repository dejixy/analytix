"""
HYPE probe: which of Hyperliquid's public info calls answer for the holder map, in what shape, how far back, and how
fast. Read-only, about 40 requests, a minute or so. Run it once before scripts.hypeMap.

    cd ~/analytix && git pull && ANALYTIX_STORE_DIR=~/analytix-store /opt/analytix/.venv/bin/python -m scripts.hypeProbe

Prints a summary and saves every raw reply to /tmp/hype_probe.json (send the summary; the file only if asked).
Checks: the HYPE token (spotMeta, tokenDetails: supply, genesis and non-circulating balances), the validators, and
for a few wallets (the Assistance Fund, the biggest genesis and reserve balances, the busiest HYPE traders in our
recordings) their spot balance, staking summary, delegations, staking history and ledger. Optionally (--third-party)
a holder list and unstaking queue from Hypurrscan, a block explorer: unofficial, used only if it answers.
"""
import argparse
import asyncio
import json
import sys
import time
from collections import Counter

import httpx

from config import HL_INFO_URL, STORE_DIR
from engine import hypemap as H

THIRD_PARTY = ["https://api.hypurrscan.io/holders/HYPE", "https://api.hypurrscan.io/unstakingQueue"]


class Probe:
    def __init__(self, url: str = HL_INFO_URL):
        self.url = url
        self.raw: dict = {}
        self.client = httpx.AsyncClient(timeout=30)
        self.times: list[float] = []

    async def info(self, name: str, body: dict):
        t0 = time.perf_counter()
        try:
            r = await self.client.post(self.url, json=body)
            self.times.append(time.perf_counter() - t0)
            if r.status_code != 200:
                self.raw[name] = {"_status": r.status_code, "_body": r.text[:300]}
                return None
            j = r.json()
            self.raw[name] = j
            return j
        except Exception as exc:                                   # noqa: BLE001 - a probe reports, never stops
            self.raw[name] = {"_error": f"{type(exc).__name__}: {exc}"}
            return None
        finally:
            await asyncio.sleep(1.2)                               # well under the rate limit, beside the live app

    async def get(self, url: str):
        try:
            r = await self.client.get(url)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]
            self.raw[url] = body if r.status_code == 200 else {"_status": r.status_code}
            return r.status_code, body
        except Exception as exc:                                   # noqa: BLE001
            self.raw[url] = {"_error": str(exc)}
            return None, str(exc)


def shape(x, depth=0) -> str:
    """A short description of a reply: its keys, or its length and the first item's keys."""
    if isinstance(x, dict):
        return "{" + ", ".join(list(x)[:12]) + (", …" if len(x) > 12 else "") + "}"
    if isinstance(x, list):
        return f"list of {len(x)}" + (f", first: {shape(x[0], depth + 1)}" if x and depth < 1 else "")
    return type(x).__name__


def when(ms) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "–"


def busiest_traders(n: int) -> list[str]:
    """The biggest HYPE takers and makers in the newest days of the fast store (empty if there's no store)."""
    try:
        from storage import tickStore
        days = tickStore.days("HYPE", STORE_DIR)
        if not days:
            return []
        start = tickStore.day_end_ms(days[-1]) - 3 * H.DAY_MS
        z = tickStore.load("HYPE", ["trades"], start, None, STORE_DIR,
                           only=["trade_px", "trade_sz", "trade_buyer", "trade_seller"])
        usd = Counter()
        w = z["wallets"]
        for side in ("trade_buyer", "trade_seller"):
            for i, v in zip(z[side], z["trade_px"] * z["trade_sz"]):
                if i >= 0:
                    usd[str(w[i])] += float(v)
        return [a for a, _ in usd.most_common(n)]
    except Exception as exc:                                       # noqa: BLE001
        print(f"  (no recordings to pick traders from: {exc})")
        return []


async def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)            # lines show as they're printed, even through tee
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--third-party", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--out", default="/tmp/hype_probe.json")
    args = ap.parse_args()
    p = Probe()
    print("HYPE probe\n")

    meta = await p.info("spotMeta", {"type": "spotMeta"})
    tok = next((t for t in (meta or {}).get("tokens", []) if t.get("name") == "HYPE"), None)
    if not tok:
        sys.exit(f"spotMeta: no HYPE token ({shape(meta)})")
    print(f"1. spotMeta: HYPE is token {tok.get('index')}, id {tok.get('tokenId')}, keys {shape(tok)}")

    det = await p.info("tokenDetails", {"type": "tokenDetails", "tokenId": tok.get("tokenId")})
    genesis, reserves = [], []
    if det:
        print(f"2. tokenDetails: {shape(det)}")
        for k in ("maxSupply", "totalSupply", "circulatingSupply"):
            if k in det:
                print(f"     {k}: {H.num(det[k]):,.0f}")
        g = (det.get("genesis") or {}).get("userBalances") or []
        genesis = sorted(((a, H.num(b)) for a, b in g), key=lambda x: -x[1])
        reserves = sorted(((a, H.num(b)) for a, b in det.get("nonCirculatingUserBalances") or []), key=lambda x: -x[1])
        print(f"     genesis balances: {len(genesis):,} wallets, {sum(b for _, b in genesis):,.0f} HYPE; biggest "
              + ", ".join(f"{a[:8]}… {b:,.0f}" for a, b in genesis[:3]))
        print(f"     non-circulating: {len(reserves)} wallets, {sum(b for _, b in reserves):,.0f} HYPE; "
              + ", ".join(f"{a[:8]}… {b:,.0f}" for a, b in reserves[:3]))
        if "futureEmissions" in det:
            print(f"     future emissions: {H.num(det['futureEmissions']):,.0f}")
    else:
        print(f"2. tokenDetails: no answer ({p.raw.get('tokenDetails')})")

    vals = await p.info("validatorSummaries", {"type": "validatorSummaries"})
    if isinstance(vals, list):
        stake = sum(H.num(v.get("stake")) for v in vals)
        print(f"3. validatorSummaries: {len(vals)} validators, {stake / 1e8 if stake > 1e12 else stake:,.0f} staked "
              f"(raw sum {stake:,.0f}; units to check), keys {shape(vals[0]) if vals else '–'}")
    else:
        print(f"3. validatorSummaries: no answer ({p.raw.get('validatorSummaries')})")

    picks = [("Assistance Fund", H.ASSISTANCE_FUND)]
    picks += [(f"genesis #{i + 1}", a) for i, (a, _) in enumerate(genesis[:2])]
    picks += [(f"reserve #{i + 1}", a) for i, (a, _) in enumerate(reserves[:2])]
    picks += [(f"trader #{i + 1}", a) for i, a in enumerate(busiest_traders(3))]
    print(f"\n4. Wallets ({len(picks)}):")
    kinds_seen, ledger_types = Counter(), Counter()
    oldest = {}
    for name, a in picks:
        spot = await p.info(f"spot:{a}", {"type": "spotClearinghouseState", "user": a})
        summ = await p.info(f"summary:{a}", {"type": "delegatorSummary", "user": a})
        dels = await p.info(f"delegations:{a}", {"type": "delegations", "user": a})
        hist = await p.info(f"history:{a}", {"type": "delegatorHistory", "user": a})
        led = await p.info(f"ledger:{a}", {"type": "userNonFundingLedgerUpdates", "user": a, "startTime": 0})
        s = H.stake_of(summ)
        for row in hist or []:
            kinds_seen.update((row.get("delta") or {}).keys())
        for row in led or []:
            ledger_types[(row.get("delta") or {}).get("type")] += 1
        ev = H.events_of(hist, led)
        for src, rows in (("history", hist), ("ledger", led)):
            ts = [int(H.num(r.get("time"))) for r in rows or [] if r.get("time")]
            if ts:
                oldest[src] = min(oldest.get(src, ts[0]), min(ts))
        print(f"   {name:<16} {a[:10]}…  spot {H.spot_hype(spot):>14,.1f}  staked {s['delegated'] + s['undelegated']:>14,.1f}"
              f"  leaving {s['pending']:>12,.1f} ({s['n_pending']})  validators {len(dels) if isinstance(dels, list) else '–'}"
              f"  history {len(hist) if isinstance(hist, list) else '–'}  ledger {len(led) if isinstance(led, list) else '–'}"
              f"  staking events {len(ev)}")
    print(f"\n   delegatorHistory change kinds: {dict(kinds_seen) or '–'}")
    print(f"   ledger types: {dict(ledger_types) or '–'}")
    print(f"   oldest row: history {when(oldest.get('history'))}, ledger {when(oldest.get('ledger'))}")
    sample = next((r for k, v in p.raw.items() if k.startswith("history:") and isinstance(v, list) for r in v
                   if "withdrawal" in (r.get("delta") or {})), None)
    if sample:
        print(f"   a withdrawal row: {json.dumps(sample)[:300]}")
    sample = next((r for k, v in p.raw.items() if k.startswith("ledger:") and isinstance(v, list) for r in v
                   if (r.get("delta") or {}).get("type") == "cStakingTransfer"), None)
    if sample:
        print(f"   a cStakingTransfer row: {json.dumps(sample)[:300]}")

    if args.third_party:
        print("\n5. Hypurrscan (unofficial):")
        for u in THIRD_PARTY:
            st, body = await p.get(u)
            print(f"   {u}: {st} {shape(body) if st == 200 else str(body)[:120]}")
    if p.times:
        ts = sorted(p.times)
        print(f"\nReplies: {len(ts)}, median {ts[len(ts) // 2] * 1000:.0f} ms, slowest {ts[-1] * 1000:.0f} ms")
    with open(args.out, "w") as fh:
        json.dump(p.raw, fh)
    print(f"(raw replies in {args.out})")
    await p.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

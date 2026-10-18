"""
Vault study: does a Hyperliquid vault's track record repeat?

The vault page ranks vaults by recent APR. If the ones that did well last month keep doing well the month after,
following them could be an edge (and if the losers keep losing, fading them). This tests it walk-forward on every
vault's full history: at each month boundary, rank the vaults by their past month (or past 3 months, or how
steady the past 3 months were), then look at what each one did the next month. Only Hyperliquid's public vault
data is used; no recordings needed.

    returns   from each vault's all-time history (profit and account value): every step's profit over the account
              value before it, chained, so deposits and withdrawals don't count as returns
    alive     a vault counts at a boundary if it had at least MIN_ALIVE_USD in it and enough history behind it
    caveat    the vault list is filtered by today's size and age, so vaults that died small are missing (survivors)

    run    python -m scripts.vaultStudy [--min-tvl 10000] [--min-age 120] [--max 1000] [--refresh]
    cache  data/vaults/ (one file per vault, so a rerun is quick; --refresh fetches them again)
"""
import argparse
import json
import math
import time
from pathlib import Path

import httpx
import numpy as np

from config import HL_INFO_URL, ROOT

VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
CACHE = ROOT / "data" / "vaults"
DAY_MS = 86_400_000
MONTH_MS = 30 * DAY_MS
MIN_ALIVE_USD = 10_000
MIN_STEP_USD = 1_000          # a step's return needs this much in the vault before it, else it's skipped
PAUSE_S = 2.0                 # vaultDetails is a heavy request; the live app shares the rate limit
MIN_VAULTS = 20               # a boundary needs this many vaults to rank


# ── the data ─────────────────────────────────────────────────────────────────
def vault_list(client: httpx.Client) -> list[dict]:
    """Every vault: address, name, leader, tvl, created (ms), closed, relationship (normal / parent / child)."""
    try:
        r = client.get(VAULTS_URL, timeout=60)
        r.raise_for_status()
        raw = r.json()
    except (httpx.HTTPError, ValueError):
        r = client.post(HL_INFO_URL, json={"type": "vaultSummaries"}, timeout=60)
        r.raise_for_status()
        raw = r.json()
    return [v for v in (parse_summary(e) for e in raw or []) if v]


def parse_summary(e: dict) -> dict | None:
    s = e.get("summary") if isinstance(e, dict) and isinstance(e.get("summary"), dict) else e
    if not isinstance(s, dict) or not s.get("vaultAddress"):
        return None
    rel = s.get("relationship") or {}
    return {"address": s["vaultAddress"].lower(), "name": (s.get("name") or "").strip(), "leader": s.get("leader"),
            "tvl": _f(s.get("tvl")) or 0.0, "created": int(s.get("createTimeMillis") or 0),
            "closed": bool(s.get("isClosed")), "rel": rel.get("type", "normal") if isinstance(rel, dict) else "normal",
            "apr": _f(e.get("apr")) if isinstance(e, dict) else None}


def details(client: httpx.Client, addr: str, refresh: bool) -> dict | None:
    """vaultDetails, cut down to the all-time history, cached in data/vaults/."""
    path = CACHE / f"{addr}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    for attempt in range(4):
        r = client.post(HL_INFO_URL, json={"type": "vaultDetails", "vaultAddress": addr}, timeout=30)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        d = r.json() or {}
        keep = {"name": d.get("name"), "leader": d.get("leader"), "apr": d.get("apr"),
                "allTime": portfolio(d, "allTime"), "month": portfolio(d, "month")}
        path.write_text(json.dumps(keep))
        time.sleep(PAUSE_S)
        return keep
    return None


def portfolio(d: dict, period: str) -> dict:
    for item in d.get("portfolio") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == period:
            p = item[1] or {}
            return {"av": p.get("accountValueHistory") or [], "pnl": p.get("pnlHistory") or []}
    return {"av": [], "pnl": []}


def series(hist: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """→ (times ms, account value, cumulative profit) on shared times, sorted."""
    av = {int(t): float(v) for t, v in hist.get("av") or []}
    pnl = {int(t): float(v) for t, v in hist.get("pnl") or []}
    ts = sorted(set(av) & set(pnl))
    if len(ts) < 3:
        return None
    return np.array(ts, dtype=np.int64), np.array([av[t] for t in ts]), np.array([pnl[t] for t in ts])


# ── returns ──────────────────────────────────────────────────────────────────
def steps(t: np.ndarray, av: np.ndarray, pnl: np.ndarray) -> np.ndarray:
    """Each step's return: profit made in it over the account value before it (0 when the vault was near empty)."""
    base = av[:-1]
    r = np.where(base >= MIN_STEP_USD, np.diff(pnl) / np.maximum(base, 1e-9), 0.0)
    return np.clip(r, -1.0, 10.0)


def value_at(t: np.ndarray, av: np.ndarray, at: int) -> float:
    i = np.searchsorted(t, at, side="right") - 1
    return float(av[i]) if i >= 0 else 0.0


def period_return(t: np.ndarray, r: np.ndarray, a: int, b: int) -> float:
    """Chained return of the steps ending in (a, b]. A vault that closed before b keeps what it had made."""
    ends = t[1:]
    use = (ends > a) & (ends <= b)
    return float(np.prod(1 + r[use]) - 1)


def steadiness(t: np.ndarray, r: np.ndarray, a: int, b: int) -> float | None:
    ends = t[1:]
    x = r[(ends > a) & (ends <= b)]
    if len(x) < 5 or x.std() == 0:
        return None
    return float(x.mean() / x.std())


# ── the test ─────────────────────────────────────────────────────────────────
def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def persistence(vaults: dict[str, tuple], now_ms: int, lookback_ms: int, how: str = "return") -> dict:
    """At each boundary (every 30 days back from a month ago): rank the alive vaults by their past (`how`: return
    over lookback, or steadiness), then compare with their next month. → per-boundary results and the summary."""
    first = min(v[0][0] for v in vaults.values())
    months = []
    T = now_ms - MONTH_MS
    while T - lookback_ms >= first:
        past, nxt = [], []
        for t, av, pnl, r in vaults.values():
            if t[0] > T - lookback_ms or value_at(t, av, T) < MIN_ALIVE_USD:
                continue
            p = period_return(t, r, T - lookback_ms, T) if how == "return" else steadiness(t, r, T - lookback_ms, T)
            if p is None:
                continue
            past.append(p)
            nxt.append(period_return(t, r, T, T + MONTH_MS))
        if len(past) >= MIN_VAULTS:
            past, nxt = np.array(past), np.array(nxt)
            k = max(2, len(past) // 10)
            order = np.argsort(past)
            top, bottom = nxt[order[-k:]], nxt[order[:k]]
            months.append({"T": T, "n": len(past), "corr": spearman(past, nxt), "all": float(np.median(nxt)),
                           "top": float(np.median(top)), "bottom": float(np.median(bottom)),
                           "top_up": float((top > 0).mean())})
        T -= MONTH_MS
    return {"months": months, **summary(months)}


def summary(months: list[dict]) -> dict:
    if len(months) < 3:
        return {"m": len(months)}
    corr = np.array([m["corr"] for m in months])
    top = np.array([m["top"] - m["all"] for m in months])
    bot = np.array([m["bottom"] - m["all"] for m in months])
    t = lambda x: float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))) if x.std(ddof=1) > 0 else 0.0
    return {"m": len(months), "n": float(np.mean([m["n"] for m in months])), "corr": float(corr.mean()), "corr_t": t(corr),
            "top": float(top.mean()), "top_t": t(top), "bottom": float(bot.mean()), "bottom_t": t(bot),
            "top_beat": int((top > 0).sum()), "top_up": float(np.mean([m["top_up"] for m in months]))}


# ── run ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tvl", type=float, default=10_000)
    ap.add_argument("--min-age", type=float, default=120, help="days")
    ap.add_argument("--max", type=int, default=1000)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)

    with httpx.Client() as client:
        allv = vault_list(client)
        pick = [v for v in allv if v["rel"] != "child" and v["created"] and now - v["created"] >= a.min_age * DAY_MS
                and v["tvl"] >= a.min_tvl]
        pick.sort(key=lambda v: -v["tvl"])
        pick = pick[: a.max]
        print(f"{len(allv)} vaults listed; {len(pick)} with ${a.min_tvl:,.0f}+ and {a.min_age:.0f}+ days "
              f"({sum(v['closed'] for v in allv)} listed as closed)", flush=True)
        vaults, info, gaps = {}, {}, []
        for i, v in enumerate(pick):
            try:
                d = details(client, v["address"], a.refresh)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"  {v['address']}: {exc}", flush=True)
                continue
            if i and i % 100 == 0:
                print(f"  fetched {i}/{len(pick)}", flush=True)
            s = series((d or {}).get("allTime") or {})
            if s is None:
                continue
            t, av, pnl = s
            vaults[v["address"]] = (t, av, pnl, steps(t, av, pnl))
            info[v["address"]] = v
            gaps.append(float(np.median(np.diff(t))) / DAY_MS)

    if len(vaults) < MIN_VAULTS:
        print(f"Only {len(vaults)} vaults with history; not enough to test.")
        return
    gap = float(np.median(gaps))
    print(f"\n{len(vaults)} vaults with history. Points in each history: about every {gap:.1f} days"
          + ("  (coarse: monthly results are rough)" if gap > 7 else ""))

    print("\nDoes the past predict the next month? Walk-forward, a boundary every 30 days.")
    print("  rank match = how well the past order matches next month's order (0 = not at all)")
    print("  top / bottom 10% = their median next month minus the median vault's, in % points\n")
    print(f"{'ranked by':<26} {'months':>6} {'vaults':>6} {'rank match (t)':>16} {'top 10% (t)':>15} "
          f"{'top beat':>9} {'top +':>6} {'bottom 10% (t)':>16}")
    for label, lb, how in (("past month's return", MONTH_MS, "return"), ("past 3 months' return", 3 * MONTH_MS, "return"),
                           ("past 3 months, steadiness", 3 * MONTH_MS, "steady")):
        s = persistence(vaults, now, lb, how)
        if s["m"] < 3:
            print(f"{label:<26} {s['m']:>6}  (not enough months)")
            continue
        print(f"{label:<26} {s['m']:>6} {s['n']:>6.0f} {s['corr']:>+9.2f} ({s['corr_t']:>+4.1f}) "
              f"{s['top'] * 100:>+8.1f} ({s['top_t']:>+4.1f}) {s['top_beat']:>5}/{s['m']:<3} {s['top_up']:>6.0%} "
              f"{s['bottom'] * 100:>+9.1f} ({s['bottom_t']:>+4.1f})")

    print("\nThe top 10 by the last 30 days, with how often their past months were positive:")
    rows = []
    for addr, (t, av, pnl, r) in vaults.items():
        if value_at(t, av, now) < MIN_ALIVE_USD:
            continue
        last = period_return(t, r, now - MONTH_MS, now)
        T, ups, tot = now - MONTH_MS, 0, 0
        while T - MONTH_MS >= t[0]:
            ups += period_return(t, r, T - MONTH_MS, T) > 0
            tot += 1
            T -= MONTH_MS
        rows.append((last, addr, ups, tot))
    for last, addr, ups, tot in sorted(rows, reverse=True)[:10]:
        v = info[addr]
        print(f"  {v['name'][:28]:<28} {addr[:6]}…{addr[-4:]}  {last * 100:>+6.1f}% last 30d · ${v['tvl'] / 1e6:>5.2f}M · "
              f"{(now - v['created']) / DAY_MS:>4.0f} days · {ups}/{tot} past months up")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()

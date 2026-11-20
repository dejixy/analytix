"""
Sentiment Check's data: the long history downloaded or topped up (ingestion/longhist.py), then the tables built from
it (engine/sentiment.py): five years of 1-minute candles, open interest and funding, three years of 1-second candles,
for BTC, ETH, SOL and HYPE.

    python -m scripts.sentimentData                       download what's new, then build every table (about an hour)
    python -m scripts.sentimentData --daily               download what's new and refresh the 30-day normals; the
                                                          tables are rebuilt too when they're over a week old
    python -m scripts.sentimentData --build-only          rebuild the tables from what's stored
    python -m scripts.sentimentData --coins BTC --kinds m1

On the server: bash ~/analytix/scripts/run.sh sentiment (in the background, as the app's user). Tables go to
data/sentiment/<coin>.npz with <coin>.json beside them (the normals the live read needs, the checks, the dates).
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

from engine import releases as RL
from engine import sentiment as S
from ingestion import longhist as L

OUT = L.ROOT / "data" / "sentiment"
QUIET_S = 60                     # a 1-second gap this short is a second with no trades, not a feed outage
TAIL_S = 3600                    # carried from one month into the next, so its first minutes have a past
REBUILD_DAYS = 7


def _say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _months(coin: str, kind: str, folder: Path) -> list[str]:
    d = folder / coin / kind
    return sorted({p.stem[:7] for p in d.glob("*.npz")}) if d.exists() else []


def _load_month(coin: str, kind: str, folder: Path, month: str) -> dict:
    """One month's candles: the month's file, or its days, joined in time order."""
    d = folder / coin / kind
    parts = [L.unpack(dict(np.load(p))) for p in sorted(d.glob(f"{month}*.npz"))]
    if not parts:
        return {}
    j = {k: np.concatenate([x[k] for x in parts if k in x]) for k in parts[0]}
    order = np.argsort(j["t"], kind="stable")
    j = {k: v[order] for k, v in j.items()}
    keep = np.concatenate([[True], np.diff(j["t"]) > 0])
    return {k: v[keep] for k, v in j.items()}


def _grid(d: dict, step: int, quiet_s: int = 0) -> S.Series:
    tbv = d.get("tbv", np.full(len(d["t"]), np.nan))
    return S.to_grid(d["t"], d["c"], d["h"], d["l"], d["v"], tbv, step, quiet_s)


def _slow(coin: str, folder: Path) -> tuple[dict, dict]:
    return L.load(coin, "oi", folder), L.load(coin, "funding", folder)


def build_long(coin: str, folder: Path, rel: np.ndarray, say=_say) -> tuple[dict, dict, dict]:
    """The 5-minute to 24-hour tables from the 1-minute candles: (tables, checks, family info)."""
    m1 = L.load(coin, "m1", folder)
    if not m1 or len(m1["t"]) < 3 * 1440:
        return {}, {}, {}
    oi, fund = _slow(coin, folder)
    s = _grid(m1, 60)
    r = S.raw(s, short=False)
    norms = S.Normals()
    norms.add(s, r)
    p = S.prepare(s, r, norms, oi, fund)
    tables, checks = {}, {}
    for h, sec in S.HORIZONS.items():
        if h in S.SHORT:
            continue
        start, ret, dip, rise = S.outcomes(p, sec)
        learner = S.Learner(h)
        learner.add(S.bins_at(p, start, sec, rel), s.t[start], ret, dip, rise)
        if learner.table is not None:
            tables[h], checks[h] = S.prune(learner.table), learner.check()
            say(f"  {coin} {h}: {len(ret):,} stretches, check {_fmt(checks[h])}")
    info = {"from": int(s.t[0]), "to": int(s.t[-1]), "normals": norms.to_json(), "funding": bool(fund), "oi": bool(oi)}
    return tables, checks, info


def build_short(coin: str, folder: Path, rel: np.ndarray, say=_say) -> tuple[dict, dict, dict]:
    """The 10-second to 2-minute tables from the 1-second candles, a month at a time."""
    months = _months(coin, "s1", folder)
    if not months:
        return {}, {}, {}
    oi, fund = _slow(coin, folder)
    norms = S.Normals()
    learners = {h: S.Learner(h) for h in S.SHORT}
    tail, first_t, last_t = None, None, None
    for month in months:
        d = _load_month(coin, "s1", folder, month)
        if not d:
            continue
        since = int(d["t"][0])
        if tail is not None and int(tail["t"][-1]) < since:
            d = {k: np.concatenate([tail[k], d[k]]) for k in d if k in tail}
        s = _grid(d, 1, QUIET_S)
        r = S.raw(s, short=True)
        norms.add(s, r, since_s=since)
        p = S.prepare(s, r, norms, oi, fund)
        first = int(np.searchsorted(s.t, since))
        for h in S.SHORT:
            sec = S.HORIZONS[h]
            start, ret, dip, rise = S.outcomes(p, sec, first=first)
            learners[h].add(S.bins_at(p, start, sec, rel), s.t[start], ret, dip, rise)
        keep = d["t"] >= d["t"][-1] - TAIL_S
        tail = {k: v[keep] for k, v in d.items()}
        first_t = since if first_t is None else first_t
        last_t = int(s.t[-1])
        say(f"  {coin} 1-second {month}: {len(s):,} seconds")
    tables, checks = {}, {}
    for h, lr in learners.items():
        if lr.table is not None:
            tables[h], checks[h] = S.prune(lr.table), lr.check()
            say(f"  {coin} {h}: check {_fmt(checks[h])}")
    return tables, checks, {"from": first_t, "to": last_t, "normals": norms.to_json()}


def _fmt(c: dict) -> str:
    if not c.get("n"):
        return "no later years to test on"
    f = lambda x: "-" if x is None else f"{x * 100:+.1f}%"       # noqa: E731
    return f"{c['n']:,} cases; skill higher {f(c['up'])}, dips {f(c['dip'])}, rises {f(c['rise'])}"


def build(coin: str, folder: Path = L.FOLDER, out: Path = OUT, say=_say) -> bool:
    rel = RL.times_s()
    t0 = time.time()
    lt, lc, li = build_long(coin, folder, rel, say)
    st, sc, si = build_short(coin, folder, rel, say)
    tables = {**st, **lt}
    if not tables:
        say(f"{coin}: nothing stored to build from")
        return False
    out.mkdir(parents=True, exist_ok=True)
    S.save_tables(out / f"{coin}.npz", tables)
    S.save_json(out / f"{coin}.json", {"coin": coin, "built": int(time.time()), "long": li, "short": si,
                                       "checks": {**sc, **lc}})
    say(f"{coin}: {len(tables)} horizons built in {(time.time() - t0) / 60:.0f} min")
    return True


def _last_s(coin: str, kind: str, folder: Path) -> int:
    """The last stored time of a kind (seconds), from its newest file."""
    files = sorted((folder / coin / kind).glob("*.npz"), key=lambda p: p.stem)
    if not files:
        return 0
    t = np.cumsum(np.load(files[-1])["dt"])
    return int(t[-1]) * (1 if kind == "s1" else 60) if len(t) else 0


def refresh_normals(coin: str, folder: Path = L.FOLDER, out: Path = OUT, say=_say) -> bool:
    """The 30-day normals the live read uses, from the last few weeks stored (quick; the tables stay as they are)."""
    path = out / f"{coin}.json"
    if not path.exists():
        return False
    import json
    meta = json.loads(path.read_text())
    for fam, kind, step, short, quiet in (("long", "m1", 60, False, 0), ("short", "s1", 1, True, QUIET_S)):
        if not meta.get(fam):
            continue
        d = L.load(coin, kind, folder, since_s=_last_s(coin, kind, folder) - (S.NORMAL_DAYS + 10) * 86400)
        if not d or not len(d.get("t", [])):
            continue
        s = _grid(d, step, quiet)
        norms = S.Normals()
        norms.add(s, S.raw(s, short))
        meta[fam]["normals"] = norms.to_json()
        meta[fam]["to"] = int(s.t[-1])
    meta["normals_at"] = int(time.time())
    S.save_json(path, meta)
    say(f"{coin}: normals refreshed")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--kinds", default="m1,oi,funding,s1")
    ap.add_argument("--build-only", action="store_true", help="skip the download")
    ap.add_argument("--download-only", action="store_true", help="skip the build")
    ap.add_argument("--daily", action="store_true", help="download, refresh normals; rebuild tables over a week old")
    args = ap.parse_args()
    t0 = time.time()
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    if not args.build_only:
        _say(f"Sentiment Check history into {L.FOLDER}")
        L.update(coins, tuple(k.strip() for k in args.kinds.split(",") if k.strip()), say=_say)
    if not args.download_only:
        for coin in coins:
            path = OUT / f"{coin}.npz"
            if args.daily and path.exists() and time.time() - path.stat().st_mtime < REBUILD_DAYS * 86400:
                refresh_normals(coin)
                continue
            _say(f"{coin}: building the tables")
            try:
                build(coin)
            except MemoryError:
                _say(f"{coin}: ran out of memory while building")
    _say(f"done in {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    sys.exit(main())

"""
Builds the informed basket's wallet scores from the server's recordings and writes them as JSON. The bot runs this in
its own low-priority process every few hours (api/informed.py), so the heavy reading never touches the dashboard.

    python -m scripts.informedScores --out data/informed_scores.json
    python -m scripts.informedScores --days 14 --out /tmp/scores.json

A wallet's score: what price did its way 15 minutes after each of its hidden orders (engine/informed.py), averaged and
shrunk toward zero by how few orders it has (score = n × mean / (n + k), k from the data). Negative = its pushes
reverse: those are the wallets the basket fades (study 2, scripts/fadeStudy.py).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from config import RECORD_DIR
from engine.informed import follow, shrink_k
from ingestion.replay import _first_recv, recording_files
from scripts.informedStudy import load

HOLD_S = 900
LAG_S = 6


def build(paths, days: float, coins: set[str], min_usd: float = 50_000) -> dict:
    files = recording_files(paths)
    if days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - days * 86_400_000]
    out = {"built_ms": int(time.time() * 1000), "files": len(files), "orders": 0, "k": None, "hold_s": HOLD_S,
           "wallets": {}}
    if not files:
        return out
    g, orders, _ = load(files, coins, min_usd)
    if not orders:
        return out
    wl = {}
    W = np.array([wl.setdefault(h.wallet, len(wl)) for h in orders])
    v = np.full(len(orders), np.nan)
    for c in {h.coin for h in orders}:
        k = np.array([i for i, h in enumerate(orders) if h.coin == c])
        t = np.array([orders[i].t_detect for i in k], dtype=np.int64)
        s = np.array([orders[i].side for i in k])
        v[k], _ = follow(g[c], t, s, LAG_S, HOLD_S, 200, c)
    ok = np.isfinite(v)
    k = shrink_k(W[ok], v[ok])
    n = np.bincount(W[ok], minlength=len(wl)).astype(float)
    tot = np.bincount(W[ok], weights=v[ok], minlength=len(wl))
    wallets = {}
    for w, i in wl.items():
        if n[i] >= 1:
            mean = tot[i] / n[i]
            wallets[w] = {"n": int(n[i]), "mean": round(float(mean), 2), "score": round(float(n[i] * mean / (n[i] + k)), 2)}
    span = max(len(gr) for gr in g.values()) / 3600
    out.update(orders=int(ok.sum()), k=round(k, 1), hours=round(span, 1), wallets=wallets)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    res = build(a.paths, a.days, {c.strip().upper() for c in a.coins.split(",") if c.strip()})
    tmp = Path(a.out).with_suffix(".tmp")
    tmp.write_text(json.dumps(res), encoding="utf-8")
    tmp.replace(a.out)
    print(f"{len(res['wallets'])} wallets scored from {res['orders']:,} hidden orders", file=sys.stderr)


if __name__ == "__main__":
    main()

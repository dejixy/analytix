"""
Builds the live bot's big-mover wallet list from the server's recordings and writes it as JSON. The bot runs this
in its own low-priority process every hour (api/bot.py), so the heavy reading never touches the dashboard.

    python -m scripts.bigMoversList --out data/bigmovers.json
    python -m scripts.bigMoversList --days 7 --cutoff 8 --t-min 2.5 --min-n 20 --out /tmp/list.json

A big mover is a wallet whose aggressive trades ($1k+ decisions) were followed by an average move of `cutoff`+ bps
its way within 60 seconds, with a t-stat of t-min+ over min-n+ decisions (the parameters the walk-forward test in
scripts/bigMovers.py settled on). Only decisions whose 60 seconds had fully passed count.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from config import RECORD_DIR
from engine.bigmovers import Arrays, markouts, pick, scores
from engine.walletstudy import Marks
from ingestion.replay import _first_recv, recording_files
from scripts.bigMovers import load


def build(paths, days: float, coins: set[str], min_usd: float, score_h: int, min_n: int, t_min: float,
          cutoff: float, progress: bool = False) -> dict:
    files = recording_files(paths)
    if days and files:
        newest = max((_first_recv(p) or 0) for p in files)
        files = [p for p in files if (_first_recv(p) or 0) >= newest - days * 86_400_000]
    out = {"built_ms": int(time.time() * 1000), "files": len(files), "hours": 0.0, "decisions": 0, "wallets": [],
           "params": {"days": days, "coins": sorted(coins), "min_usd": min_usd, "score_h": score_h, "min_n": min_n,
                      "t_min": t_min, "cutoff": cutoff}}
    if not files:
        return out
    g, eps = load(files, coins, min_usd, progress=progress)
    if not eps:
        return out
    a = Arrays.of(eps)
    mo = markouts(a, {c: Marks(gr) for c, gr in g.items()}, score_h)
    sc = scores(a, mo, int(a.t.max()) + 1, score_h)
    ids = pick(sc, min_n, t_min, cutoff)
    rows = []
    for w in ids:
        mine = a.wallet == w
        coin_n = np.bincount(a.coin[mine], minlength=len(a.coins))
        rows.append({"wallet": a.wallets[w], "n": int(sc["n"][w]), "mean_bps": round(float(sc["mean"][w]), 2),
                     "t": round(float(sc["t"][w]), 2), "usd": round(float(np.median(a.usd[mine])), 0),
                     "coins": [a.coins[c] for c in np.argsort(-coin_n) if coin_n[c]],
                     "last_ms": int(a.t[mine].max())})
    rows.sort(key=lambda r: -r["mean_bps"])
    out.update(hours=round((int(a.t.max()) - int(a.t.min())) / 3.6e6, 1), decisions=len(eps), wallets=rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--min-usd", type=float, default=1000)
    ap.add_argument("--score-h", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--t-min", type=float, default=2.5)
    ap.add_argument("--cutoff", type=float, default=8.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        os.nice(19)                    # stay out of the dashboard's way
    except (AttributeError, OSError):
        pass
    coins = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    res = build(a.paths, a.days, coins, a.min_usd, a.score_h, a.min_n, a.t_min, a.cutoff)
    tmp = Path(a.out).with_suffix(".tmp")
    tmp.write_text(json.dumps(res), encoding="utf-8")
    tmp.replace(a.out)                 # the bot never reads a half-written list
    print(f"{len(res['wallets'])} big movers from {res['decisions']:,} decisions over {res['hours']} h", file=sys.stderr)


if __name__ == "__main__":
    main()

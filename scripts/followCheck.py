"""
Follow check: the live informed basket settles every hidden order it spots (bot_fades in the bars SQLite), so it
can also show whether FOLLOWING the "right" wallets works, with no trades and no UI. Read it here.

    fade        score -5 or worse, the other side (what the basket trades)
    follow      score +5 or better, the same side as the wallet
    every       every hidden order, faded (the control)
    no score    wallets with no history yet, faded

All in bps at the mid: in 6s after spotting, out 15 min later. t is from single orders (they can overlap, so it
reads a little high); "days +" counts days where the group made money.

    run    python -m scripts.followCheck [--days 30]
"""
import argparse
import json
import math
import sqlite3
import time
from collections import defaultdict

from config import BARS_DB

MAKER_RT_BPS = 3.0


def load(db_path, since_ms: int) -> list[dict]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT data FROM bot_fades WHERE t_ms >= ? ORDER BY t_ms", (since_ms,)).fetchall()
    finally:
        db.close()
    return [r for r in (json.loads(d) for (d,) in rows) if r.get("result")]


def groups(recs: list[dict], fade_at: float = -5.0, follow_at: float = 5.0) -> dict[str, list[tuple[int, float]]]:
    """→ group name: [(t_ms, bps your way)]. A fade's bps is the result as saved; following is its opposite."""
    out = defaultdict(list)
    for r in recs:
        b, s, t = r["result"]["bps"], r.get("score"), r["t_ms"]
        out["every"].append((t, b))
        if s is None:
            out["no score"].append((t, b))
        elif s <= fade_at:
            out["fade"].append((t, b))
        elif s >= follow_at:
            out["follow"].append((t, -b))
    return out


def stats(xs: list[tuple[int, float]]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0}
    v = [b for _, b in xs]
    mean = sum(v) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    days = defaultdict(float)
    for t, b in xs:
        days[t // 86_400_000] += b
    return {"n": n, "bps": mean, "t": mean / (sd / math.sqrt(n)) if sd else 0.0, "win": sum(x > 0 for x in v) / n,
            "maker": mean - MAKER_RT_BPS, "days_up": sum(x > 0 for x in days.values()), "days": len(days)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    a = ap.parse_args()
    recs = load(BARS_DB, int((time.time() - a.days * 86_400) * 1000))
    if not recs:
        print("No settled hidden orders yet.")
        return
    span = (recs[-1]["t_ms"] - recs[0]["t_ms"]) / 3.6e6
    print(f"Live informed basket: {len(recs)} settled hidden orders over {span:.0f} h\n")
    print(f"{'group':<10} {'n':>6} {'bps (t)':>14} {'win':>5} {'after limit fees':>17} {'days +':>7}")
    g = groups(recs)
    for k in ("fade", "follow", "every", "no score"):
        s = stats(g.get(k, []))
        if not s["n"]:
            print(f"{k:<10} {0:>6}")
            continue
        print(f"{k:<10} {s['n']:>6} {s['bps']:>+7.1f} ({s['t']:>+4.1f}) {s['win']:>5.0%} {s['maker']:>+17.1f} "
              f"{s['days_up']:>4}/{s['days']}")
    print("\nIn the study: fade +7.5 (t 4.0), follow about -0.6, every order +1.1.")


if __name__ == "__main__":
    main()

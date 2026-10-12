"""
How good each independent estimate has been, scored on prediction markets that have settled.

At 24h, 6h and 1h before a market closes (the first time it's seen inside each, one moment never counted twice),
the service saves the market's estimate (its source and the chance it gave Yes), the market's own odds and the
best bid and ask. Once the market settles:

    accuracy   Brier score (mean squared error of a chance against what happened; 0 is perfect) for the source,
               and for the market's own odds at the same moments. Skill = 1 − source ÷ market: above zero, the
               source has been closer to what happened than Hyperliquid's price was.
    edges      every time the source said a side was worth +3% or more at the price on offer: what buying it
               returned per $1, and how often it won.

A source needs MIN_MARKETS settled markets for a verdict: "beats" the market or "worse" than it. Until then it's
"unproven". Sources that are other markets' prices (options, sportsbooks, ESPN's live model) are shown while
unproven; a model of our own (the perp's moves, the goals model) is shown but doesn't alert until it has a record.
A source that has been worse than the market is hidden from the edge board and never alerts.
"""
import math
from collections import defaultdict

from engine.sharp import SNAP_BEFORE

MIN_MARKETS = 15
EDGE_EV = 0.03
TRUSTED_UNPROVEN = {"options", "book", "espn"}
NOT_SCORED = {"final"}              # a final score isn't a forecast


def _edge_return(p: float, bid: float | None, ask: float | None, y: int) -> float | None:
    """What following the estimate returned per $1, if it saw an edge at the prices then; None if it saw none."""
    if ask is not None and 0 < ask < 1 and p / ask - 1 >= EDGE_EV:
        return y / ask - 1
    if bid is not None and 0 < bid < 1 and (1 - p) / (1 - bid) - 1 >= EDGE_EV:
        return (1 - y) / (1 - bid) - 1
    return None


def score(results) -> dict[str, dict]:
    """results: (outcome id, snaps, 1|0) with snaps["ref:6h"] = {"p", "src", "m", "bid", "ask"} → per source."""
    obs = defaultdict(list)
    for oid, snaps, y in results:
        for h in SNAP_BEFORE:
            r = (snaps or {}).get(f"ref:{h}")
            if isinstance(r, dict) and r.get("p") is not None and r.get("m") is not None and r.get("src") not in NOT_SCORED:
                obs[r["src"]].append((oid, float(r["p"]), float(r["m"]), r.get("bid"), r.get("ask"), int(y)))
    out = {}
    for src, rows in obs.items():
        n = len(rows)
        markets = len({o for o, *_ in rows})
        brier = sum((p - y) ** 2 for _, p, _, _, _, y in rows) / n
        brier_mkt = sum((m - y) ** 2 for _, _, m, _, _, y in rows) / n
        skill = 1 - brier / brier_mkt if brier_mkt > 0 else None
        rets = [r for r in (_edge_return(p, b, a, y) for _, p, _, b, a, y in rows) if r is not None]
        ret = sum(rets) / len(rets) if rets else None
        t = None
        if len(rets) >= 5:
            sd = math.sqrt(sum((x - ret) ** 2 for x in rets) / (len(rets) - 1))
            t = ret / (sd / math.sqrt(len(rets))) if sd > 0 else None
        verdict = "unproven" if markets < MIN_MARKETS or skill is None else "beats" if skill > 0 else "worse"
        out[src] = {"source": src, "checks": n, "markets": markets, "brier": brier, "brier_market": brier_mkt,
                    "skill": skill, "edges": len(rets), "edge_return": ret, "edge_won": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
                    "edge_t": t, "verdict": verdict}
    return out


def gate(source: str | None, card: dict[str, dict]) -> str:
    """"show" (on the board and alerts), "quiet" (on the board marked unproven, no alerts) or "hide"."""
    if source in NOT_SCORED:
        return "show"
    rec = card.get(source or "")
    verdict = rec["verdict"] if rec else "unproven"
    if verdict == "worse":
        return "hide"
    if verdict == "beats" or source in TRUSTED_UNPROVEN:
        return "show"
    return "quiet"

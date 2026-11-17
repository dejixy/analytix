"""
The bet rules replayed on three seasons of football-data's games (/bets "The rules on three seasons").

There is no history of Nigerian books' prices, so two stand-ins play the soft book:

    bet365   one big soft bookmaker's price (B365H/A), read when football-data read Pinnacle's (PSH/A)
    best     the best price of every book it follows (MaxH/A): an upper bound, kinder than any one Nigerian book

The fair chance is Pinnacle's price at that same moment, the margin taken out by the method in use (engine/betting.py).
Only home and away wins can be replayed (football-data has no double-chance prices). The history adjustment is
walk-forward: a season is adjusted only by the seasons before it, and each team's form only uses earlier games, so no
rule sees its own result. Each rule reports its bets, how many won, the return on a flat stake with its standard error,
and how often the price beat Pinnacle's close (the sign an edge is real).

The rollover replay takes one pick a day by the live rule and follows it in runs from one loss to the next.
"""
import math
from datetime import datetime, timezone

from engine import betting as B

SOFT = {"bet365": ("B365H", "B365A"), "best": ("MaxH", "MaxA")}
SHARP = ("PSH", "PSD", "PSA")
CLOSE = ("PSCH", "PSCD", "PSCA")
TOP = {"E0", "SP1", "I1", "D1", "F1"}
WAT = 3600_000                                            # Lagos is an hour ahead of UTC


def season(t_ms: int) -> int:
    d = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
    return d.year if d.month >= 7 else d.year - 1


def candidates(history: list[dict], teams: B.Teams | None = None) -> list[dict]:
    """Every past home or away win priced 1.10 to 2.10 by a stand-in soft book, with its fair chances, form and result."""
    teams = teams or B.Teams(history)
    seasons = sorted({season(g["t_ms"]) for g in history})
    cal = {s: B.calibration([g for g in history if season(g["t_ms"]) < s]) for s in seasons}
    out, form = [], {}
    for g in history:
        if g.get("hg") is None:
            continue
        sharp = {"1": g.get(SHARP[0]), "X": g.get(SHARP[1]), "2": g.get(SHARP[2])}
        fair = B.devig(sharp) if all(sharp.values()) else None
        if not fair:
            continue
        close = B.devig({"1": g.get(CLOSE[0]), "X": g.get(CLOSE[1]), "2": g.get(CLOSE[2])})
        s = season(g["t_ms"])
        for i, m in enumerate(("1", "2")):
            prices = {k: g.get(cols[i]) for k, cols in SOFT.items()}
            if not any(v and 1.10 <= v <= 2.10 for v in prices.values()):
                continue
            p_adj, _ = B.adjust(fair[m], m, g["league"], cal[s], "sharp")
            team, opp = (g["home"], g["away"]) if m == "1" else (g["away"], g["home"])
            for t in (team, opp):
                if (t, g["t_ms"]) not in form:
                    form[(t, g["t_ms"])] = B.form(history, t, g["t_ms"], teams=teams)
            agree, _ = B.stats_agree(form[(team, g["t_ms"])], form[(opp, g["t_ms"])])
            out.append({"t_ms": g["t_ms"], "league": g["league"], "season": s, "market": m, "prices": prices,
                        "p": fair[m], "p_adj": p_adj, "close": close[m] if close else None, "agree": agree,
                        "won": B.wins(m, g["hg"], g["ag"])})
    return out


def score(bets: list[tuple[dict, float]]) -> dict:
    """[(candidate, odds taken)] → bets, won, average odds, return per bet with its standard error, beat the close."""
    n = len(bets)
    if not n:
        return {"n": 0}
    profit = [(o - 1) if c["won"] else -1.0 for c, o in bets]
    roi = sum(profit) / n
    se = math.sqrt(sum((x - roi) ** 2 for x in profit) / (n - 1) / n) if n > 1 else None
    clv = [o * c["close"] - 1 for c, o in bets if c["close"]]
    return {"n": n, "won": sum(c["won"] for c, _ in bets), "odds": sum(o for _, o in bets) / n, "roi": roi, "se": se,
            "clv": sum(clv) / len(clv) if clv else None, "clv_pos": sum(x > 0 for x in clv) / len(clv) if clv else None}


def rules(cands: list[dict], book: str) -> list[dict]:
    """Each rule's record on one stand-in book. The first row is the live flat rule; the others change one thing."""
    def pick(lo=B.FLAT_MIN, hi=B.FLAT_MAX, edge=B.FLAT_EDGE, adj=True, form=False, leagues=None):
        out = []
        for c in cands:
            o = c["prices"].get(book)
            if not o or not lo <= o <= hi or (leagues == "top" and c["league"] not in TOP) or (leagues == "rest" and c["league"] in TOP):
                continue
            if edge is not None and o * (c["p_adj"] if adj else c["p"]) - 1 < edge:
                continue
            if form and not c["agree"]:
                continue
            out.append((c, o))
        return out
    rows = [
        ("The flat rule: odds 1.20–1.50, edge 2%+", pick()),
        ("…without the history adjustment", pick(adj=False)),
        ("…and form backs the favourite", pick(form=True)),
        ("…edge 0%+", pick(edge=0.0)),
        ("…edge 1%+", pick(edge=0.01)),
        ("…edge 3%+", pick(edge=0.03)),
        ("…edge 5%+", pick(edge=0.05)),
        ("…top five leagues only", pick(leagues="top")),
        ("…the other leagues only", pick(leagues="rest")),
        ("Odds 1.50–2.00, edge 2%+", pick(lo=1.50, hi=2.00)),
        ("Every price 1.20–1.50, no edge needed", pick(edge=None)),
    ]
    return [{"rule": r, **score(b)} for r, b in rows]


def rollover(cands: list[dict], book: str, form: bool = True) -> dict:
    """The live rollover rule, one pick a day (Lagos days): the highest adjusted chance at odds 1.15–1.35 with the edge
    no worse than -2% (and form backing it). Followed in runs: a run ends at its first loss."""
    days: dict[int, tuple[dict, float]] = {}
    for c in cands:
        o = c["prices"].get(book)
        if not o or not B.ROLL_MIN <= o <= B.ROLL_MAX or o * c["p_adj"] - 1 < B.ROLL_EDGE or (form and not c["agree"]):
            continue
        d = (c["t_ms"] + WAT) // B.DAY
        if d not in days or c["p_adj"] > days[d][0]["p_adj"]:
            days[d] = (c, o)
    runs, run, best = [], 0, 0
    for d in sorted(days):
        c, o = days[d]
        if c["won"]:
            run += 1
            best = max(best, run)
        else:
            runs.append(run)
            run = 0
    runs.append(run)
    picks = list(days.values())
    return {**score(picks), "days": len(days), "longest": best, "runs": len(runs),
            "avg_run": sum(runs) / len(runs) if runs else 0, "to30": sum(r >= B.ROLL_TARGET for r in runs),
            "to10": sum(r >= 10 for r in runs)}


def study(history: list[dict], teams: B.Teams | None = None) -> dict:
    cands = candidates(history, teams)
    out = {"games": len({(c["t_ms"], c["league"]) for c in cands}), "seasons": sorted({c["season"] for c in cands}),
           "method": B.method(), "books": {}}
    for book in SOFT:
        if any(c["prices"].get(book) for c in cands):
            out["books"][book] = {"rules": rules(cands, book), "rollover": rollover(cands, book),
                                  "rollover_no_form": rollover(cands, book, form=False)}
    return out

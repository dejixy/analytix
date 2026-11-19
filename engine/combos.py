"""
The combo builder (/bets "Combo builder", GET /api/bets/combo): the best accumulator for a total-odds target, 1.1 to 200.

A slip goes on one bookmaker, with at most one bet a game. For a given total the slip most likely to win is also the one
that returns most on average (return = total odds × chance), so the builder maximises the slip's return, the product of
each bet's odds × chance, over slips whose total lands from the target to 10% above it. The chances are Pinnacle's, as
everywhere on the page.

Each bet's edge counts a point lower than measured (two in lower leagues and internationals, where Pinnacle's price is
less sure): the edges that look best are the ones most likely overstated, and for the same total it prefers fewer games.
The chance and return shown are the measured ones.

A bet may go on a slip under the page's reliability rules: its book's odds under 2 hours old and the sharp price under a
day, no more than 10% above fair (above that it is nearly always a stale price or the wrong game), no draws, and odds
1.10 to 3.00.

The search is exact up to rounding: a knapsack over total log-odds in steps of 0.001 (each bet's odds rounded by at most
0.05%), keeping each game's best option per step so the slip can be read back; a slip's total is then checked exactly.

past() replays it on three seasons of football-data's games, one slip a day for each target, with Bet365's home and away
prices as the Nigerian book's stand-in (as in engine/betstudy.py).
"""
import math

import numpy as np

from engine import betting as B
from engine import betstudy as S

STEP = 0.001
TOL = 0.10                                   # a slip may come out up to 10% above the target
T_MIN, T_MAX = 1.1, 200.0
LEG_MIN, LEG_MAX = 1.10, 3.00
HAIRCUT, HAIRCUT_LOW = 0.01, 0.02
LADDER = (1.5, 2, 3, 5, 10, 20, 50, 100, 200)
DAYS = (1, 3, 7)


def bins_for(top: float) -> int:
    return int(math.ceil(math.log(top * (1 + TOL)) / STEP)) + 8


def haircut(x: dict) -> float:
    return HAIRCUT_LOW if B.lower(x) else HAIRCUT


def legs(board: list[dict], book: str, book_ms: int | None, now_ms: int, until_ms: int) -> list[dict]:
    """The board's bets on one book that may go on a slip kicking off before until_ms."""
    if book_ms is None or (now_ms - book_ms) / B.HOUR > B.ODDS_FRESH_H:
        return []
    out = []
    for x in board:
        if (x["market"] == "X" or not now_ms + 10 * 60_000 <= x["t_ms"] <= until_ms or x["sharp_age_h"] > B.SHARP_FRESH_H
                or not x.get("p")):
            continue
        o = next((b["odds"] for b in x.get("others") or [] if b["book"] == book), None)
        if not o or not LEG_MIN <= o <= LEG_MAX or o * x["p"] - 1 > B.EDGE_MAX:
            continue
        out.append({k: x.get(k) for k in ("key", "match_id", "home", "away", "league", "league_name", "t_ms", "market", "intl", "p")}
                   | {"book": book, "odds": o, "edge": o * x["p"] - 1})
    return out


class Solver:
    """Every slip total reachable from a set of bets (one per game), with the best slip at each."""

    def __init__(self, bets: list[dict], top: float = T_MAX):
        n = bins_for(top)
        games: dict = {}
        for x in bets:
            games.setdefault(x["match_id"], []).append(x)
        self.games = [g[:127] for g in games.values()]
        self.w = [[int(round(math.log(x["odds"]) / STEP)) for x in g] for g in self.games]
        best = np.full(n, -np.inf)
        best[0] = 0.0
        self.choice = []
        for g, ws in zip(self.games, self.w):
            new = best.copy()
            pick = np.full(n, -1, dtype=np.int8)
            for i, (x, w) in enumerate(zip(g, ws)):
                if not 0 < w < n:
                    continue
                cand = best[:-w] + (math.log(x["odds"] * x["p"]) - haircut(x))
                better = cand > new[w:]
                new[w:][better] = cand[better]
                pick[w:][better] = i
            self.choice.append(pick)
            best = new
        self.best = best
        self.n = len(bets)

    def read(self, j: int) -> list[dict]:
        out = []
        for g, ws, pick in zip(reversed(self.games), reversed(self.w), reversed(self.choice)):
            i = pick[j]
            if i >= 0:
                out.append(g[i])
                j -= ws[i]
        return out if j == 0 else []

    def find(self, target: float, tol: float = TOL) -> list[dict] | None:
        """The best slip with total odds from target to target × (1 + tol), or None."""
        lo = max(int(math.floor(math.log(target) / STEP)) - 3, 1)
        hi = min(int(math.ceil(math.log(target * (1 + tol)) / STEP)) + 3, len(self.best) - 1)
        if lo > hi:
            return None
        seg = self.best[lo:hi + 1]
        for k in np.argsort(-seg)[:64]:
            if not np.isfinite(seg[k]):
                break
            s = self.read(lo + int(k))
            total = math.prod(x["odds"] for x in s)
            if s and target * (1 - 1e-9) <= total <= target * (1 + tol) * (1 + 1e-9):
                return s
        return None

    def reach(self) -> float:
        """The biggest total these bets can make (within the search's range)."""
        j = np.flatnonzero(np.isfinite(self.best))
        return math.exp(j[-1] * STEP) if len(j) else 1.0


def value(s: list[dict]) -> float:
    return sum(math.log(x["odds"] * x["p"]) - haircut(x) for x in s)


def slip(s: list[dict], with_legs: bool = True) -> dict:
    odds = math.prod(x["odds"] for x in s)
    p = math.prod(x["p"] for x in s)
    out = {"book": s[0]["book"], "odds": odds, "p": p, "fair_odds": 1 / p, "edge": odds * p - 1, "n": len(s),
           "last_ms": max(x["t_ms"] for x in s)}
    if with_legs:
        out["legs"] = sorted(s, key=lambda x: x["t_ms"])
    return out


def best_of(solvers: dict[str, Solver], target: float) -> list[dict] | None:
    """The best slip for a target over every book."""
    got = [s for s in (sv.find(target) for sv in solvers.values()) if s]
    return max(got, key=value) if got else None


def build(solvers: dict[str, Solver], target: float, past: dict | None = None) -> dict:
    """The page's data: the slip for the target, and each size on the ladder with its record on past games."""
    target = min(max(float(target), T_MIN), T_MAX)
    s = best_of(solvers, target)
    ladder = []
    for t in LADDER:
        x = best_of(solvers, t)
        ladder.append({"target": t, **(slip(x, False) if x else {}), "past": (past or {}).get(str(t))})
    done = [r for r in ladder if r.get("n")]
    top = max(done, key=lambda r: r["edge"]) if done else None
    return {"target": target, "slip": slip(s) if s else None, "ladder": ladder, "best_target": top["target"] if top else None,
            "reach": max((sv.reach() for sv in solvers.values()), default=1.0),
            "bets": sum(sv.n for sv in solvers.values()), "books": sorted(k for k, sv in solvers.items() if sv.n),
            "settings": {"legs": [LEG_MIN, LEG_MAX], "tol": TOL, "range": [T_MIN, T_MAX], "haircut": [HAIRCUT, HAIRCUT_LOW]}}


def solvers(board: list[dict], books: dict[str, dict], now_ms: int, days: int) -> dict[str, Solver]:
    until = now_ms + days * B.DAY
    return {b: Solver(legs(board, b, got.get("t_ms"), now_ms, until)) for b, got in books.items()}


# ── on past games ────────────────────────────────────────────────────────────
def past(history: list[dict], book: str = "bet365", targets: tuple = LADDER) -> dict:
    """One slip a day (Lagos days) for each target, built the same way from that day's home and away wins at the stand-in
    book's price: slips, won, the wins the chances expected, average total odds and the return a slip with its
    standard error. {str(target): {...}}"""
    col = dict(zip(("1", "2"), S.SOFT[book]))
    days: dict[int, list[dict]] = {}
    for g in history:
        if g.get("hg") is None:
            continue
        fair = B.devig({"1": g.get(S.SHARP[0]), "X": g.get(S.SHARP[1]), "2": g.get(S.SHARP[2])}) \
            if all(g.get(c) for c in S.SHARP) else None
        if not fair:
            continue
        for m in ("1", "2"):
            o = g.get(col[m])
            if not o or not LEG_MIN <= o <= LEG_MAX or o * fair[m] - 1 > B.EDGE_MAX:
                continue
            days.setdefault((g["t_ms"] + S.WAT) // B.DAY, []).append(
                {"match_id": (g["t_ms"], g["home"]), "league": g["league"], "market": m, "odds": o, "p": fair[m],
                 "won": B.wins(m, g["hg"], g["ag"]), "t_ms": g["t_ms"], "book": book})
    got: dict[float, list[tuple[float, float, bool]]] = {t: [] for t in targets}
    for d in sorted(days):
        sv = Solver(days[d], top=max(targets))
        for t in targets:
            s = sv.find(t)
            if s:
                got[t].append((math.prod(x["odds"] for x in s), math.prod(x["p"] for x in s), all(x["won"] for x in s)))
    out = {}
    for t, xs in got.items():
        n = len(xs)
        if not n:
            out[str(t)] = {"n": 0}
            continue
        profit = [(o - 1) if w else -1.0 for o, _, w in xs]
        roi = sum(profit) / n
        se = math.sqrt(sum((v - roi) ** 2 for v in profit) / (n - 1) / n) if n > 1 else None
        out[str(t)] = {"n": n, "won": sum(w for *_, w in xs), "expected": sum(p for _, p, _ in xs),
                       "odds": sum(o for o, *_ in xs) / n, "roi": roi, "se": se}
    return out

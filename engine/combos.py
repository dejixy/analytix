"""
The combo builder (/bets "Combo builder", GET /api/bets/combo): the best accumulator for a total-odds target, 1.1 to 1,000.

A slip goes on one bookmaker, with at most one bet a game and at most 15 games (20 for a target over 200), and its total lands from the target to
10% above it. The chances are Pinnacle's, as everywhere on the page.

Safest games first (the default): the builder asks for every game on the slip to be 90% likely, and if no slip reaches
the target that way, 85%, then 80%, and so on down to 60%; below that it builds nothing. At the first level that reaches
the target it takes the slip with the best return (total odds × chance), which for a given total is also the slip most
likely to win. With a minimum chance chosen instead (60% to 90%), it takes the best-return slip whose every game is at
least that likely: fewer, longer-priced games than safest-first when the minimum is lower.
Each bet's edge counts a point lower there (two in lower leagues and internationals, where Pinnacle's price is less
sure): the edges that look best are the ones most likely overstated. The chance and return shown are the measured ones.

Markets: wins and double chance, draw no bet (a draw refunds it, so its "chance" is of not losing), total goals, each
team's goals, both teams to score, a result or over x goals, and total corners; `kinds` picks which may go on a slip.
Safest first mixes every picked kind. Goals markets other than over/under 2.5 come from the goals model and count two
points lower; corners come from the corners model alone (no sharp price) and count four points lower.

A bet may go on a slip under the page's reliability rules: its book's odds under 2 hours old and the sharp price under a
day, no more than 10% above fair (above that it is nearly always a stale price or the wrong game), no draws, and odds of
1.10 or more.

The search is exact up to rounding: a knapsack over total log-odds in steps of 0.001 (each bet's odds rounded by at most
0.05%), keeping each game's best option per step so the slip can be read back; a slip's total is then checked exactly.

past() replays it on three seasons of football-data's games, one slip a day for each target, with Bet365's home win,
away win and over/under 2.5 prices as the Nigerian book's stand-in (as in engine/betstudy.py).
"""
import math

import numpy as np

from engine import betting as B
from engine import betstudy as S

STEP = 0.001
TOL = 0.10                                   # a slip may come out up to 10% above the target
T_MIN, T_MAX = 1.1, 1000.0
LEG_MIN = 1.10
FLOORS = (0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60)   # every game on the slip at least this likely, safest first
MAX_GAMES, MAX_GAMES_BIG, BIG = 15, 20, 200      # a target over BIG may use up to 20 games


GAME_CAPS = (10, 15, 20, 25, 30)                # the most games a slip may have, when chosen on the page


def games_cap(target: float, cap: int | None = None) -> int:
    """The most games a slip may have: the chosen cap, else 15 (20 for a target over 200)."""
    if cap in GAME_CAPS:
        return cap
    return MAX_GAMES_BIG if target > BIG else MAX_GAMES
HAIRCUT, HAIRCUT_LOW, HAIRCUT_CORNERS = 0.01, 0.02, 0.04
LADDER = (1.5, 2, 3, 5, 10, 20, 50, 100, 200, 500, 1000)
DAYS = (0, 1, 3, 7)                 # 0: today only (until midnight in Lagos)


def bins_for(top: float) -> int:
    return int(math.ceil(math.log(top * (1 + TOL)) / STEP)) + 8


def haircut(x: dict) -> float:
    if x.get("model") in B.COUNT_MODELS or x.get("model") == "subs":   # no sharp price at all (subs: half the book's)
        return HAIRCUT_CORNERS
    return HAIRCUT_LOW if B.lower(x) or x.get("model") else HAIRCUT


def chance(x: dict) -> float:
    """A bet's chance of not losing (a draw-no-bet refund counts)."""
    return x.get("chance") or x["p"] + x.get("p_void", 0.0)


def ev(x: dict) -> float:
    """What a bet returns on average for each 1 staked (a refund counts as 1 back)."""
    return x["odds"] * x["p"] + x.get("p_void", 0.0)


def legs(board: list[dict], book: str, book_ms: int | None, now_ms: int, until_ms: int,
         kinds: tuple | None = None) -> list[dict]:
    """The board's bets on one book that may go on a slip kicking off before until_ms (of these `kinds` of market)."""
    if book_ms is None or (now_ms - book_ms) / B.HOUR > B.ODDS_FRESH_H:
        return []
    out = []
    for x in board:
        if (x["market"] == "X" or not now_ms + 10 * 60_000 <= x["t_ms"] <= until_ms or x["sharp_age_h"] > B.SHARP_FRESH_H
                or not x.get("p") or (kinds is not None and B.kind(x["market"]) not in kinds)):
            continue
        on = next((b for b in x.get("others") or [] if b["book"] == book), None)
        o = on and on["odds"]
        pv = x.get("p_void") or 0.0
        if not o or o < LEG_MIN or x["p"] + pv < FLOORS[-1] or o * x["p"] + pv - 1 > B.EDGE_MAX:
            continue
        out.append({k: x.get(k) for k in ("key", "match_id", "home", "away", "league", "league_name", "t_ms", "market", "intl",
                                          "p", "model", "sharp_src")}
                   | {"book": book, "odds": o, "p_void": pv, "chance": x["p"] + pv, "edge": o * x["p"] + pv - 1,
                      "sel": on.get("sel")})
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
                cand = best[:-w] + (math.log(ev(x)) - haircut(x))
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

    def find(self, target: float, tol: float = TOL, max_n: int = 10**6) -> list[dict] | None:
        """The best slip of at most max_n games with total odds from target to target × (1 + tol), or None."""
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
            if s and len(s) <= max_n and target * (1 - 1e-9) <= total <= target * (1 + tol) * (1 + 1e-9):
                return s
        return None



class Builder:
    """One book's bets, with a search for each chance level (made when first needed)."""

    def __init__(self, bets: list[dict], top: float = T_MAX):
        self.bets, self.top, self.n, self._at = bets, top, len(bets), {}

    def find(self, target: float, floor: float, cap: int | None = None) -> list[dict] | None:
        if floor not in self._at:
            self._at[floor] = Solver([x for x in self.bets if chance(x) >= floor], self.top)
        return self._at[floor].find(target, max_n=games_cap(target, cap))

    def reach(self, cap: int = MAX_GAMES) -> float:
        """The biggest total of at most `cap` games: the longest price of each game, the longest of those."""
        top: dict = {}
        for x in self.bets:
            top[x["match_id"]] = max(top.get(x["match_id"], 1.0), x["odds"])
        return math.prod(sorted(top.values(), reverse=True)[:cap]) if top else 1.0


def value(s: list[dict]) -> float:
    return sum(math.log(ev(x)) - haircut(x) for x in s)


def slip(s: list[dict], with_legs: bool = True) -> dict:
    odds = math.prod(x["odds"] for x in s)
    p = math.prod(chance(x) for x in s)                          # no bet loses (a refunded one lowers the total)
    out = {"book": s[0]["book"], "odds": odds, "p": p, "fair_odds": 1 / p, "edge": math.prod(ev(x) for x in s) - 1, "n": len(s),
           "least": min(chance(x) for x in s), "last_ms": max(x["t_ms"] for x in s),
           "models": sorted({x["model"] for x in s if x.get("model")}),
           "coded": all(x.get("sel") for x in s)}                  # the book's own ids for every bet: a code can be made
    if with_legs:
        out["legs"] = sorted(s, key=lambda x: x["t_ms"])
    return out


def snap(least: float | None) -> float | None:
    """A chosen minimum chance as one of the levels (None, or 0: safest first)."""
    return min(FLOORS, key=lambda f: abs(f - least)) if least else None


def mode(least: float | None) -> str:
    return "safest" if least is None else f"{least:.2f}"


def best_of(books: dict[str, Builder], target: float, least: float | None = None, cap: int | None = None) -> list[dict] | None:
    """The slip for a target over every book: the safest level any book reaches it at, the best return there; or, with
    a minimum chance, the best return with every game at least that likely."""
    for floor in (FLOORS if least is None else (least,)):
        got = [s for s in (b.find(target, floor, cap) for b in books.values()) if s]
        if got:
            return max(got, key=value)
    return None


def build(solvers: dict[str, Builder], target: float, past: dict | None = None, least: float | None = None,
          book: str | None = None, kinds: tuple | None = None, cap: int | None = None) -> dict:
    """The page's data: the slip for the target, and each size on the ladder with its record on past games (safest first,
    or every game at least `least` likely), from every book or only `book`."""
    target = min(max(float(target), T_MIN), T_MAX)
    every = sorted(k for k, sv in solvers.items() if sv.n)
    if book:
        solvers = {k: v for k, v in solvers.items() if k == book}
    least = snap(least)
    s = best_of(solvers, target, least, cap)
    replayed = kinds is None or set(kinds) <= {"win", "goals"}     # the replay only has wins and over/under 2.5
    ladder, done_past = [], ((past or {}).get(mode(least)) or {}) if replayed else {}
    for t in LADDER:
        x = best_of(solvers, t, least, cap)
        ladder.append({"target": t, **(slip(x, False) if x else {}), "past": done_past.get(str(t)) if cap is None or cap == games_cap(t) else None})   # replayed at the usual cap
    done = [r for r in ladder if r.get("n")]
    top = max(done, key=lambda r: r["edge"]) if done else None
    return {"target": target, "least": least, "slip": slip(s) if s else None, "ladder": ladder,
            "best_target": top["target"] if top else None,
            "reach": max((sv.reach(games_cap(target, cap)) for sv in solvers.values()), default=1.0),
            "bets": sum(sv.n for sv in solvers.values()), "books": every, "book": book or None,
            "settings": {"leg_min": LEG_MIN, "floors": list(FLOORS), "games": games_cap(target, cap), "cap": cap, "caps": list(GAME_CAPS), "tol": TOL, "range": [T_MIN, T_MAX],
                         "haircut": [HAIRCUT, HAIRCUT_LOW]}}


def solvers(board: list[dict], books: dict[str, dict], now_ms: int, days: int, kinds: tuple | None = None,
            leagues_in: tuple | None = None) -> dict[str, Builder]:
    until = window_end(now_ms, days)
    if leagues_in:                                        # only these leagues (and cups)
        board = [x for x in board if x.get("league") in leagues_in]
    return {b: Builder(legs(board, b, got.get("t_ms"), now_ms, until, kinds)) for b, got in books.items()}


def leagues(board: list[dict], now_ms: int, days: int) -> list[dict]:
    """The leagues with games in the window, for the page's picker: [{code, name, games}], most games first."""
    until = window_end(now_ms, days)
    seen: dict = {}
    for x in board:
        if now_ms + 10 * 60_000 <= x["t_ms"] <= until and x.get("league"):
            d = seen.setdefault(x["league"], {"code": x["league"], "name": x.get("league_name") or x["league"], "games": set()})
            d["games"].add(x["match_id"])
    return sorted(({**d, "games": len(d["games"])} for d in seen.values()), key=lambda d: (-d["games"], d["name"]))


def _norm(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def same_league(a: str, b: str) -> bool:
    """Two books' names for a league ("LaLiga", "La Liga") as the same one."""
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x == y or x in y or y in x)


def window_end(now_ms: int, days: int) -> int:
    """The end of the combo builder's window: midnight in Lagos for today only (0), else `days` from now."""
    return B.day_end(now_ms) if days == 0 else now_ms + days * B.DAY


# ── friends' rollovers: a slip of today's games in an odds range ─────────────
def roll_slip(board: list[dict], books: dict[str, dict], now_ms: int, lo: float, hi: float,
              prefer: str | None = "sportybet") -> dict | None:
    """A friend's rollover slip: games kicking off later today (Lagos) that each pass the rollover's rules (Pinnacle-
    priced markets, no worse than 2% under fair, fresh odds and sharp price), on one book, safest games first, the
    total landing from lo to hi. On `prefer` (SportyBet, so a booking code can be made) whenever it can make one, else
    on whichever book can. The slip's record (legs, total odds, chance, edge), or None."""
    if prefer and prefer in books:
        got = roll_slip(board, {prefer: books[prefer]}, now_ms, lo, hi, prefer=None)
        if got:
            return got
    until = B.day_end(now_ms)
    per_book = {}
    for b, got in books.items():
        ls = [x for x in legs(board, b, got.get("t_ms"), now_ms, until) if x["market"] in B.CORE and x["edge"] >= B.roll_bar(x)]
        if ls:
            per_book[b] = ls
    tol = hi / lo - 1
    for floor in FLOORS:
        got = [s for s in (Solver([x for x in ls if chance(x) >= floor], top=hi).find(lo, tol, max_n=games_cap(hi))
                           for ls in per_book.values()) if s]
        if got:
            s = sorted(max(got, key=value), key=lambda x: x["t_ms"])
            return {"key": "+".join(x["key"] for x in s), "legs": s, "n": len(s), "book": s[0]["book"],
                    "odds": math.prod(x["odds"] for x in s), "p": math.prod(chance(x) for x in s),
                    "fair_p": math.prod(chance(x) for x in s), "edge": math.prod(ev(x) for x in s) - 1,
                    "least": min(chance(x) for x in s), "t_ms": s[0]["t_ms"], "last_ms": s[-1]["t_ms"],
                    "market": "slip", "home": s[0]["home"], "away": s[0]["away"], "league": s[0]["league"],
                    "coded": all(x.get("sel") for x in s)}
    return None


# ── players to score: a slip of their own ────────────────────────────────────
SCORERS_PER_GAME = 8          # each game's shortest-priced goalscorers considered


def scorer_legs(books: dict[str, dict], now_ms: int, until_ms: int, book: str = "sportybet",
                league_names: tuple | None = None) -> list[dict]:
    """Each game's shortest-priced anytime goalscorers on a book. There's no chance of our own for these (no sharp
    price, no player data), so a leg's p is only the book's own 1 / odds, used to search, never shown."""
    got = books.get(book)
    if not got or got.get("t_ms") is None or (now_ms - got["t_ms"]) / B.HOUR > B.ODDS_FRESH_H:
        return []
    out = []
    for r in got.get("rows") or []:
        if not now_ms + 10 * 60_000 <= r["t_ms"] <= until_ms or (
                league_names and not any(same_league(r.get("league", ""), n) for n in league_names)):
            continue
        names, ids = r.get("players") or {}, r.get("ids") or {}
        ps = sorted(((m, o) for m, o in r["odds"].items() if B.kind(m) == "scorers" and o >= LEG_MIN), key=lambda t: t[1])
        for m, o in ps[:SCORERS_PER_GAME]:
            out.append({"key": f"{book}:{r['home']}:{r['away']}:{m}", "match_id": f"{r['home']}|{r['away']}|{r['t_ms']}",
                        "home": r["home"], "away": r["away"], "league": r.get("league", ""), "league_name": r.get("league", ""),
                        "t_ms": r["t_ms"], "market": m, "player": names.get(m) or "", "book": book, "odds": o, "p": 1 / o,
                        "p_void": 0.0, "model": "scorer", "sel": ids.get(m)})
    return out


def scorer_slip(bets: list[dict], target: float, cap: int | None = None) -> list[dict] | None:
    """Goalscorers only, one a game: the slip whose longest-priced player is as short as possible (the book's own
    favourites), then the fewest players."""
    if not bets:
        return None
    prices = sorted({x["odds"] for x in bets})
    lo, hi, best = 0, len(prices) - 1, None
    while lo <= hi:                                        # more players to choose from only ever reaches more totals
        mid = (lo + hi) // 2
        got = Solver([x for x in bets if x["odds"] <= prices[mid]], top=target).find(target, max_n=games_cap(target, cap))
        if got:
            best, hi = got, mid - 1
        else:
            lo = mid + 1
    return best


def scorers(books: dict[str, dict], now_ms: int, days: int, target: float, cap: int | None = None,
            league_names: tuple | None = None) -> dict:
    """The players-to-score slip for a total-odds target: no chance, edge or fair odds (we have none to give)."""
    target = min(max(float(target), T_MIN), T_MAX)
    legs = scorer_legs(books, now_ms, window_end(now_ms, days), league_names=league_names)
    out = {"games": len({x["match_id"] for x in legs}), "players": len(legs), "slip": None}
    s = scorer_slip(legs, target, cap)
    if s:
        s = sorted(s, key=lambda x: x["t_ms"])
        out["slip"] = {"book": s[0]["book"], "odds": math.prod(x["odds"] for x in s), "n": len(s), "p": None, "edge": None,
                       "least": None, "last_ms": max(x["t_ms"] for x in s), "coded": all(x.get("sel") for x in s),
                       "legs": [{**x, "p": None} for x in s]}
    return out


# ── on past games ────────────────────────────────────────────────────────────
def past(history: list[dict], book: str = "bet365", targets: tuple = LADDER) -> dict:
    """One slip a day (Lagos days) for each target, built the same way from that day's home wins, away wins and over/under
    2.5 goals at the stand-in book's price: slips, won, the wins the chances expected, average total odds and the
    return a slip with its standard error, for safest-first and each minimum chance.
    {"safest" or "0.70": {str(target): {...}}}"""
    col = {**dict(zip(("1", "2"), S.SOFT[book])), **dict(zip(B.GOALS, S.SOFT_OU[book]))}
    days: dict[int, list[dict]] = {}
    for g in history:
        if g.get("hg") is None:
            continue
        fair = B.devig({"1": g.get(S.SHARP[0]), "X": g.get(S.SHARP[1]), "2": g.get(S.SHARP[2])}) \
            if all(g.get(c) for c in S.SHARP) else None
        if not fair:
            continue
        ou = B.devig(dict(zip(B.GOALS, (g.get(c) for c in S.SHARP_OU))))
        if ou:
            fair = {**fair, **ou}
        for m in ("1", "2", *(B.GOALS if ou else ())):
            o = g.get(col[m])
            if not o or o < LEG_MIN or fair[m] < FLOORS[-1] or o * fair[m] - 1 > B.EDGE_MAX:
                continue
            days.setdefault((g["t_ms"] + S.WAT) // B.DAY, []).append(
                {"match_id": (g["t_ms"], g["home"]), "league": g["league"], "market": m, "odds": o, "p": fair[m], "chance": fair[m],
                 "won": B.wins(m, g["hg"], g["ag"]), "t_ms": g["t_ms"], "book": book})
    leasts = (None, *sorted(FLOORS))
    got: dict = {(lst, t): [] for lst in leasts for t in targets}
    for d in sorted(days):
        day = {"bet365": Builder(days[d], top=max(targets))}            # each level's search is shared by every mode
        for lst in leasts:
            for t in targets:
                s = best_of(day, t, lst)
                if s:
                    got[(lst, t)].append((math.prod(x["odds"] for x in s), math.prod(chance(x) for x in s),
                                          all(x["won"] for x in s)))
    out: dict = {mode(lst): {} for lst in leasts}
    for (lst, t), xs in got.items():
        n = len(xs)
        if not n:
            out[mode(lst)][str(t)] = {"n": 0}
            continue
        profit = [(o - 1) if w else -1.0 for o, _, w in xs]
        roi = sum(profit) / n
        se = math.sqrt(sum((v - roi) ** 2 for v in profit) / (n - 1) / n) if n > 1 else None
        out[mode(lst)][str(t)] = {"n": n, "won": sum(w for *_, w in xs), "expected": sum(p for _, p, _ in xs),
                                  "odds": sum(o for o, *_ in xs) / n, "roi": roi, "se": se}
    return out

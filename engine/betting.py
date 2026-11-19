"""
The bets suite: football odds from Nigerian bookmakers measured against a sharp reference, football stats and history.
Pure functions and a small paper book; ingestion/football.py reads the data, api/bets.py runs it, /bets shows it.

    fair odds    the sharp market (Pinnacle, else the Betfair exchange, else the average of many books) with its margin
                 taken out by the method that fits past results best (proportional, power or Shin); double chance is the
                 sum of its two outcomes
    history      three seasons of every covered league (football-data.co.uk): how often favourites at each price actually
                 won, by league and side, against what the price said. Shown beside each bet (the History column, and
                 the chance it would give), but not used by the rules: on past games the rule did as well without it,
                 and Pinnacle's price alone is the better-proven of the two
    stats        each team's last six league games: points, goals and shots on target for and against. A favourite's
                 numbers "agree" when it has more points a game and a bigger share of the shots on target than its
                 opponent, and hasn't lost two of its last four. Shown, not used by the rules (on past games the check
                 left out picks without improving them)
    value        edge = Nigerian odds x the sharp fair chance - 1. Flat bets: odds 1.20 to 1.50 and an edge of 2%+ (3%+ in
                 lower leagues and internationals) up to 10%, odds read in the last two hours, a sharp price from the last
                 24; shown up to a week ahead, booked on paper from three days before kick-off
    rollover     one pick a day, fixed once made: odds 1.15 to 1.35, kick-off within 30 hours, edge no worse than -2% (at
                 least fair in lower leagues and internationals); the highest sharp chance of winning. Followed as a
                 paper chain from N5,000, the balance riding on each pick, with up to six refills
    record       every flat value bet and every rollover pick, followed to its result with its closing-line value (the
                 bet's odds against the fair closing price: beating the close is the earliest sign an edge is real)
"""
import json
import math
import os
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

HOUR = 3_600_000
DAY = 24 * HOUR
MARKETS = ("1", "X", "2", "1X", "12", "X2", "O25", "U25")
GOALS = ("O25", "U25")               # over / under 2.5 goals: priced from Pinnacle's own over/under (football-data)
MARKET_NAME = {"1": "Home win", "X": "Draw", "2": "Away win", "1X": "Home or draw", "12": "Either team wins",
               "X2": "Away or draw", "O25": "Over 2.5 goals", "U25": "Under 2.5 goals"}
BOOK_KEYS = {"home": "1", "draw": "X", "away": "2", "home_or_draw": "1X", "home_or_away": "12", "draw_or_away": "X2",
             "over_2_5": "O25", "under_2_5": "U25"}

FLAT_MIN, FLAT_MAX, FLAT_EDGE = 1.20, 1.50, 0.02
FLAT_EDGE_LOW = 0.03                 # leagues where Pinnacle's own price is less sure, internationals, and goals bets
EDGE_MAX = 0.10                      # above this an "edge" is nearly always a stale price or the wrong game: not bet
LOWER_LEAGUES = {"E2", "E3", "SC0", "B1", "T1", "G1"}
FLAT_STAKE = 250
ROLL_MIN, ROLL_MAX, ROLL_EDGE = 1.15, 1.35, -0.02
ROLL_START, ROLL_REFILLS, ROLL_TARGET = 5_000, 6, 30
ODDS_FRESH_H, SHARP_FRESH_H = 2, 24
SHOW_DAYS, BOOK_DAYS = 7, 3          # games shown up to a week ahead; paper bets booked from three days before kick-off
SHRINK = 200                      # history moves the chance by (wins - expected) / (n + SHRINK)
FORM_N = 6


# ── names ────────────────────────────────────────────────────────────────────
ALIASES = {
    "manchester united": "man united", "man utd": "man united", "manchester city": "man city",
    "tottenham hotspur": "tottenham", "spurs": "tottenham", "wolverhampton wanderers": "wolves", "wolverhampton": "wolves",
    "nottingham forest": "nottm forest", "nott m forest": "nottm forest", "newcastle united": "newcastle",
    "west ham united": "west ham", "brighton and hove albion": "brighton", "brighton hove albion": "brighton",
    "leeds united": "leeds", "leicester city": "leicester", "sheffield united": "sheffield utd", "sheff utd": "sheffield utd",
    "afc bournemouth": "bournemouth", "inter milan": "inter", "internazionale": "inter", "ac milan": "milan",
    "paris saint germain": "paris sg", "psg": "paris sg", "paris st germain": "paris sg", "bayern munich": "bayern munchen",
    "fc bayern munchen": "bayern munchen", "borussia dortmund": "dortmund", "borussia monchengladbach": "mgladbach",
    "b monchengladbach": "mgladbach", "bayer leverkusen": "leverkusen", "rb leipzig": "rb leipzig",
    "eintracht frankfurt": "ein frankfurt", "atletico madrid": "ath madrid", "atletico de madrid": "ath madrid",
    "athletic bilbao": "ath bilbao", "athletic club": "ath bilbao", "real betis": "betis", "real sociedad": "sociedad",
    "celta vigo": "celta", "rayo vallecano": "vallecano", "deportivo alaves": "alaves", "sporting cp": "sp lisbon",
    "sporting lisbon": "sp lisbon", "fc porto": "porto", "sl benfica": "benfica", "olympique marseille": "marseille",
    "olympique lyonnais": "lyon", "olympique lyon": "lyon", "as monaco": "monaco", "stade rennais": "rennes",
    "psv eindhoven": "psv", "ajax amsterdam": "ajax", "az alkmaar": "az alkmaar", "feyenoord rotterdam": "feyenoord",
    "glasgow rangers": "rangers", "celtic glasgow": "celtic", "as roma": "roma", "ss lazio": "lazio", "ssc napoli": "napoli",
    "hellas verona": "verona", "juventus turin": "juventus",
    # national teams, as the books and The Odds API spell them differently
    "czechia": "czech republic", "czech rep": "czech republic", "turkiye": "turkey", "republic of ireland": "ireland",
    "ireland republic": "ireland", "bosnia and herzegovina": "bosnia", "bosnia herzegovina": "bosnia",
    "korea republic": "south korea", "korea rep": "south korea", "republic of korea": "south korea", "usa": "united states",
    "united states of america": "united states", "cote d ivoire": "ivory coast", "cabo verde": "cape verde",
    "cape verde islands": "cape verde", "congo dr": "dr congo", "democratic republic of congo": "dr congo",
    "democratic republic of the congo": "dr congo", "macedonia": "north macedonia", "fyr macedonia": "north macedonia",
    "china pr": "china", "ir iran": "iran", "holland": "netherlands", "kyrgyz republic": "kyrgyzstan",
    "swaziland": "eswatini", "chinese taipei": "taiwan", "republic of moldova": "moldova",
}
DROP = {"fc", "cf", "afc", "sc", "ac", "club", "the", "de", "calcio", "1", "sv", "fk", "sk", "cd", "ud", "sd", "rc", "ca"}


@lru_cache(maxsize=16384)
def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and "))
    s = " ".join(s.split())
    if s in ALIASES:
        return ALIASES[s]
    s = " ".join(w for w in s.split() if w not in DROP) or s
    return ALIASES.get(s, s)


@lru_cache(maxsize=65536)
def same_team(a: str, b: str, least: float = 0.0) -> float:
    """How alike two team names are, 0 to 1, after aliases ("Man Utd" and "Manchester United" are 1). With `least`, a
    pair that can't reach it gives 0 without the slow comparison (the quick checks are upper bounds of the full one)."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if (x in y or y in x) and min(len(x), len(y)) >= 4:
        return 0.92
    sm = SequenceMatcher(None, x, y)
    if least and (sm.real_quick_ratio() < least or sm.quick_ratio() < least):
        return 0.0
    return sm.ratio()


def match_fixture(home: str, away: str, t_ms: int, fixtures: list[dict], within_h: float = 36, least: float = 0.72) -> dict | None:
    """The fixture in `fixtures` ({home, away, t_ms, ...}) that is the same game, by both names and kick-off."""
    best, score = None, 0.0
    for f in fixtures:
        if abs((f.get("t_ms") or 0) - t_ms) > within_h * HOUR:
            continue
        h = same_team(home, f["home"], least)
        if h < least:
            continue
        a = same_team(away, f["away"], least)
        if a < least:
            continue
        if h + a > score:
            best, score = f, h + a
    return best


# ── fair chances ─────────────────────────────────────────────────────────────
# Three ways to take a bookmaker's margin out of its prices. Which fits best is an empirical question: the server scores
# each on three seasons of Pinnacle's prices against the results (compare_methods) and uses the best (set_method).
#   proportional  every outcome's chance shrunk by the same factor
#   power         chances = (1/odds)^k with k chosen so they add to 1: more of the margin comes off the long shots
#   shin          Shin's model: the margin is the price of protection against insiders, also heavier on long shots
METHODS = ("proportional", "power", "shin")
_method = "proportional"


def set_method(name: str) -> None:
    global _method
    _method = name if name in METHODS else "proportional"


def method() -> str:
    return _method


def devig(odds: dict[str, float], how: str | None = None) -> dict[str, float] | None:
    """{outcome: odds} → {outcome: fair chance}, the margin taken out by the chosen method."""
    inv = {k: 1 / v for k, v in odds.items() if v and v > 1}
    if len(inv) != len(odds) or not inv:
        return None
    s = sum(inv.values())
    how = how or _method
    if s > 1 and how == "power":
        lo, hi = 1.0, 20.0
        for _ in range(60):
            k = (lo + hi) / 2
            lo, hi = (k, hi) if sum(q ** k for q in inv.values()) > 1 else (lo, k)
        p = {o: q ** ((lo + hi) / 2) for o, q in inv.items()}
    elif s > 1 and how == "shin":
        def shin(z):
            return {o: (math.sqrt(z * z + 4 * (1 - z) * q * q / s) - z) / (2 * (1 - z)) for o, q in inv.items()}
        lo, hi = 0.0, 0.5
        for _ in range(60):
            z = (lo + hi) / 2
            lo, hi = (z, hi) if sum(shin(z).values()) > 1 else (lo, z)
        p = shin((lo + hi) / 2)
    else:
        p = inv
    t = sum(p.values())
    return {k: v / t for k, v in p.items()}


def compare_methods(history: list[dict], cols: tuple = ("PSH", "PSD", "PSA")) -> dict:
    """How well each method's fair chances fit the results of past games priced in `cols` (Pinnacle's, by default):
    {method: {n, logloss (lower is better), fav_n, fav_won, fav_said}}, the "fav" numbers for favourites priced 1.15 to
    1.50, the range the bets live in."""
    out = {m: {"n": 0, "logloss": 0.0, "fav_n": 0, "fav_won": 0, "fav_said": 0.0} for m in METHODS}
    for g in history:
        if g.get("hg") is None:
            continue
        odds = {"1": g.get(cols[0]), "X": g.get(cols[1]), "2": g.get(cols[2])}
        if not all(odds.values()):
            continue
        r = "1" if g["hg"] > g["ag"] else "2" if g["ag"] > g["hg"] else "X"
        fav = min(("1", "2"), key=lambda k: odds[k])
        for m in METHODS:
            p = devig(odds, m)
            if not p:
                continue
            o = out[m]
            o["n"] += 1
            o["logloss"] -= math.log(max(p[r], 1e-9))
            if 1.15 <= odds[fav] <= 1.50:
                o["fav_n"] += 1
                o["fav_won"] += r == fav
                o["fav_said"] += p[fav]
    for o in out.values():
        if o["n"]:
            o["logloss"] /= o["n"]
    return out


def best_method(scores: dict, least: int = 1000) -> str:
    ok = {m: v for m, v in scores.items() if v["n"] >= least}
    return min(ok, key=lambda m: ok[m]["logloss"]) if ok else "proportional"


def markets_from_1x2(p: dict[str, float]) -> dict[str, float]:
    return {"1": p["1"], "X": p["X"], "2": p["2"], "1X": p["1"] + p["X"], "12": p["1"] + p["2"], "X2": p["X"] + p["2"]}


def wins(market: str, hg: int, ag: int) -> bool:
    if market in GOALS:
        return (hg + ag >= 3) == (market == "O25")
    r = "1" if hg > ag else "2" if ag > hg else "X"
    return r in market


# ── history ──────────────────────────────────────────────────────────────────
BUCKETS = [(0.55, 0.62), (0.62, 0.68), (0.68, 0.74), (0.74, 0.80), (0.80, 0.86), (0.86, 0.95)]


def bucket_of(p: float) -> int | None:
    return next((i for i, (lo, hi) in enumerate(BUCKETS) if lo <= p < hi), None)


HIST_PRICES = {"sharp": (("PSCH", "PSCD", "PSCA"), ("PSH", "PSD", "PSA")),
               "avg": (("AvgCH", "AvgCD", "AvgCA"), ("AvgH", "AvgD", "AvgA"), ("B365H", "B365D", "B365A"))}


def hist_fair(row: dict, kind: str | None = None) -> dict[str, float] | None:
    """A past game's fair 1X2 chances from its prices of one kind ("sharp": Pinnacle's close, else Pinnacle; "avg":
    the market's average close, else its average, else Bet365), or the sharpest it has when no kind is given."""
    for k in ([kind] if kind else ["sharp", "avg"]):
        for h, d, a in HIST_PRICES[k]:
            p = devig({"1": row.get(h), "X": row.get(d), "2": row.get(a)})
            if p:
                return p
    return None


def price_kind(src: str) -> str:
    """Which history a live price is judged against: "sharp" for Pinnacle, an exchange or Matchbook, else "avg"."""
    s = (src or "").lower()
    return "sharp" if any(k in s for k in ("pinnacle", "betfair", "exchange", "matchbook")) else "avg"


def calibration(history: list[dict]) -> dict:
    """{(kind, league, market, bucket): [n, wins, expected]} and the pooled (kind, "*", market, bucket), for each kind
    of price: how often a bet at each fair chance actually won. Favourite markets only."""
    out: dict = {}
    for g in history:
        if g.get("hg") is None:
            continue
        for kind in HIST_PRICES:
            p = hist_fair(g, kind)
            if p:
                _count(out, kind, g, p)
    return out


def _count(out: dict, kind: str, g: dict, p: dict) -> None:
    """Count one game's favourite markets under its price kind."""
    for m, q in markets_from_1x2(p).items():
        if m == "X":
            continue
        b = bucket_of(q)
        if b is None:
            continue
        won = wins(m, g["hg"], g["ag"])
        for key in ((kind, g["league"], m, b), (kind, "*", m, b)):
            c = out.setdefault(key, [0, 0, 0.0])
            c[0] += 1
            c[1] += won
            c[2] += q


def adjust(p: float, market: str, league: str, cal: dict, kind: str = "sharp") -> tuple[float, dict | None]:
    """The fair chance moved by history measured on the same kind of price: the league's bucket if it has 40+ games,
    else every league's; shrunk. No adjustment without 40 games of that kind."""
    b = bucket_of(p)
    if b is None:
        return p, None
    c = cal.get((kind, league, market, b))
    scope = league
    if not c or c[0] < 40:
        c, scope = cal.get((kind, "*", market, b)), "all leagues"
    if not c or c[0] < 40:
        return p, None
    n, w, e = c
    shift = (w - e) / (n + SHRINK)
    lo, hi = BUCKETS[b]
    return min(0.99, max(0.01, p + shift)), {"n": n, "won": w, "rate": w / n, "implied": e / n, "scope": scope, "kind": kind,
                                             "range": [round(1 / hi, 2), round(1 / lo, 2)], "shift": shift}


class Teams:
    """History by team name, so a team's games are found by comparing its name with the few hundred names in history
    rather than with every game (thousands of games for each of hundreds of teams froze the server)."""

    def __init__(self, history: list[dict]):
        self.history = history
        self.by_name: dict[str, list[tuple[int, str]]] = {}
        for i, g in enumerate(history):
            self.by_name.setdefault(g["home"], []).append((i, "1"))
            self.by_name.setdefault(g["away"], []).append((i, "2"))
        self._seen: dict[str, list[tuple[int, str]]] = {}

    def games(self, team: str) -> list[tuple[dict, str]]:
        """[(game, the team's side "1" or "2")] for every game a team played, in history's order."""
        hit = self._seen.get(team)
        if hit is None:
            hit = sorted((i, side) for name, played in self.by_name.items() if same_team(team, name, 0.92) >= 0.92
                         for i, side in played)
            self._seen[team] = hit
        return [(self.history[i], side) for i, side in hit]


def team_games(history: list[dict], team: str, teams: Teams | None = None) -> list[tuple[dict, str]]:
    if teams is not None:
        return teams.games(team)
    out = []
    for g in history:
        if same_team(team, g["home"], 0.92) >= 0.92:
            out.append((g, "1"))
        elif same_team(team, g["away"], 0.92) >= 0.92:
            out.append((g, "2"))
    return out


def team_as_favourite(history: list[dict], team: str, least: float = 0.65, teams: Teams | None = None) -> dict | None:
    """How a team did when the market made it a clear favourite (fair chance of winning >= `least`)."""
    n = w = 0
    for g, side in team_games(history, team, teams):
        p = hist_fair(g)
        if not p or g.get("hg") is None:
            continue
        if p[side] >= least:
            n += 1
            w += wins(side, g["hg"], g["ag"])
    return {"n": n, "won": w} if n else None


def form(history: list[dict], team: str, before_ms: int, n: int = FORM_N, teams: Teams | None = None) -> dict | None:
    """A team's last `n` league games before a time: points, goals and shots on target a game, results newest first."""
    games = []
    for g, side in team_games(history, team, teams):
        if g.get("hg") is None or g["t_ms"] >= before_ms:
            continue
        if side == "1":
            games.append((g["t_ms"], g["hg"], g["ag"], g.get("hst"), g.get("ast")))
        else:
            games.append((g["t_ms"], g["ag"], g["hg"], g.get("ast"), g.get("hst")))
    games = sorted(games, reverse=True)[:n]
    if len(games) < 3:
        return None
    pts = [3 if f > a else 1 if f == a else 0 for _, f, a, _, _ in games]
    sf = [x[3] for x in games if x[3] is not None]
    sa = [x[4] for x in games if x[4] is not None]
    return {"n": len(games), "ppg": sum(pts) / len(games), "gf": sum(x[1] for x in games) / len(games),
            "ga": sum(x[2] for x in games) / len(games), "sot_for": sum(sf) / len(sf) if sf else None,
            "sot_against": sum(sa) / len(sa) if sa else None,
            "results": "".join("W" if p == 3 else "D" if p == 1 else "L" for p in pts)}


def stats_agree(fav: dict | None, opp: dict | None) -> tuple[bool | None, str]:
    """Do the favourite's recent numbers back the price? (see the module note)"""
    if not fav or not opp:
        return None, "not enough recent games to judge"
    reasons = []
    ok = True
    if fav["ppg"] <= opp["ppg"]:
        ok = False
        reasons.append(f"fewer points a game lately ({fav['ppg']:.1f} vs {opp['ppg']:.1f})")
    share = lambda f: f["sot_for"] / (f["sot_for"] + f["sot_against"]) if f["sot_for"] is not None and f["sot_against"] else None
    sf, so = share(fav), share(opp)
    if sf is not None and so is not None and sf <= so:
        ok = False
        reasons.append(f"a smaller share of shots on target ({sf:.0%} vs {so:.0%})")
    if fav["results"][:4].count("L") >= 2:
        ok = False
        reasons.append(f"lost {fav['results'][:4].count('L')} of its last 4")
    if ok:
        return True, (f"{fav['ppg']:.1f} points a game against {opp['ppg']:.1f}"
                      + (f", {sf:.0%} of its games' shots on target against {so:.0%}" if sf is not None and so is not None else ""))
    return False, "; ".join(reasons)


def favourite_side(market: str, p: dict[str, float]) -> str | None:
    if market in ("1", "1X"):
        return "1"
    if market in ("2", "X2"):
        return "2"
    if market == "12":
        return "1" if p["1"] >= p["2"] else "2"
    return None


def goals_fair(s: dict) -> dict[str, float]:
    """A sharp game's fair over/under 2.5 chances ({"O25", "U25"}), by the margin method in use, or {} without them."""
    o = s.get("ou_odds")
    return (devig(o) if o else None) or {}


# ── the board ────────────────────────────────────────────────────────────────
def build(books: dict[str, dict], sharp: list[dict], history: list[dict], cal: dict, now_ms: int,
          teams: Teams | None = None) -> list[dict]:
    """Every Nigerian price matched to a sharp game, with the fair chance, the history-adjusted chance, the edge, the
    stats and the patterns. books: {book: {"t_ms", "rows": [{home, away, t_ms, league, odds: {market: price}}]}}."""
    out = []
    for book, got in books.items():
        age_h = (now_ms - got["t_ms"]) / HOUR
        for r in got["rows"]:
            if r["t_ms"] < now_ms + 10 * 60_000 or r["t_ms"] > now_ms + SHOW_DAYS * DAY:
                continue
            s = match_fixture(r["home"], r["away"], r["t_ms"], sharp)
            if not s or s["t_ms"] < now_ms + 10 * 60_000:      # the sharp kick-off is the reliable one
                continue
            fair1x2 = (devig(s["odds"]) if s.get("odds") else None) or s["fair"]     # the current method, from raw prices
            fair = {**markets_from_1x2(fair1x2), **goals_fair(s)}
            intl = bool(s.get("intl"))
            for m, price in r["odds"].items():
                if m not in fair or not price or price <= 1:
                    continue
                goals = m in GOALS
                src = s.get("ou_src", s["src"]) if goals else s["src"]
                read_ms = s.get("ou_read_ms", s["read_ms"]) if goals else s["read_ms"]
                p_adj, pat = (fair[m], None) if intl or goals else adjust(fair[m], m, s.get("league", ""), cal, price_kind(src))
                out.append({"key": f"{s['id']}:{m}", "match_id": s["id"], "home": s["home"], "away": s["away"],
                            "league": s.get("league", ""), "league_name": s.get("league_name", ""), "t_ms": s["t_ms"],
                            "market": m, "book": book, "odds": price, "odds_age_h": age_h, "fair_p": fair[m], "p": fair[m],
                            "p_hist": p_adj, "fair_odds": 1 / fair[m], "edge": price * fair[m] - 1, "edge_hist": price * p_adj - 1,
                            "sharp_src": src, "fair1x2": fair1x2,
                            "sharp_age_h": (now_ms - read_ms) / HOUR, "pattern": pat, "intl": intl})
    groups: dict[str, list[dict]] = {}
    for x in out:
        groups.setdefault(x["key"], []).append(x)
    rows = []
    for xs in groups.values():                             # the best Nigerian price for each game and market
        top = max(xs, key=lambda x: x["odds"])
        rows.append({**top, "others": sorted(({"book": x["book"], "odds": x["odds"]} for x in xs), key=lambda o: -o["odds"])})
    stats_cache: dict = {}
    for x in rows:
        side = favourite_side(x["market"], x["fair1x2"])
        if side is None:
            continue
        fav, opp = (x["home"], x["away"]) if side == "1" else (x["away"], x["home"])
        if x["intl"]:                                      # club history says nothing about national teams
            x["fav"], x["fav_form"], x["opp_form"], x["fav_record"] = fav, None, None, None
            x["agree"], x["agree_why"] = None, "an international: no club form to check, so it's judged on the price alone"
            continue
        for t in (fav, opp):
            if t not in stats_cache:
                stats_cache[t] = form(history, t, x["t_ms"], teams=teams)
        x["fav"], x["fav_form"], x["opp_form"] = fav, stats_cache[fav], stats_cache[opp]
        x["agree"], x["agree_why"] = stats_agree(stats_cache[fav], stats_cache[opp])
        if ("rec", fav) not in stats_cache:
            stats_cache[("rec", fav)] = team_as_favourite(history, fav, teams=teams)
        x["fav_record"] = stats_cache[("rec", fav)]
    return sorted(rows, key=lambda x: -x["edge"])


def lower(x: dict) -> bool:
    """A lower league, an international or a goals bet: the sharp price there is less sure (a goals price comes from
    football-data's copy of Pinnacle's, read a day or two before), so the bar is higher."""
    return bool(x.get("intl")) or x.get("league") in LOWER_LEAGUES or x.get("market") in GOALS


def edge_bar(x: dict) -> float:
    return FLAT_EDGE_LOW if lower(x) else FLAT_EDGE


def rule_of(x: dict, edge: float | None = None) -> str | None:
    """Which rule finds value in a price: "new" when Pinnacle's chance alone gives an edge of 2% (3% in lower leagues,
    internationals and goals bets, or `edge` if given) up to 10%; else "old" when the history-adjusted chance does
    (the rule before PR #140); else None."""
    bar = edge if edge is not None else edge_bar(x)
    if bar <= x["edge"] <= EDGE_MAX:
        return "new"
    if x.get("pattern") and x.get("edge_hist") is not None and bar <= x["edge_hist"] <= EDGE_MAX:
        return "old"
    return None


def value_bets(board: list[dict], lo: float = FLAT_MIN, hi: float = FLAT_MAX, edge: float | None = None,
               before_ms: int | None = None, old: bool = True) -> list[dict]:
    """Flat value bets by either rule (rule_of; with old=False, the new rule only), the new rule's first; with
    before_ms, only those kicking off before it."""
    out = [x for x in board if lo <= x["odds"] <= hi and rule_of(x, edge) in (("new", "old") if old else ("new",))
           and x["odds_age_h"] <= ODDS_FRESH_H and x["sharp_age_h"] <= SHARP_FRESH_H and x["market"] != "X"
           and (before_ms is None or x["t_ms"] <= before_ms)]
    return sorted(out, key=lambda x: (rule_of(x, edge) != "new", -(x["edge"] if rule_of(x, edge) == "new" else x["edge_hist"])))


def roll_bar(x: dict) -> float:
    """The worst edge a rollover pick may have: 2% under fair, or at least fair in lower leagues and internationals."""
    return 0.0 if lower(x) else ROLL_EDGE


def rollover_pick(board: list[dict], now_ms: int) -> dict | None:
    """Today's one rollover bet (see the module note), or None if nothing qualifies."""
    c = [x for x in board if ROLL_MIN <= x["odds"] <= ROLL_MAX and x["t_ms"] <= now_ms + 30 * HOUR
         and roll_bar(x) <= x["edge"] <= EDGE_MAX
         and x["odds_age_h"] <= ODDS_FRESH_H and x["sharp_age_h"] <= SHARP_FRESH_H and x["market"] != "X"]
    return max(c, key=lambda x: (x["p"], x["edge"])) if c else None


def rollover_misses(board: list[dict], now_ms: int, n: int = 3) -> list[dict]:
    """When nothing qualifies: the games in the next 30 hours closest to being the rollover pick, each with the tests
    it fails in plain words, fewest failures first."""
    out = []
    for x in board:
        if x["market"] == "X" or x["t_ms"] > now_ms + 30 * HOUR or not 1.10 <= x["odds"] <= 1.45:
            continue
        why = []
        if not ROLL_MIN <= x["odds"] <= ROLL_MAX:
            why.append(f"odds {x['odds']:.2f}, needs {ROLL_MIN:.2f}–{ROLL_MAX:.2f}")
        if x["edge"] < roll_bar(x):
            where = ("an international" if x.get("intl") else "a goals bet" if x.get("market") in GOALS
                     else "a lower league" if lower(x) else None)
            why.append(f"{-x['edge'] * 100:.1f}% under fair; {where} needs at least fair" if where
                       else f"{-x['edge'] * 100:.1f}% under fair, allowed {-ROLL_EDGE * 100:.0f}%")
        if x["edge"] > EDGE_MAX:
            why.append(f"{x['edge'] * 100:.0f}% above fair: likely a stale price or the wrong game")
        if x["odds_age_h"] > ODDS_FRESH_H:
            why.append(f"Nigerian odds {x['odds_age_h']:.0f}h old, need under {ODDS_FRESH_H}h")
        if x["sharp_age_h"] > SHARP_FRESH_H:
            why.append(f"Pinnacle's price {x['sharp_age_h']:.0f}h old")
        if why:
            out.append({k: x.get(k) for k in ("key", "home", "away", "market", "odds", "book", "t_ms", "league", "league_name",
                                              "edge")} | {"why": why})
    out.sort(key=lambda m: (len(m["why"]), -m["edge"]))
    return out[:n]


def chain_odds(p: float, days: int) -> float:
    """The chance of winning `days` picks in a row at chance p each."""
    return p ** max(days, 0)


# ── the paper book ───────────────────────────────────────────────────────────
class Book:
    """Flat value bets and the rollover chain, followed to their results. Kept in a JSON file."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.flat: list[dict] = []
        self.picks: list[dict] = []                 # one a day: {day, bet, chain, stake, status, ...}
        self.chain = {"n": 1, "balance": float(ROLL_START), "day": 0, "refills": 0, "best": 0}
        self.load()

    def load(self) -> None:
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.flat, self.picks = d.get("flat", []), d.get("picks", [])
                self.chain.update(d.get("chain") or {})
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"flat": self.flat[-3000:], "picks": self.picks[-1000:], "chain": self.chain}))
        os.replace(tmp, self.path)

    @staticmethod
    def _slim(x: dict) -> dict:
        keep = ("key", "match_id", "home", "away", "league", "league_name", "t_ms", "market", "book", "odds", "fair_p", "p",
                "edge", "sharp_src", "p_hist", "edge_hist")
        return {**{k: x.get(k) for k in keep}, "rule": rule_of(x)}

    def add_flat(self, x: dict, now_ms: int, stake: float = FLAT_STAKE) -> dict | None:
        if any(b["key"] == x["key"] for b in self.flat):
            return None
        b = {**self._slim(x), "placed_ms": now_ms, "stake": stake, "status": "open"}
        self.flat.append(b)
        return b

    def today_pick(self, day: str) -> dict | None:
        return next((p for p in self.picks if p["day"] == day), None)

    def add_pick(self, x: dict, day: str, now_ms: int) -> dict | None:
        """Today's rollover bet, once a day, only when yesterday's has settled (the balance has to be known)."""
        if self.today_pick(day) or any(p["status"] == "open" for p in self.picks):
            return None
        if self.chain["refills"] > ROLL_REFILLS:
            return None
        p = {**self._slim(x), "day": day, "placed_ms": now_ms, "chain": self.chain["n"], "leg": self.chain["day"] + 1,
             "stake": round(self.chain["balance"], 2), "status": "open"}
        self.picks.append(p)
        return p

    def settle(self, results: list[dict], closing: list[dict] | None = None) -> list[dict]:
        """Settle open bets from results ({home, away, t_ms, hg, ag}) and add closing-line value from closing fair
        prices ({home, away, t_ms, fair}). Returns what settled."""
        done = []
        for b in [*self.flat, *self.picks]:
            if b["status"] == "open":
                r = match_fixture(b["home"], b["away"], b["t_ms"], results, within_h=30)
                if r and r.get("hg") is not None:
                    won = wins(b["market"], r["hg"], r["ag"])
                    b.update(status="won" if won else "lost", score=f"{r['hg']}-{r['ag']}",
                             profit=round(b["stake"] * (b["odds"] - 1) if won else -b["stake"], 2))
                    done.append(b)
                    if b in self.picks:
                        self._roll(b, won)
            if closing and b.get("clv") is None:
                c = match_fixture(b["home"], b["away"], b["t_ms"], closing, within_h=30)
                if c and (c.get("fair") or c.get("ou")):
                    q = (c.get("ou") or {}).get(b["market"]) if b["market"] in GOALS else \
                        markets_from_1x2(c["fair"]).get(b["market"]) if c.get("fair") else None
                    if q:
                        b["clv"] = b["odds"] * q - 1
        return done

    def _roll(self, p: dict, won: bool) -> None:
        c = self.chain
        if won:
            c["balance"] = p["stake"] * p["odds"]
            c["day"] += 1
            c["best"] = max(c["best"], c["day"])
        else:
            c["refills"] += 1
            c["n"] += 1
            c["balance"] = float(ROLL_START)
            c["day"] = 0

    def record(self) -> dict:
        s = [b for b in self.flat if b["status"] != "open"]
        staked = sum(b["stake"] for b in s)
        profit = sum(b["profit"] for b in s)
        clv = [b["clv"] for b in self.flat if b.get("clv") is not None]
        p = [x for x in self.picks if x["status"] != "open"]
        return {"flat": {"n": len(s), "won": sum(1 for b in s if b["status"] == "won"), "staked": staked, "profit": profit,
                         "roi": profit / staked if staked else None, "open": sum(1 for b in self.flat if b["status"] == "open"),
                         "clv": sum(clv) / len(clv) if clv else None, "clv_n": len(clv),
                         "expected": sum(b["edge"] * b["stake"] for b in s)},
                "rollover": {**self.chain, "picks": len(p), "won": sum(1 for x in p if x["status"] == "won"),
                             "spent": ROLL_START * (self.chain["refills"] + 1)}}

"""
The bets suite: football odds from Nigerian bookmakers measured against a sharp reference, football stats and history.
Pure functions and a small paper book; ingestion/football.py reads the data, api/bets.py runs it, /bets shows it.

    fair odds    the sharp market (Pinnacle, else the Betfair exchange, else the average of many books) with its margin
                 taken out in proportion; double chance is the sum of its two outcomes
    history      three seasons of every covered league (football-data.co.uk): how often favourites at each price actually
                 won, by league and side, against what the price said. The difference, shrunk toward zero so a thin
                 sample can't move it much, adjusts the fair chance. Kept apart by the kind of price it was measured
                 on: a sharp price (Pinnacle, an exchange) is only adjusted by history measured on sharp prices, because
                 the market average carries a bigger margin and taking it out understates favourites more
    stats        each team's last six league games: points, goals and shots on target for and against. A favourite's
                 numbers "agree" when it has more points a game and a bigger share of the shots on target than its
                 opponent, and hasn't lost two of its last four
    value        edge = Nigerian odds x adjusted chance - 1. Flat bets: odds 1.20 to 1.50 and an edge of 2%+, odds read in
                 the last two hours, a sharp price from the last 24; shown up to a week ahead, booked on paper from three
                 days before kick-off
    rollover     one pick a day, fixed once made: odds 1.15 to 1.35, kick-off within 30 hours, stats agree, edge no worse
                 than -2%; the highest adjusted chance of winning. Followed as a paper chain from N5,000, the balance
                 riding on each pick, with up to six refills
    record       every flat value bet and every rollover pick, followed to its result with its closing-line value (the
                 bet's odds against the fair closing price: beating the close is the earliest sign an edge is real)
"""
import json
import math
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

HOUR = 3_600_000
DAY = 24 * HOUR
MARKETS = ("1", "X", "2", "1X", "12", "X2")
MARKET_NAME = {"1": "Home win", "X": "Draw", "2": "Away win", "1X": "Home or draw", "12": "Either team wins",
               "X2": "Away or draw"}
BOOK_KEYS = {"home": "1", "draw": "X", "away": "2", "home_or_draw": "1X", "home_or_away": "12", "draw_or_away": "X2"}

FLAT_MIN, FLAT_MAX, FLAT_EDGE = 1.20, 1.50, 0.02
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
}
DROP = {"fc", "cf", "afc", "sc", "ac", "club", "the", "de", "calcio", "1", "sv", "fk", "sk", "cd", "ud", "sd", "rc", "ca"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and "))
    s = " ".join(s.split())
    if s in ALIASES:
        return ALIASES[s]
    s = " ".join(w for w in s.split() if w not in DROP) or s
    return ALIASES.get(s, s)


def same_team(a: str, b: str) -> float:
    """How alike two team names are, 0 to 1, after aliases ("Man Utd" and "Manchester United" are 1)."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if (x in y or y in x) and min(len(x), len(y)) >= 4:
        return 0.92
    return SequenceMatcher(None, x, y).ratio()


def match_fixture(home: str, away: str, t_ms: int, fixtures: list[dict], within_h: float = 36, least: float = 0.72) -> dict | None:
    """The fixture in `fixtures` ({home, away, t_ms, ...}) that is the same game, by both names and kick-off."""
    best, score = None, 0.0
    for f in fixtures:
        if abs((f.get("t_ms") or 0) - t_ms) > within_h * HOUR:
            continue
        h, a = same_team(home, f["home"]), same_team(away, f["away"])
        if min(h, a) < least:
            continue
        if h + a > score:
            best, score = f, h + a
    return best


# ── fair chances ─────────────────────────────────────────────────────────────
def devig(odds: dict[str, float]) -> dict[str, float] | None:
    """{outcome: odds} → {outcome: fair chance}, the margin taken out in proportion."""
    inv = {k: 1 / v for k, v in odds.items() if v and v > 1}
    if len(inv) != len(odds) or not inv:
        return None
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()}


def markets_from_1x2(p: dict[str, float]) -> dict[str, float]:
    return {"1": p["1"], "X": p["X"], "2": p["2"], "1X": p["1"] + p["X"], "12": p["1"] + p["2"], "X2": p["X"] + p["2"]}


def wins(market: str, hg: int, ag: int) -> bool:
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


def team_as_favourite(history: list[dict], team: str, least: float = 0.65) -> dict | None:
    """How a team did when the market made it a clear favourite (fair chance of winning >= `least`)."""
    n = w = 0
    for g in history:
        p = hist_fair(g)
        if not p or g.get("hg") is None:
            continue
        side = "1" if same_team(team, g["home"]) >= 0.92 else "2" if same_team(team, g["away"]) >= 0.92 else None
        if side and p[side] >= least:
            n += 1
            w += wins(side, g["hg"], g["ag"])
    return {"n": n, "won": w} if n else None


def form(history: list[dict], team: str, before_ms: int, n: int = FORM_N) -> dict | None:
    """A team's last `n` league games before a time: points, goals and shots on target a game, results newest first."""
    games = []
    for g in history:
        if g.get("hg") is None or g["t_ms"] >= before_ms:
            continue
        if same_team(team, g["home"]) >= 0.92:
            games.append((g["t_ms"], g["hg"], g["ag"], g.get("hst"), g.get("ast")))
        elif same_team(team, g["away"]) >= 0.92:
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


# ── the board ────────────────────────────────────────────────────────────────
def build(books: dict[str, dict], sharp: list[dict], history: list[dict], cal: dict, now_ms: int) -> list[dict]:
    """Every Nigerian price matched to a sharp game, with the fair chance, the history-adjusted chance, the edge, the
    stats and the patterns. books: {book: {"t_ms", "rows": [{home, away, t_ms, league, odds: {market: price}}]}}."""
    out = []
    for book, got in books.items():
        age_h = (now_ms - got["t_ms"]) / HOUR
        for r in got["rows"]:
            if r["t_ms"] < now_ms + 10 * 60_000 or r["t_ms"] > now_ms + SHOW_DAYS * DAY:
                continue
            s = match_fixture(r["home"], r["away"], r["t_ms"], sharp)
            if not s:
                continue
            fair = markets_from_1x2(s["fair"])
            for m, price in r["odds"].items():
                if m not in fair or not price or price <= 1:
                    continue
                p_adj, pat = adjust(fair[m], m, s.get("league", ""), cal, price_kind(s["src"]))
                out.append({"key": f"{s['id']}:{m}", "match_id": s["id"], "home": s["home"], "away": s["away"],
                            "league": s.get("league", ""), "league_name": s.get("league_name", ""), "t_ms": s["t_ms"],
                            "market": m, "book": book, "odds": price, "odds_age_h": age_h, "fair_p": fair[m], "p": p_adj,
                            "fair_odds": 1 / fair[m], "edge": price * p_adj - 1, "sharp_src": s["src"], "fair1x2": s["fair"],
                            "sharp_age_h": (now_ms - s["read_ms"]) / HOUR, "pattern": pat})
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
        for t in (fav, opp):
            if t not in stats_cache:
                stats_cache[t] = form(history, t, x["t_ms"])
        x["fav"], x["fav_form"], x["opp_form"] = fav, stats_cache[fav], stats_cache[opp]
        x["agree"], x["agree_why"] = stats_agree(stats_cache[fav], stats_cache[opp])
        if ("rec", fav) not in stats_cache:
            stats_cache[("rec", fav)] = team_as_favourite(history, fav)
        x["fav_record"] = stats_cache[("rec", fav)]
    return sorted(rows, key=lambda x: -x["edge"])


def value_bets(board: list[dict], lo: float = FLAT_MIN, hi: float = FLAT_MAX, edge: float = FLAT_EDGE,
               before_ms: int | None = None) -> list[dict]:
    """Flat value bets; with before_ms, only those kicking off before it."""
    return [x for x in board if lo <= x["odds"] <= hi and x["edge"] >= edge and x["odds_age_h"] <= ODDS_FRESH_H
            and x["sharp_age_h"] <= SHARP_FRESH_H and x["market"] != "X" and (before_ms is None or x["t_ms"] <= before_ms)]


def rollover_pick(board: list[dict], now_ms: int) -> dict | None:
    """Today's one rollover bet (see the module note), or None if nothing qualifies."""
    c = [x for x in board if ROLL_MIN <= x["odds"] <= ROLL_MAX and x["t_ms"] <= now_ms + 30 * HOUR and x.get("agree")
         and x["edge"] >= ROLL_EDGE and x["odds_age_h"] <= ODDS_FRESH_H and x["sharp_age_h"] <= SHARP_FRESH_H
         and x["market"] != "X"]
    return max(c, key=lambda x: (x["p"], x["edge"])) if c else None


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
                "edge", "sharp_src")
        return {k: x.get(k) for k in keep}

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
                if c and c.get("fair"):
                    q = markets_from_1x2(c["fair"]).get(b["market"])
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

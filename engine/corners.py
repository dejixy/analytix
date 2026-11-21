"""
The count models: corners, shots, shots on target and goalkeeper saves, priced from three seasons of match stats
(football-data's HC/AC, HS/AS and HST/AST, with saves worked out as the other side's shots on target less its goals).

There's no sharp price for any of these within reach (Pinnacle's isn't in any free feed), so each is a model of its own,
less sure than anything priced from Pinnacle: each team's count for and against over its last LAST games, relative to
its league's average and pulled toward it (SHRINK games' worth of an average team), give each side's expected count:

    home expected = league home average × home team's count for × away team's count against   (as ratios)

Counts vary more than a plain count would (one side camps in the other's half), so each side's count is a negative
binomial whose spread is fitted per league; the total is the two added. A team's own count is priced too.

Codes:  <stat><who><O|U><line>5   stat C corners, SH shots, ST shots on target, SV goalkeeper saves; who "" for the
        match total, H or A for one side (for saves: that side's goalkeeper)
        CO95 = over 9.5 corners, SHHO125 = home over 12.5 shots, STU85 = under 8.5 shots on target,
        SVAO25 = away goalkeeper over 2.5 saves

check() is walk-forward: each past game priced only from games before it, then compared with what happened.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np

LAST, SHRINK = 30, 10


@dataclass(frozen=True)
class Stat:
    prefix: str
    name: str
    maxc: int
    total: tuple          # x.5 lines for the match total
    team: tuple           # x.5 lines for one side
    check: tuple          # (who, line) pairs shown on the page's model table


STATS = {
    "corners": Stat("C", "corners", 40, tuple(range(5, 15)), tuple(range(1, 9)),
                    (("", 8), ("", 9), ("", 10), ("", 11), ("H", 4), ("A", 3))),
    "shots": Stat("SH", "shots", 70, tuple(range(15, 36)), tuple(range(4, 21)),
                  (("", 22), ("", 25), ("H", 11), ("A", 9))),
    "sot": Stat("ST", "shots on target", 35, tuple(range(3, 16)), tuple(range(0, 10)),
                (("", 7), ("", 9), ("H", 3), ("A", 2))),
    "saves": Stat("SV", "goalkeeper saves", 25, tuple(range(2, 12)), tuple(range(0, 8)),
                  (("", 4), ("", 6), ("H", 2), ("A", 2))),
}
KIND = {"corners": "corners", "shots": "shots", "sot": "shots", "saves": "saves"}     # the page's market groups
_BY_PREFIX = {s.prefix: k for k, s in STATS.items()}
_CODE = re.compile(r"^(C|SH|ST|SV)([HA]?)([OU])(\d{1,2})5$")
MAXC = STATS["corners"].maxc
LINES = STATS["corners"].total


def pair(stat: str, g: dict) -> tuple[float, float] | None:
    """A finished game's (home, away) count for a stat; for saves, each side's goalkeeper's saves."""
    if stat == "corners":
        h, a = g.get("hc"), g.get("ac")
    elif stat == "shots":
        h, a = g.get("hs"), g.get("as")
    elif stat == "sot":
        h, a = g.get("hst"), g.get("ast")
    elif stat == "saves":
        if None in (g.get("hst"), g.get("ast"), g.get("hg"), g.get("ag")):
            return None
        h, a = g["ast"] - g["ag"], g["hst"] - g["hg"]          # the home keeper saves what the away side put on target
        if h < 0 or a < 0:
            return None
    else:
        return None
    return None if h is None or a is None else (float(h), float(a))


def code(over: bool, line: int, stat: str = "corners", who: str = "") -> str:
    return f"{STATS[stat].prefix}{who}{'O' if over else 'U'}{line}5"


def parse(market: str) -> tuple[str, str, bool, int] | None:
    """"CO95" -> ("corners", "", True, 9): the stat, whose count ("" both, H, A), over or under, the line's whole part."""
    m = _CODE.match(market)
    if not m:
        return None
    return _BY_PREFIX[m.group(1)], m.group(2), m.group(3) == "O", int(m.group(4))


def wins(market: str, h: float, a: float) -> bool:
    """Whether a count market won, given that stat's (home, away) counts."""
    _, who, over, line = parse(market)
    n = h + a if not who else h if who == "H" else a
    return (n >= line + 1) == over


def _nb(mu: float, r: float, maxc: int = MAXC) -> np.ndarray:
    """Negative binomial counts 0..maxc with mean mu and spread r (Poisson as r grows), the rest in the last."""
    k = np.arange(maxc + 1)
    if r > 1e4:
        logp = k * math.log(mu) - mu - np.array([math.lgamma(x + 1) for x in k])
    else:
        lg = np.array([math.lgamma(x + r) - math.lgamma(r) - math.lgamma(x + 1) for x in k])
        logp = lg + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu))
    p = np.exp(logp)
    p[-1] += max(0.0, 1.0 - float(p.sum()))
    return p / p.sum()


def total(mu_h: float, mu_a: float, r: float, maxc: int = MAXC) -> np.ndarray:
    """The match total 0..maxc (the rare rest counted in the last)."""
    d = np.convolve(_nb(mu_h, r, maxc), _nb(mu_a, r, maxc))[:maxc + 1]
    d[-1] += max(0.0, 1.0 - float(d.sum()))
    return d


def markets(dist: np.ndarray, stat: str = "corners", who: str = "", lines: tuple | None = None) -> dict[str, float]:
    """Over/under chances at each line from a count's spread."""
    out = {}
    for line in lines or (STATS[stat].total if not who else STATS[stat].team):
        o = float(dist[line + 1:].sum())
        out[code(True, line, stat, who)], out[code(False, line, stat, who)] = o, 1 - o
    return out


def priced(stat: str, mu_h: float, mu_a: float, r: float) -> dict[str, float]:
    """Every market of a stat for one game: the match total and each side's own count."""
    s = STATS[stat]
    return {**markets(total(mu_h, mu_a, r, s.maxc), stat), **markets(_nb(mu_h, r, s.maxc), stat, "H"),
            **markets(_nb(mu_a, r, s.maxc), stat, "A")}


class Model:
    """A stat's rates from history, kept as they stood after the last game read (feed games in time order)."""

    def __init__(self, stat: str = "corners"):
        self.stat = stat
        self.team: dict[str, deque] = defaultdict(lambda: deque(maxlen=LAST))     # (for, against)
        self.league: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # home sum, away sum, games
        self._found: dict[str, str | None] = {}

    def find(self, name: str) -> str | None:
        """A team's name as history has it (the sharp prices may spell it differently), if it has 8+ games of this."""
        if name not in self._found:
            from engine.betting import same_team
            hit = name if name in self.team else None
            if hit is None:
                score, hit = max(((same_team(name, t, 0.92), t) for t in self.team), default=(0.0, None))
                hit = hit if score >= 0.92 else None
            self._found[name] = hit if hit and len(self.team[hit]) >= 8 else None
        return self._found[name]

    def add(self, g: dict) -> None:
        c = pair(self.stat, g)
        if c is None:
            return
        h, a = c
        L = self.league[g["league"]]
        L[0] += h
        L[1] += a
        L[2] += 1
        self.team[g["home"]].append((h, a))
        self.team[g["away"]].append((a, h))

    def means(self, league: str | None) -> tuple[float, float]:
        L = self.league.get(league) if league else None
        if not L or L[2] < 50:                                     # an unknown league or cup: every league's
            n = sum(x[2] for x in self.league.values()) or 1
            return (sum(x[0] for x in self.league.values()) / n or 5.5, sum(x[1] for x in self.league.values()) / n or 4.5)
        return L[0] / L[2], L[1] / L[2]

    def _ratio(self, team: str | None, idx: int, avg: float) -> float:
        games = self.team.get(team) if team else None
        n = len(games) if games else 0
        s = sum(g[idx] for g in games) if games else 0.0
        return (s + SHRINK * avg) / ((n + SHRINK) * avg)

    def expect(self, home: str | None, away: str | None, league: str | None) -> tuple[float, float]:
        lh, la = self.means(league)
        avg = (lh + la) / 2
        mu_h = lh * self._ratio(home, 0, avg) * self._ratio(away, 1, avg)
        mu_a = la * self._ratio(away, 0, avg) * self._ratio(home, 1, avg)
        return max(mu_h, 0.3), max(mu_a, 0.3)


def _games(history: list[dict], stat: str) -> list[dict]:
    return sorted((g for g in history if pair(stat, g) is not None), key=lambda g: g["t_ms"])


def _walk(games: list[dict], stat: str = "corners", warm: int = 8):
    """Each game (in time order) with both sides' expected counts from earlier games only, once both teams have
    `warm` games behind them."""
    m = Model(stat)
    for g in games:
        if len(m.team.get(g["home"], ())) >= warm and len(m.team.get(g["away"], ())) >= warm:
            yield g, *m.expect(g["home"], g["away"], g["league"])
        m.add(g)


def spread(history: list[dict], stat: str = "corners") -> dict[str, float]:
    """Each league's negative binomial spread r for one side's count, from how far counts land from what the model
    expected (walk-forward): variance = mean + mean² / r."""
    num: dict[str, float] = defaultdict(float)
    den: dict[str, float] = defaultdict(float)
    cnt: dict[str, int] = defaultdict(int)
    for g, mh, ma in _walk(_games(history, stat), stat):
        for x, mu in zip(pair(stat, g), (mh, ma)):
            num[g["league"]] += mu * mu
            den[g["league"]] += (x - mu) ** 2 - mu
            cnt[g["league"]] += 1
    return {lg: (num[lg] / den[lg] if den[lg] > 0 else 1e5) for lg in num if cnt[lg] >= 200}


CHECK = (8, 9, 10, 11)


def check(history: list[dict], stat: str = "corners", last_n: int = 4000) -> dict:
    """Walk-forward on past games: each priced from earlier games only; at the stat's check lines, the games, what the
    model said on average, what happened, and the log loss against the league's plain rate (lower is better)."""
    games = _games(history, stat)
    if len(games) < 500:
        return {}
    s = STATS[stat]
    r = spread(games, stat)
    lines = s.check
    rate: dict = defaultdict(lambda: {k: [0, 0] for k in lines})
    out = {k: {"n": 0, "said": 0.0, "won": 0, "ll": 0.0, "ll_base": 0.0} for k in lines}
    start = games[max(len(games) - last_n, len(games) // 3)]["t_ms"]    # the first games only build the ratings up
    seen = 0

    def count(g, who):
        h, a = pair(stat, g)
        return h + a if not who else h if who == "H" else a
    for g, mh, ma in _walk(games, stat):
        while seen < len(games) and games[seen]["t_ms"] < g["t_ms"]:      # the league's plain rate, from earlier games
            e = games[seen]
            for who, line in lines:
                rate[e["league"]][(who, line)][0] += count(e, who) >= line + 1
                rate[e["league"]][(who, line)][1] += 1
            seen += 1
        if g["t_ms"] < start:
            continue
        mk = priced(stat, mh, ma, r.get(g["league"], 8.0))
        for who, line in lines:
            w, n = rate[g["league"]][(who, line)]
            if n < 30:
                continue
            p = min(max(mk[code(True, line, stat, who)], 1e-6), 1 - 1e-6)
            base = min(max(w / n, 1e-6), 1 - 1e-6)
            won = count(g, who) >= line + 1
            o = out[(who, line)]
            o["n"] += 1
            o["said"] += p
            o["won"] += won
            o["ll"] -= math.log(p if won else 1 - p)
            o["ll_base"] -= math.log(base if won else 1 - base)
    for o in out.values():
        if o["n"]:
            o["said"] /= o["n"]
            o["ll"] /= o["n"]
            o["ll_base"] /= o["n"]
    return {"games": len(games), "markets": {code(True, line, stat, who): v for (who, line), v in out.items()}}


def build(history: list[dict], stat: str = "corners") -> tuple[Model, dict[str, float]]:
    """The model as it stands after every game in history, and each league's spread."""
    m = Model(stat)
    for g in sorted(history, key=lambda g: g["t_ms"]):
        m.add(g)
    return m, spread(history, stat)

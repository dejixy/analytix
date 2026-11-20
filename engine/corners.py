"""
The corners model: total corners priced from three seasons of corner counts (football-data's HC and AC).

There's no sharp corners price within reach (Pinnacle's isn't in any free feed), so this is a model of its own, less sure
than anything priced from Pinnacle: each team's corners won and conceded over its last LAST games, relative to its
league's average and pulled toward it (SHRINK games' worth of an average team), give each side's expected corners:

    home expected = league home average × home team's corners won × away team's corners conceded   (as ratios)

Corners vary more than a plain count would (one side camps in the other's half), so each side's count is a negative
binomial whose spread is fitted per league; the total is the two added. markets() prices total corners over/under
5.5 to 14.5 (codes CO85 for over 8.5, CU85 for under 8.5, CO105, ...).

check() is walk-forward: each past game priced only from games before it, then compared with what happened.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np

LAST, SHRINK, MAXC = 30, 10, 40
LINES = (5, 6, 7, 8, 9, 10, 11, 12, 13, 14)        # x.5 lines


def code(over: bool, line: int) -> str:
    return f"C{'O' if over else 'U'}{line}5"


def parse(market: str) -> tuple[bool, int] | None:
    """("CO95") -> (True, 9) for over 9.5, or None if it isn't a corners market."""
    if len(market) >= 4 and market[0] == "C" and market[1] in "OU" and market[2:-1].isdigit() and market[-1] == "5":
        return market[1] == "O", int(market[2:-1])
    return None


def wins(market: str, hc: int, ac: int) -> bool:
    over, line = parse(market)
    return (hc + ac >= line + 1) == over


def _nb(mu: float, r: float) -> np.ndarray:
    """Negative binomial counts 0..MAXC with mean mu and spread r (Poisson as r grows)."""
    k = np.arange(MAXC + 1)
    if r > 1e4:
        logp = k * math.log(mu) - mu - np.array([math.lgamma(x + 1) for x in k])
    else:
        lg = np.array([math.lgamma(x + r) - math.lgamma(r) - math.lgamma(x + 1) for x in k])
        logp = lg + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu))
    p = np.exp(logp)
    return p / p.sum()


def total(mu_h: float, mu_a: float, r: float) -> np.ndarray:
    """Total corners 0..MAXC (the rare rest counted in the last)."""
    d = np.convolve(_nb(mu_h, r), _nb(mu_a, r))[:MAXC + 1]
    d[-1] += max(0.0, 1.0 - float(d.sum()))
    return d


def markets(dist: np.ndarray) -> dict[str, float]:
    out = {}
    for line in LINES:
        o = float(dist[line + 1:].sum())
        out[code(True, line)], out[code(False, line)] = o, 1 - o
    return out


class Model:
    """Corner rates from history, kept as they stood after the last game read (feed games in time order)."""

    def __init__(self):
        self.team: dict[str, deque] = defaultdict(lambda: deque(maxlen=LAST))     # (won, conceded)
        self.league: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # home sum, away sum, games
        self._found: dict[str, str | None] = {}

    def find(self, name: str) -> str | None:
        """A team's name as history has it (the sharp prices may spell it differently), if it has 8+ games of corners."""
        if name not in self._found:
            from engine.betting import same_team
            hit = name if name in self.team else None
            if hit is None:
                score, hit = max(((same_team(name, t, 0.92), t) for t in self.team), default=(0.0, None))
                hit = hit if score >= 0.92 else None
            self._found[name] = hit if hit and len(self.team[hit]) >= 8 else None
        return self._found[name]

    def add(self, g: dict) -> None:
        if g.get("hc") is None or g.get("ac") is None:
            return
        hc, ac = float(g["hc"]), float(g["ac"])
        L = self.league[g["league"]]
        L[0] += hc
        L[1] += ac
        L[2] += 1
        self.team[g["home"]].append((hc, ac))
        self.team[g["away"]].append((ac, hc))

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
        return max(mu_h, 0.5), max(mu_a, 0.5)


def _walk(games: list[dict], warm: int = 8):
    """Each game (in time order) with both sides' expected corners from earlier games only, once both teams have
    `warm` games behind them."""
    m = Model()
    for g in games:
        if len(m.team.get(g["home"], ())) >= warm and len(m.team.get(g["away"], ())) >= warm:
            yield g, *m.expect(g["home"], g["away"], g["league"])
        m.add(g)


def spread(history: list[dict]) -> dict[str, float]:
    """Each league's negative binomial spread r for one side's corners, from how far counts land from what the model
    expected (walk-forward): variance = mean + mean² / r."""
    games = sorted((g for g in history if g.get("hc") is not None and g.get("ac") is not None), key=lambda g: g["t_ms"])
    num: dict[str, float] = defaultdict(float)
    den: dict[str, float] = defaultdict(float)
    cnt: dict[str, int] = defaultdict(int)
    for g, mh, ma in _walk(games):
        for x, mu in ((g["hc"], mh), (g["ac"], ma)):
            num[g["league"]] += mu * mu
            den[g["league"]] += (x - mu) ** 2 - mu
            cnt[g["league"]] += 1
    return {lg: (num[lg] / den[lg] if den[lg] > 0 else 1e5) for lg in num if cnt[lg] >= 200}


CHECK = (8, 9, 10, 11)


def check(history: list[dict], last_n: int = 4000) -> dict:
    """Walk-forward on past games: each priced from earlier games only; for over 8.5 to 11.5, the games, what the model
    said on average, what happened, and the log loss against the league's plain rate (lower is better)."""
    games = sorted((g for g in history if g.get("hc") is not None and g.get("ac") is not None), key=lambda g: g["t_ms"])
    if len(games) < 500:
        return {}
    r = spread(games)
    rate: dict = defaultdict(lambda: {k: [0, 0] for k in CHECK})
    out = {k: {"n": 0, "said": 0.0, "won": 0, "ll": 0.0, "ll_base": 0.0} for k in CHECK}
    start = games[max(len(games) - last_n, len(games) // 3)]["t_ms"]    # the first games only build the ratings up
    seen = 0
    for g, mh, ma in _walk(games):
        while seen < len(games) and games[seen]["t_ms"] < g["t_ms"]:      # the league's plain rate, from earlier games
            e = games[seen]
            for line in CHECK:
                rate[e["league"]][line][0] += (e["hc"] + e["ac"]) >= line + 1
                rate[e["league"]][line][1] += 1
            seen += 1
        if g["t_ms"] < start:
            continue
        mk = markets(total(mh, ma, r.get(g["league"], 8.0)))
        t = g["hc"] + g["ac"]
        for line in CHECK:
            w, n = rate[g["league"]][line]
            if n < 30:
                continue
            p = min(max(mk[code(True, line)], 1e-6), 1 - 1e-6)
            base = min(max(w / n, 1e-6), 1 - 1e-6)
            won = t >= line + 1
            o = out[line]
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
    return {"games": len(games), "markets": {code(True, k): v for k, v in out.items()}}


def build(history: list[dict]) -> tuple[Model, dict[str, float]]:
    """The model as it stands after every game in history, and each league's spread."""
    m = Model()
    for g in sorted(history, key=lambda g: g["t_ms"]):
        m.add(g)
    return m, spread(history)

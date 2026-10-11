"""
Sports: what the real game says, independent of any order book.

    pre-game     the sportsbook line (ESPN shows it), with the bookmaker's margin taken out
    live         football (NFL, college): ESPN's own win-probability model, play by play
                 soccer: a goals model. Each side scores at a steady rate (Poisson), set so the model matches the
                 pre-match line; once the game is on, only the goals still to come are random:
                     P(home wins) = sum over remaining goals i, j of  P(i) P(j) [home + i > away + j]
                 so a goal in the 80th minute moves the odds the moment the score updates, while a thin
                 order book often hasn't caught up yet
    final        the result itself

Soccer markets here count regulation time (90 minutes plus stoppage), as their rules say.
"""
import math
from functools import lru_cache

REG_MIN = 93.0          # 90 minutes plus a typical stoppage time
MAX_GOALS = 10


def american_to_prob(ml) -> float | None:
    """-150 → 0.60, +130 → 0.435 (still with the bookmaker's margin in it)."""
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def devig(probs: list[float | None]) -> list[float] | None:
    """Take the bookmaker's margin out: scale the implied chances so they add up to 1."""
    if not probs or any(p is None or p <= 0 for p in probs):
        return None
    s = sum(probs)
    return [p / s for p in probs]


def _pois(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k) if lam > 0 else (1.0 if k == 0 else 0.0)


def three_way(lam_h: float, lam_a: float, h: int = 0, a: int = 0) -> tuple[float, float, float]:
    """(home win, draw, away win) when home scores ~Poisson(lam_h) more goals and away ~Poisson(lam_a), from h–a."""
    ph = [_pois(i, lam_h) for i in range(MAX_GOALS + 1)]
    pa = [_pois(j, lam_a) for j in range(MAX_GOALS + 1)]
    win = draw = lose = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            d = (h + i) - (a + j)
            if d > 0:
                win += x * y
            elif d == 0:
                draw += x * y
            else:
                lose += x * y
    s = win + draw + lose
    return win / s, draw / s, lose / s


@lru_cache(maxsize=512)
def fit_rates(p_home: float, p_draw: float, p_away: float) -> tuple[float, float]:
    """Full-match scoring rates (home, away) that reproduce a pre-match 3-way line. A coarse grid, then a fine one
    around the best point; cached, since a match's pre-game line is fitted once."""
    target = (p_home, p_draw, p_away)

    def err(mu, share):
        w, d, l = three_way(mu * share, mu * (1 - share))
        return (w - target[0]) ** 2 + (d - target[1]) ** 2 + (l - target[2]) ** 2

    best = min(((err(mu, sh), mu, sh) for mu in [1.0 + 0.25 * i for i in range(15)]
                for sh in [0.10 + 0.05 * j for j in range(17)]))
    _, mu0, sh0 = best
    best = min(((err(mu, sh), mu, sh) for mu in [mu0 - 0.25 + 0.025 * i for i in range(21)]
                for sh in [sh0 - 0.05 + 0.005 * j for j in range(21)] if mu > 0.2 and 0.02 < sh < 0.98))
    _, mu, sh = best
    return mu * sh, mu * (1 - sh)


def soccer_live(pre: tuple[float, float, float], minute: float, home_goals: int, away_goals: int) -> tuple[float, float, float]:
    """Live (home, draw, away) chances from the pre-match line, the minute and the score."""
    lam_h, lam_a = fit_rates(*(round(p, 3) for p in pre))
    left = max(0.0, (REG_MIN - min(minute, REG_MIN)) / REG_MIN)
    if left <= 0.005:
        d = home_goals - away_goals
        return (1.0, 0.0, 0.0) if d > 0 else (0.0, 1.0, 0.0) if d == 0 else (0.0, 0.0, 1.0)
    return three_way(lam_h * left, lam_a * left, home_goals, away_goals)


def soccer_minute(display_clock: str | None, period: int | None, detail: str | None = None) -> float | None:
    """ESPN's clock for soccer: "67'", "45'+2'", "HT" → minutes played."""
    if detail and "half" in detail.lower() and "time" in detail.lower():
        return 45.0
    s = str(display_clock or "").replace("'", " ").replace("+", " ").split()
    try:
        m = float(s[0]) + (float(s[1]) if len(s) > 1 else 0.0)
    except (ValueError, IndexError):
        return None
    return min(m, REG_MIN)

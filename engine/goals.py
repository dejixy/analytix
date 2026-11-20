"""
The goals model: every goals market priced from Pinnacle's own prices.

Pinnacle's win prices (home, draw, away) and its over/under 2.5 price pin down how many goals each side is expected to
score. fit() finds the two scoring rates (λ home, λ away) whose score grid matches those chances best: each side's goals
a Poisson count, with Dixon and Coles' small correction for 0-0, 1-0, 0-1 and 1-1 (RHO, chosen on past games). Without
an over/under price the three win chances alone fix both rates. From the grid, markets() prices the rest: over/under
0.5 to 4.5, each team's goals, both teams to score, draw no bet, and win-or-over.

check() replays it on past games (fitted on Pinnacle's prices before kick-off, compared with the final score), so the
page can show, market by market, what the model said against what happened.

Market codes (shared with engine/betting.py, the collector and the page):
    O15 U15 ... O45 U45     total goals over / under x.5
    HO05 HO15 HO25          home team x.5+ goals (HU05... under)
    AO05 AO15 AO25          away team x.5+ goals (AU05... under)
    GG NG                   both teams score / not
    DNB1 DNB2               draw no bet: a draw is refunded
    1|O15 1|O25 2|O15 2|O25 X|O25   home (away, draw) wins OR over x.5 goals
"""
from __future__ import annotations

import math

import numpy as np

N = 11                                            # 0..10 goals a side (more is too rare to matter)
GRID = np.arange(0.05, 4.5001, 0.025)
RHO = -0.06                                       # replaced by the history read's best fit (set_rho)
_g = np.arange(N)
_LOGF = np.array([math.lgamma(k + 1) for k in range(N)])
_PMF = np.exp(_g[None, :] * np.log(GRID)[:, None] - GRID[:, None] - _LOGF[None, :])        # (grid, goals)
_PMF /= _PMF.sum(axis=1, keepdims=True)
_H = np.add.outer(_g, -_g)                         # home goals - away goals
_T = np.add.outer(_g, _g)                          # total goals
MASKS = {"1": _H > 0, "X": _H == 0, "2": _H < 0, "O25": _T >= 3}


def set_rho(rho: float) -> None:
    global RHO
    RHO = float(rho)


def grid(lh: float, la: float, rho: float | None = None) -> np.ndarray:
    """The score grid (home goals × away goals) for two scoring rates."""
    rho = RHO if rho is None else rho
    ph = np.exp(_g * math.log(lh) - lh - _LOGF)
    pa = np.exp(_g * math.log(la) - la - _LOGF)
    m = np.outer(ph, pa)
    m[0, 0] *= 1 - lh * la * rho
    m[0, 1] *= 1 + lh * rho
    m[1, 0] *= 1 + la * rho
    m[1, 1] *= 1 - rho
    return m / m.sum()


def _coarse(rho: float) -> dict[str, np.ndarray]:
    """Each market's chance for every pair of rates on the grid (home rate × away rate), Dixon-Coles corrected."""
    out = {}
    P = _PMF
    lam = GRID
    for k, mask in MASKS.items():
        base = P @ mask.astype(float) @ P.T
        fix = np.zeros_like(base)
        cells = {(0, 0): -np.outer(lam, lam) * rho, (0, 1): np.outer(lam, np.ones_like(lam)) * rho,
                 (1, 0): np.outer(np.ones_like(lam), lam) * rho, (1, 1): -rho * np.ones((len(lam), len(lam)))}
        for (h, a), t in cells.items():
            if mask[h, a]:
                fix += t * np.outer(P[:, h], P[:, a])
        out[k] = base + fix
    return out


_COARSE: dict[float, dict] = {}


def fit(p1: float, px: float, p2: float, po: float | None = None, rho: float | None = None) -> tuple[float, float]:
    """The scoring rates (home, away) whose score grid best matches Pinnacle's fair chances."""
    rho = RHO if rho is None else rho
    c = _COARSE.get(rho)
    if c is None:
        c = _COARSE[rho] = _coarse(rho)
    err = (c["1"] - p1) ** 2 + (c["X"] - px) ** 2 + (c["2"] - p2) ** 2
    if po is not None:
        err = err + 2 * (c["O25"] - po) ** 2
    i, j = np.unravel_index(int(np.argmin(err)), err.shape)
    best, lh, la = None, GRID[i], GRID[j]
    for dh in np.arange(-0.025, 0.02501, 0.005):                  # a finer look around the best grid point
        for da in np.arange(-0.025, 0.02501, 0.005):
            h, a = max(0.02, GRID[i] + dh), max(0.02, GRID[j] + da)
            m = grid(h, a, rho)
            e = (m[MASKS["1"]].sum() - p1) ** 2 + (m[MASKS["X"]].sum() - px) ** 2 + (m[MASKS["2"]].sum() - p2) ** 2
            if po is not None:
                e += 2 * (m[MASKS["O25"]].sum() - po) ** 2
            if best is None or e < best:
                best, lh, la = e, h, a
    return float(lh), float(la)


def markets(m: np.ndarray) -> dict[str, float | tuple[float, float]]:
    """Every goals market's chance from a score grid. Draw no bet gives (win, refund) chances."""
    p1, px, p2 = float(m[_H > 0].sum()), float(m[_H == 0].sum()), float(m[_H < 0].sum())
    out: dict = {}
    for k in range(5):                                            # total goals over/under k + 0.5
        o = float(m[_T >= k + 1].sum())
        out[f"O{k}5"], out[f"U{k}5"] = o, 1 - o
    for k in range(3):                                            # each team's goals
        ho, ao = float(m[k + 1:, :].sum()), float(m[:, k + 1:].sum())
        out[f"HO{k}5"], out[f"HU{k}5"], out[f"AO{k}5"], out[f"AU{k}5"] = ho, 1 - ho, ao, 1 - ao
    gg = float(m[1:, 1:].sum())
    out["GG"], out["NG"] = gg, 1 - gg
    out["DNB1"], out["DNB2"] = (p1, px), (p2, px)
    for k in (1, 2):                                              # a result OR over k.5 goals
        over = _T >= k + 1
        out[f"1|O{k}5"] = float(m[(_H > 0) | over].sum())
        out[f"2|O{k}5"] = float(m[(_H < 0) | over].sum())
        out[f"X|O{k}5"] = float(m[(_H == 0) | over].sum())
    return out


def wins(market: str, hg: int, ag: int) -> bool | None:
    """Whether a goals market won on a final score (None: refunded, for draw no bet on a draw)."""
    t = hg + ag
    if market in ("DNB1", "DNB2"):
        return None if hg == ag else (hg > ag) == (market == "DNB1")
    if market in ("GG", "NG"):
        return (hg > 0 and ag > 0) == (market == "GG")
    if "|" in market:
        res, over = market.split("|")
        r = "1" if hg > ag else "2" if ag > hg else "X"
        return r == res or t >= int(over[1]) + 1
    if market[:2] in ("HO", "HU", "AO", "AU"):
        g = hg if market[0] == "H" else ag
        return (g >= int(market[2]) + 1) == (market[1] == "O")
    if market[0] in "OU" and len(market) == 3:
        return (t >= int(market[1]) + 1) == (market[0] == "O")
    raise ValueError(f"not a goals market: {market}")


# ── on past games ────────────────────────────────────────────────────────────
CHECK = ("O15", "O35", "U15", "U35", "HO05", "HO15", "AO05", "AO15", "GG", "NG", "1|O15", "2|O15", "1|O25", "2|O25")


def check(history: list[dict], devig, rhos=(-0.12, -0.08, -0.04, 0.0), sample: int = 1000, last_n: int = 4000) -> dict:
    """The model on past games with Pinnacle's prices: the best Dixon-Coles correction (on a sample, by how likely the
    real scores were), then for each market the games, what the model said on average, what happened, and how much
    better the model's chances fit the results than the league's plain rate (log loss, lower is better)."""
    games = []
    for g in history:
        if g.get("hg") is None:
            continue
        f = devig({"1": g.get("PSH"), "X": g.get("PSD"), "2": g.get("PSA")}) if g.get("PSH") and g.get("PSD") and g.get("PSA") else None
        if not f:
            continue
        ou = devig({"O25": g.get("P>2.5"), "U25": g.get("P<2.5")}) if g.get("P>2.5") and g.get("P<2.5") else None
        games.append((g, f, ou["O25"] if ou else None))
    if len(games) < 200:
        return {}
    games = sorted(games, key=lambda x: x[0]["t_ms"])[-last_n:]     # the most recent games: enough, and quick
    step = max(1, len(games) // sample)
    best_rho, best_ll = RHO, None
    for rho in rhos:
        ll = 0.0
        for g, f, po in games[::step]:
            m = grid(*fit(f["1"], f["X"], f["2"], po, rho), rho)
            ll -= math.log(max(float(m[min(g["hg"], N - 1), min(g["ag"], N - 1)]), 1e-9))
        if best_ll is None or ll < best_ll:
            best_rho, best_ll = rho, ll
    rate: dict = {}
    for g, _, _ in games:                                         # each league's plain rate, the yardstick
        r = rate.setdefault(g["league"], {k: [0, 0] for k in CHECK})
        for k in CHECK:
            r[k][0] += bool(wins(k, g["hg"], g["ag"]))
            r[k][1] += 1
    out = {k: {"n": 0, "said": 0.0, "won": 0, "ll": 0.0, "ll_base": 0.0} for k in CHECK}
    for g, f, po in games:
        mk = markets(grid(*fit(f["1"], f["X"], f["2"], po, best_rho), best_rho))
        for k in CHECK:
            p = min(max(mk[k], 1e-6), 1 - 1e-6)
            w, n = rate[g["league"]][k]
            base = min(max(w / n, 1e-6), 1 - 1e-6)
            won = bool(wins(k, g["hg"], g["ag"]))
            o = out[k]
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
    return {"rho": best_rho, "games": len(games), "with_ou": sum(1 for *_, po in games if po is not None), "markets": out}

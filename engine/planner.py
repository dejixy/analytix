"""
Position planner: what to expect from a trade before you take it.

Given a side, margin, leverage and how long you'll hold, it answers:

  liquidation   the price (Hyperliquid's formula) and the chance price reaches it before you exit
  safe leverage the most leverage that keeps that chance under 1% / 5% for this hold
  path          how far against you price typically goes before you exit, and the chance of
                touching each level on the way (stops, targets)
  exit          the spread of outcomes when you close, after fees, slippage and funding
  costs         fees, slippage on the live book, funding expected over the hold

How the paths are made — filtered historical simulation (Barone-Adesi et al.), the method
risk desks use for VaR:

  1. Take the coin's own candles: 1-hour (≈200 days) for holds over 6h, 5-minute (≈17 days)
     for shorter ones. For each candle record three moves from the previous close: to the
     close, to the low, to the high — the wicks are what liquidate people.
  2. Fit a GARCH(1,1) to the close-to-close moves (variance targeting, quasi-likelihood over a
     grid). It says how volatile each hour was *expected* to be, given the hours before it.
  3. Divide each candle's three moves by that expected volatility. What's left is the
     coin's shape of surprise — fat tails, long down-wicks — with today's regime taken out.
  4. Simulate 20,000 paths: each step draws a random historical candle, scales it by the
     volatility the GARCH expects *now*, and updates that volatility with the simulated move,
     so a big move makes the next ones bigger, as in real markets.
  5. Read off each path's lowest low, highest high and final price.

No direction is assumed: historical drift is removed, so a long and a short of the same size
face mirror-image odds except where the coin's own wicks are lopsided. This planner says how
much room a trade needs, not which way price will go.

Assumptions, stated in the result: isolated margin, entry and exit at market now and at the
end, liquidation counted as losing the whole margin (backstop liquidations keep the maintenance
margin; book liquidations return what's left), candle wicks are last-trade prices while
liquidations use the mark price (so wicks slightly overstate liquidation risk).
"""
import math
from dataclasses import dataclass, field

import numpy as np

TAKER_FEE = 0.00045              # Hyperliquid base tier, perps
N_PATHS = 20_000
MAX_PATH_STEPS = 24_000_000      # paths × steps cap, so a week of 5-minute steps stays under a second or two
MIN_CANDLES = 300
DEFAULT_MAX_LEVERAGE = 20
FUNDING_HALF_LIFE_H = 8.0        # how fast today's funding drifts back to the weekly average
SHORT_HOLD_H = 6.0               # holds up to this use 5-minute candles; longer ones hourly candles
VAR_CAP = 100.0                  # a simulated variance never exceeds 100× the long-run level
GRID_ALPHA = (0.02, 0.04, 0.07, 0.10, 0.15, 0.22)
GRID_PERSISTENCE = (0.70, 0.85, 0.93, 0.97, 0.99, 0.996)
TOUCH_LEVELS_PCT = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 50)
TOUCH_TARGETS = (0.6, 0.35, 0.15, 0.05)
CURVE_LEVERAGES = (1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0)


@dataclass(frozen=True, slots=True)
class Candles:
    step_s: int
    close: np.ndarray
    low: np.ndarray
    high: np.ndarray

    def __len__(self) -> int:
        return len(self.close)


def candles_from_rows(rows: list[dict], step_s: int, now_ms: int | None = None) -> Candles:
    """Hyperliquid candleSnapshot rows → arrays, oldest first, dropping the candle still forming."""
    rows = sorted(rows, key=lambda r: int(r["t"]))
    if now_ms is not None:
        rows = [r for r in rows if int(r["T"]) < now_ms]
    return Candles(step_s, np.array([float(r["c"]) for r in rows]), np.array([float(r["l"]) for r in rows]),
                   np.array([float(r["h"]) for r in rows]))


def candles_from_bars(bars, step_s: int) -> Candles:
    """Aggregate saved bars (1-minute live bars and 5-minute backfilled candles) into step_s candles.
    Only complete, gap-free buckets count; the newest (still forming) bucket is dropped."""
    buckets: dict[int, list] = {}
    for b in bars:
        if b.span_s > step_s or not b.close:
            continue
        buckets.setdefault(b.timestamp // (step_s * 1000), []).append(b)
    keys = sorted(buckets)[:-1]
    close, low, high = [], [], []
    prev = None
    for k in keys:
        bs = sorted(buckets[k], key=lambda b: b.timestamp)
        if sum(b.span_s for b in bs) < step_s or (prev is not None and k != prev + 1):
            close, low, high = [], [], []           # a gap: start the run again so returns stay consecutive
            prev = None
            if sum(b.span_s for b in bs) < step_s:
                continue
        close.append(bs[-1].close)
        low.append(min(b.low for b in bs))
        high.append(max(b.high for b in bs))
        prev = k
    return Candles(step_s, np.array(close), np.array(low), np.array(high))


# ── the model ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RiskModel:
    step_s: int
    resid: np.ndarray        # (n, 3): standardised moves to close, low, high
    omega: float
    alpha: float
    beta: float
    var_next: float          # expected variance of the next step's log move
    var_long: float          # long-run variance per step
    n: int
    kind: str                # "fhs" (from the coin's candles) | "rough" (not enough history)

    @property
    def vol_ratio(self) -> float:
        """Volatility expected now vs this coin's long-run level."""
        return math.sqrt(self.var_next / self.var_long) if self.var_long > 0 else 1.0

    @property
    def span_days(self) -> float:
        return self.n * self.step_s / 86_400


def _garch_filter(r2: np.ndarray, var_long: float, alpha: float, beta: float) -> tuple[np.ndarray, float]:
    """Conditional variances (one per step, plus the forecast for the next) and the quasi-likelihood."""
    omega = var_long * (1 - alpha - beta)
    out = np.empty(len(r2) + 1)
    v = var_long
    for i, x in enumerate(r2.tolist()):
        out[i] = v
        v = omega + alpha * x + beta * v
    out[-1] = v
    s = out[:-1]
    return out, float(np.sum(np.log(s) + r2 / s))


def fit_model(c: Candles) -> RiskModel | None:
    if len(c) < MIN_CANDLES + 1:
        return None
    ref = c.close[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(c.close[1:] / ref)
        lo = np.log(c.low[1:] / ref)
        hi = np.log(c.high[1:] / ref)
    ok = np.isfinite(r) & np.isfinite(lo) & np.isfinite(hi)
    r, lo, hi = r[ok], lo[ok], hi[ok]
    if len(r) < MIN_CANDLES:
        return None
    mu = float(r.mean())                                   # no directional view: remove the history's drift
    r = r - mu
    lo = np.minimum(np.minimum(lo - mu, 0.0), r)           # a candle's low is at or below where it started and ended
    hi = np.maximum(np.maximum(hi - mu, 0.0), r)
    r2 = r * r
    var_long = float(r2.mean())
    if var_long <= 0:
        return None
    best = None
    for a in GRID_ALPHA:
        for p in GRID_PERSISTENCE:
            b = p - a
            if b <= 0:
                continue
            var, nll = _garch_filter(r2, var_long, a, b)
            if best is None or nll < best[0]:
                best = (nll, a, b, var)
    _, a, b, var = best
    sd = np.sqrt(var[:-1])
    z = _centred(np.column_stack([r / sd, lo / sd, hi / sd]))
    return RiskModel(c.step_s, z, var_long * (1 - a - b), a, b, float(var[-1]), var_long, len(r), "fhs")


def _centred(z: np.ndarray) -> np.ndarray:
    """Residuals with exactly zero mean and unit variance. Even a tiny average in the pool compounds over
    hundreds of steps into a drift — a view on direction the planner must not have."""
    mean = float(z[:, 0].mean())
    close = z[:, 0] - mean
    low = np.minimum(np.minimum(z[:, 1] - mean, 0.0), close)
    high = np.maximum(np.maximum(z[:, 2] - mean, 0.0), close)
    scale = float(np.std(close)) or 1.0
    return np.column_stack([close, low, high]) / scale


def rough_model(sigma_1s_bps: float, step_s: int, seed: int = 7, n: int = 20_000, dof: float = 4.0) -> RiskModel:
    """Not enough candles: a fat-tailed (Student-t, 4 d.o.f.) random walk at the volatility seen in the live
    feed, with each step's low and high drawn from the Brownian bridge between its start and end."""
    rng = np.random.default_rng(seed)
    t = rng.standard_t(dof, n) * math.sqrt((dof - 2) / dof)
    u1, u2 = rng.random(n), rng.random(n)
    lo = (t - np.sqrt(t * t - 2 * np.log(u1))) / 2         # exact bridge extremes for unit variance
    hi = (t + np.sqrt(t * t - 2 * np.log(u2))) / 2
    var = (sigma_1s_bps / 10_000) ** 2 * step_s
    return RiskModel(step_s, _centred(np.column_stack([t, lo, hi])), var, 0.0, 0.0, var, var, 0, "rough")


@dataclass(frozen=True, slots=True)
class Paths:
    final: np.ndarray        # log move from entry to exit
    low: np.ndarray          # lowest log move on the way (≤ 0)
    high: np.ndarray         # highest log move on the way (≥ 0)
    steps: int
    step_s: int


def simulate(m: RiskModel, hours: float, n_paths: int = N_PATHS, seed: int = 11) -> Paths:
    steps = max(1, math.ceil(hours * 3600 / m.step_s))
    n_paths = int(min(n_paths, max(2_000, MAX_PATH_STEPS // steps)))
    rng = np.random.default_rng(seed)
    cum = np.zeros(n_paths)
    low = np.zeros(n_paths)
    high = np.zeros(n_paths)
    var = np.full(n_paths, m.var_next)
    cap = VAR_CAP * m.var_long
    zc, zl, zh = m.resid[:, 0], m.resid[:, 1], m.resid[:, 2]
    for _ in range(steps):
        k = rng.integers(0, len(zc), n_paths)
        s = np.sqrt(var)
        np.minimum(low, cum + zl[k] * s, out=low)
        np.maximum(high, cum + zh[k] * s, out=high)
        move = zc[k] * s
        cum += move
        if m.alpha or m.beta:
            var = np.minimum(m.omega + m.alpha * move * move + m.beta * var, cap)
    return Paths(cum, low, high, steps, m.step_s)


# ── the plan ─────────────────────────────────────────────────────────────────
def maintenance_rate(max_leverage: float) -> float:
    """Hyperliquid: maintenance margin is half the initial margin at max leverage."""
    return 1 / (2 * max_leverage)


def liquidation_price(entry: float, side: int, leverage: float, maint: float) -> float | None:
    """Isolated margin. Hyperliquid's liq_price = price − side·margin_available/size/(1 − l·side) with
    margin_available = margin − maintenance at entry, which simplifies to entry·(1 − side/L)/(1 − side·m).
    None for a 1× long, which can't be liquidated."""
    p = entry * (1 - side / leverage) / (1 - side * maint)
    return p if p > 0 else None


def _adverse(paths: Paths, side: int) -> np.ndarray:
    """How far each path went against the position at its worst, as a positive log move."""
    return -paths.low if side > 0 else paths.high


def _favourable(paths: Paths, side: int) -> np.ndarray:
    return paths.high if side > 0 else -paths.low


def liq_probability(paths: Paths, side: int, entry: float, leverage: float, maint: float,
                    adverse: np.ndarray | None = None) -> float:
    liq = liquidation_price(entry, side, leverage, maint)
    if liq is None:
        return 0.0
    dist = math.log(entry / liq) if side > 0 else math.log(liq / entry)
    adverse = _adverse(paths, side) if adverse is None else adverse
    return float(np.mean(adverse >= dist))


@dataclass(frozen=True, slots=True)
class Costs:
    fees: float
    slippage: float
    slippage_known: bool
    funding: float               # positive = you pay
    funding_now_apr: float | None
    funding_avg_apr: float | None

    @property
    def total(self) -> float:
        return self.fees + self.slippage + self.funding


def expected_funding_rate(hours: float, now: float | None, avg: float | None,
                          half_life_h: float = FUNDING_HALF_LIFE_H) -> float:
    """Sum of hourly funding rates expected over the hold: today's rate drifting back to the weekly average."""
    if now is None and avg is None:
        return 0.0
    if avg is None:
        return now * hours
    if now is None:
        return avg * hours
    tau = half_life_h / math.log(2)
    return avg * hours + (now - avg) * tau * (1 - math.exp(-hours / tau))


@dataclass(frozen=True, slots=True)
class Touch:
    move_pct: float          # signed price move from entry
    price: float
    pnl: float               # your P&L if you closed there
    prob: float              # chance price trades there before you exit
    kind: str                # "against" | "for" | "liq"


@dataclass(frozen=True, slots=True)
class Plan:
    coin: str
    side: int
    margin: float
    leverage: float
    notional: float
    hours: float
    entry: float
    max_leverage: float
    max_leverage_known: bool
    maint: float
    liq_price: float | None
    liq_distance_pct: float | None
    liq_prob: float
    liq_sigmas: float | None             # distance to liquidation in typical hold-length moves
    safe_leverage: dict[str, float]      # "1" / "5" → most leverage with liq chance under that %
    leverage_curve: tuple[tuple[float, float], ...]
    sigma_pct: float                     # typical move over the hold (std), %
    range_pct: tuple[float, float]       # 5th and 95th percentile price move at exit, %
    drawdown_pct: tuple[float, float]    # median and 1-in-4 worst move against you before exit, %
    touches: tuple[Touch, ...]
    outcome: dict[str, float]            # P&L percentiles at exit after costs: p5 … p95
    prob_profit: float
    costs: Costs
    breakeven_pct: float
    model: RiskModel
    paths_n: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def _pnl(notional: float, side: int, log_move: np.ndarray | float):
    return notional * (np.expm1(log_move) if side > 0 else -np.expm1(log_move))


def _safe_leverage(paths, side, entry, maint, max_lev, threshold, adverse) -> float:
    best = 1.0
    lev = 1.0
    while lev <= max_lev + 1e-9:
        if liq_probability(paths, side, entry, lev, maint, adverse) > threshold:
            break
        best = lev
        lev += 0.5 if lev < 10 else 1.0
    return best


def make_plan(*, coin: str, side: int, margin: float, leverage: float, hours: float, entry: float,
              paths: Paths, model: RiskModel, max_leverage: float | None, entry_slip_bps: float | None,
              exit_slip_bps: float | None, funding_now: float | None, funding_avg: float | None) -> Plan:
    known = max_leverage is not None
    max_lev = float(max_leverage or DEFAULT_MAX_LEVERAGE)
    maint = maintenance_rate(max_lev)
    notional = margin * leverage
    adverse, favourable = _adverse(paths, side), _favourable(paths, side)

    liq = liquidation_price(entry, side, leverage, maint)
    liq_dist = None if liq is None else (math.log(entry / liq) if side > 0 else math.log(liq / entry))
    liq_hit = adverse >= liq_dist if liq_dist is not None else np.zeros(len(adverse), bool)
    sigma = float(np.std(paths.final))

    # costs
    fees = 2 * TAKER_FEE * notional
    slip_known = entry_slip_bps is not None and exit_slip_bps is not None
    slippage = ((entry_slip_bps or 0) + (exit_slip_bps or 0)) / 10_000 * notional
    funding = side * expected_funding_rate(hours, funding_now, funding_avg) * notional
    to_apr = lambda f: None if f is None else f * 24 * 365 * 100
    costs = Costs(fees, slippage, slip_known, funding, to_apr(funding_now), to_apr(funding_avg))

    # outcome at exit: liquidated paths lose the margin, the rest pay costs
    pnl = np.where(liq_hit, -margin, _pnl(notional, side, paths.final) - costs.total)
    q = np.percentile(pnl, [5, 25, 50, 75, 95])
    outcome = dict(zip(("p5", "p25", "p50", "p75", "p95"), (float(x) for x in q)))

    # touches on the way: the round levels whose chance of trading before you exit is nearest 60%, 35%, 15%
    # and 5%, so the table spans "noise will hit this" to "only a real move gets here"
    touches: list[Touch] = []
    for kind, excursion, sign in (("against", adverse, -side), ("for", favourable, side)):
        cands = []
        for pct in TOUCH_LEVELS_PCT:
            move = sign * pct / 100
            dist = abs(math.log1p(move))
            if kind == "against" and liq_dist is not None and dist >= liq_dist:
                break
            p = float(np.mean(excursion >= dist))
            if 0.02 <= p <= 0.9:
                cands.append(Touch(move * 100, entry * (1 + move), float(_pnl(notional, side, math.log1p(move))), p, kind))
        picked: list[Touch] = []
        for target in TOUCH_TARGETS:
            left = [t for t in cands if t not in picked]
            if left:
                picked.append(min(left, key=lambda t: abs(math.log(t.prob / target))))
        touches += sorted(set(picked), key=lambda t: abs(t.move_pct))
    if liq is not None:
        touches.append(Touch((liq / entry - 1) * 100, liq, -margin, float(liq_hit.mean()), "liq"))

    lo5, hi95 = np.percentile(paths.final, [5, 95])
    dd50, dd75 = np.percentile(adverse, [50, 75])
    against = lambda a: float(np.expm1(-a) * 100) if side > 0 else float(np.expm1(a) * 100)   # signed price move
    curve_levs = sorted({x for x in CURVE_LEVERAGES if x <= max_lev} | {float(leverage), max_lev})
    curve = tuple((lv, liq_probability(paths, side, entry, lv, maint, adverse)) for lv in curve_levs)
    notes = []
    if not known:
        notes.append(f"Hyperliquid's max leverage for {coin} wasn't available; assumed {max_lev:.0f}× "
                     f"(maintenance margin {maint * 100:.2f}%).")
    if model.kind == "rough":
        notes.append("Rough estimate: not enough price history yet (live mode downloads it), so this uses a "
                     "fat-tailed random walk at the volatility seen in the feed.")
    if not slip_known:
        notes.append("Your size goes beyond the visible order book, so slippage isn't included.")

    return Plan(
        coin=coin, side=side, margin=margin, leverage=leverage, notional=notional, hours=hours, entry=entry,
        max_leverage=max_lev, max_leverage_known=known, maint=maint, liq_price=liq,
        liq_distance_pct=None if liq is None else (liq / entry - 1) * 100,
        liq_prob=float(liq_hit.mean()),
        liq_sigmas=None if liq_dist is None or sigma <= 0 else liq_dist / sigma,
        safe_leverage={"1": _safe_leverage(paths, side, entry, maint, max_lev, 0.01, adverse),
                       "5": _safe_leverage(paths, side, entry, maint, max_lev, 0.05, adverse)},
        leverage_curve=curve,
        sigma_pct=float(np.expm1(sigma) * 100),
        range_pct=(float(np.expm1(lo5) * 100), float(np.expm1(hi95) * 100)),
        drawdown_pct=(against(dd50), against(dd75)),
        touches=tuple(touches),
        outcome=outcome,
        prob_profit=float(np.mean(pnl > 0)),
        costs=costs,
        breakeven_pct=costs.total / notional * 100 if notional else 0.0,
        model=model,
        paths_n=len(paths.final),
        notes=tuple(notes),
    )

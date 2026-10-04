"""
Position planner: what to expect from a trade before you take it.

Given a side, margin, leverage and how long you'll hold, it answers:

  liquidation   the price (Hyperliquid's formula) and the chance price reaches it before you exit
  safe leverage the most leverage that keeps that chance under 1% / 5% for this hold
  path          how far against you price typically goes before you exit, and the chance of
                touching each level on the way (stops, targets) while you're still in the trade
  exit          the spread of outcomes when you close, after fees, slippage and funding
  costs         fees, slippage on the live book, funding expected over the hold

How the paths are made — filtered historical simulation (Barone-Adesi et al.), the method
risk desks use for VaR:

  1. Take the coin's own candles: 1-hour (up to ~200 days) for holds over 6h, 5-minute (up to
     ~17 days) for shorter ones. For each candle record three moves from the previous close:
     to the close, to the low, to the high — the wicks are what liquidate people.
  2. Take out the time of day: crypto is busier at some hours (and quieter at weekends), so each
     move is divided by its hour's usual volatility (shrunk toward 1 where data is thin).
  3. Fit a GARCH(1,1) to the close-to-close moves (variance targeting, quasi-likelihood on a
     coarse grid, then a fine one around the best point). It says how volatile each candle was
     *expected* to be, given the ones before it.
  4. Divide each candle's three moves by that expected volatility. What's left is the coin's
     shape of surprise — fat tails, long down-wicks — with the regime taken out.
  5. Bring the volatility up to the moment: run the GARCH forward through every candle since
     the fit and through the move so far in the candle still forming, so a crash ten minutes
     ago counts.
  6. Simulate 20,000 paths: each step draws a random historical candle, scales it by the
     volatility expected for that step (GARCH × time of day), and updates the GARCH with the
     simulated move, so a big move makes the next ones bigger, as in real markets.
  7. Read off each path's lowest low, highest high, final price, and the best price it reached
     before it would have been liquidated.

No direction is assumed: historical drift is removed, so a long and a short of the same size
face mirror-image odds except where the coin's own wicks are lopsided. This planner says how
much room a trade needs, not which way price will go.

Assumptions, stated in the result: isolated margin; entry and exit at market now and at the end;
liquidation costs the whole margin plus entry costs (backstop liquidations keep the maintenance
margin; book liquidations return what's left); candle wicks are last-trade prices while
liquidations use the mark price, so wicks slightly overstate liquidation risk; inside the candle
where a path is liquidated, the liquidation is assumed to come before that candle's best price.
"""
import math
from dataclasses import dataclass, field

import numpy as np

TAKER_FEE = 0.00045              # Hyperliquid base tier, perps
N_PATHS = 20_000
MAX_PATH_STEPS = 24_000_000      # paths × steps cap, so a week of 5-minute steps stays under a second or two
MIN_CANDLES = 300
DEFAULT_MAX_LEVERAGE = 10        # when Hyperliquid's max isn't known: 5% maintenance, the cautious case for majors
FUNDING_HALF_LIFE_H = 8.0        # how fast today's funding drifts back to the weekly average
SHORT_HOLD_H = 6.0               # holds up to this use 5-minute candles; longer ones hourly candles
VAR_CAP = 100.0                  # a simulated variance never exceeds 100× the long-run level
SEASON_SHRINK_DAYS = 10          # an hour of the day needs ~10 days of data before its own volatility counts fully
WEEKEND_SHRINK_DAYS = 4          # …and the weekend effect a few weekend days
COARSE_ALPHA = (0.01, 0.03, 0.05, 0.08, 0.11, 0.15, 0.20, 0.25)
COARSE_PERSISTENCE = (0.60, 0.80, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.998)
TOUCH_LEVELS_PCT = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 50)
TOUCH_TARGETS = (0.6, 0.35, 0.15, 0.05)
CURVE_LEVERAGES = (1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0)
DAY_MS = 86_400_000
MAX_NOWCAST_STEPS = 5_000


@dataclass(frozen=True, slots=True)
class Candles:
    step_s: int
    t: np.ndarray            # open time, ms
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
    return Candles(step_s, np.array([int(r["t"]) for r in rows], dtype=np.int64),
                   np.array([float(r["c"]) for r in rows]), np.array([float(r["l"]) for r in rows]),
                   np.array([float(r["h"]) for r in rows]))


def candles_from_bars(bars, step_s: int, keep_last_run_only: bool = True) -> Candles:
    """Aggregate saved bars (1-minute live bars and 5-minute backfilled candles) into step_s candles.
    Only complete buckets count, the newest (still forming) bucket is dropped, and only the latest
    gap-free run is kept so consecutive candles really are consecutive."""
    step_ms = step_s * 1000
    buckets: dict[int, list] = {}
    for b in bars:
        if b.span_s > step_s or not b.close:
            continue
        buckets.setdefault(b.timestamp // step_ms, []).append(b)
    keys = sorted(buckets)[:-1]
    t, close, low, high = [], [], [], []
    prev = None
    for k in keys:
        bs = sorted(buckets[k], key=lambda b: b.timestamp)
        complete = sum(b.span_s for b in bs) >= step_s
        if not complete or (prev is not None and k != prev + 1):
            if keep_last_run_only:
                t, close, low, high = [], [], [], []
            prev = None
            if not complete:
                continue
        t.append(k * step_ms)
        close.append(bs[-1].close)
        low.append(min(b.low for b in bs))
        high.append(max(b.high for b in bs))
        prev = k
    return Candles(step_s, np.array(t, dtype=np.int64), np.array(close), np.array(low), np.array(high))


# ── time of day ──────────────────────────────────────────────────────────────
def season_bucket(t_ms) -> np.ndarray:
    """0–23: hour of day (UTC) on weekdays; 24–47: the same hours at weekends."""
    t = np.asarray(t_ms, dtype=np.int64)
    hour = (t // 3_600_000) % 24
    weekday = (t // DAY_MS + 3) % 7                 # 1 Jan 1970 was a Thursday; Monday = 0
    return hour + 24 * (weekday >= 5)


def seasonal_factors(t_ms: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Volatility multiplier for each of the 48 buckets (Andersen–Bollerslev style, made robust):
      · each move is divided by the average size of moves on its own day, so a volatile week raises
        every hour of that week alike instead of masquerading as a volatile hour
      · mean |move| per hour of day, not mean move² — one fat-tailed candle can't make an hour look busy
      · one weekend multiplier, measured day by day, on top of the hourly shape
      · neighbouring hours blended (¼ ½ ¼) and each hour shrunk toward 1 by how many *days* it was seen on
        (seventeen days of 5-minute candles are 200 candles an hour but still only 17 looks at that hour)
    Normalised so the factors average to 1 in variance over the history."""
    b = season_bucket(t_ms)
    hour, weekend = b % 24, b >= 24
    a = np.abs(r)
    if float(a.mean()) <= 0:
        return np.ones(48)
    day = (np.asarray(t_ms, dtype=np.int64) // DAY_MS)
    day = day - day.min()
    cnt = np.bincount(day).astype(float)
    day_mean = np.bincount(day, weights=a) / np.maximum(cnt, 1)
    full = cnt >= 0.5 * np.median(cnt)                   # days with enough candles to stand on
    use = full[day] & (day_mean[day] > 0)
    if use.sum() < 100:
        return np.ones(48)
    z = a[use] / day_mean[day[use]]
    h = hour[use]
    n = np.bincount(h, minlength=24).astype(float)
    hourly = np.bincount(h, weights=z, minlength=24) / np.maximum(n, 1)
    hourly = np.where(n > 0, hourly / float(z.mean()), 1.0)
    hourly = 0.25 * np.roll(hourly, 1) + 0.5 * hourly + 0.25 * np.roll(hourly, -1)
    t_sorted = np.sort(np.asarray(t_ms, dtype=np.int64))
    hours_per_candle = float(np.median(np.diff(t_sorted))) / 3_600_000 if len(t_sorted) > 1 else 1.0
    days_seen = n * min(hours_per_candle, 1.0)
    hourly = (days_seen * hourly + SEASON_SHRINK_DAYS) / (days_seen + SEASON_SHRINK_DAYS)
    days = np.flatnonzero(full & (cnt > 0))
    is_wkend = (days + int(np.asarray(t_ms).min() // DAY_MS) + 3) % 7 >= 5
    wk = 1.0
    if is_wkend.any() and (~is_wkend).any():
        ratio = float(day_mean[days[is_wkend]].mean() / day_mean[days[~is_wkend]].mean())
        k = float(is_wkend.sum())
        wk = (k * ratio + WEEKEND_SHRINK_DAYS) / (k + WEEKEND_SHRINK_DAYS)
    f = np.concatenate([hourly, hourly * wk])
    return f / math.sqrt(float(np.mean(f[b] ** 2)))


# ── the model ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RiskModel:
    step_s: int
    resid: np.ndarray        # (n, 3): standardised moves to close, low, high
    omega: float
    alpha: float
    beta: float
    var_next: float          # expected (deseasonalised) variance of the step after last_t
    var_long: float          # long-run deseasonalised variance per step
    season: np.ndarray       # (48,) volatility multiplier by hour of day / weekend
    last_t: int              # open time of the last candle in the fit, ms
    last_close: float
    n: int
    kind: str                # "fhs" (from the coin's candles) | "rough" (not enough history)
    params: np.ndarray | None = None   # (k, 3): plausible (α, β, weight) — each path draws its own

    @property
    def span_days(self) -> float:
        return self.n * self.step_s / 86_400

    def nowcast(self, recent: Candles | None, mid: float | None, now_ms: int) -> float:
        """The variance to start from now: run the GARCH through the candles completed since the fit,
        then through the move so far in the candle still forming (its expected part scaled by how much
        of the candle has passed, plus the actual shock)."""
        if not (self.alpha or self.beta):
            return self.var_next
        step_ms = self.step_s * 1000
        v, ref, t_next = self.var_next, self.last_close, self.last_t + step_ms
        if recent is not None:
            for t, c in zip(recent.t.tolist(), recent.close.tolist()):
                if t < t_next:
                    continue
                if t != t_next:
                    break                               # a gap in what's in memory: stop at the last known candle
                r = math.log(c / ref) / self.season[season_bucket(t)]
                v = min(self.omega + self.alpha * r * r + self.beta * v, VAR_CAP * self.var_long)
                ref, t_next = c, t + step_ms
        if mid and ref and now_ms > t_next:
            # the move since the last known close. Usually that's the candle still forming; if candles are
            # missing (a gap in memory) the move is spread evenly over the candles it took, rather than
            # counted as one candle's shock — a 2-hour move is not a 5-minute crash.
            k = (now_ms - t_next) / step_ms
            r2 = (math.log(mid / ref) / self.season[season_bucket(t_next)]) ** 2
            if k <= 1:
                v = v + k * (self.omega + (self.beta - 1) * v) + self.alpha * r2
            else:
                whole = min(int(k), MAX_NOWCAST_STEPS)
                for _ in range(whole):
                    v = self.omega + self.alpha * r2 / k + self.beta * v
                frac = k - int(k)
                v = v + frac * (self.omega + (self.beta - 1) * v) + self.alpha * r2 * frac / k
        return float(min(max(v, 1e-12), VAR_CAP * self.var_long))

    def vol_ratio(self, v0: float, now_ms: int) -> float:
        """Volatility expected for the next step vs this coin's long-run average."""
        f = float(self.season[season_bucket(now_ms)])
        return math.sqrt(v0 / self.var_long) * f if self.var_long > 0 else 1.0


def _nll_grid(r2: np.ndarray, var_long: float, alphas: np.ndarray, betas: np.ndarray) -> np.ndarray:
    """GARCH(1,1) quasi-likelihood for many (α, β) at once (variance targeting)."""
    omega = var_long * (1 - alphas - betas)
    v = np.full(len(alphas), var_long)
    nll = np.zeros(len(alphas))
    for x in r2.tolist():
        nll += np.log(v) + x / v
        v = omega + alphas * x + betas * v
    return nll


def _grid(alphas, persistences) -> tuple[np.ndarray, np.ndarray]:
    pairs = [(a, p - a) for a in alphas for p in persistences if p - a > 0.0 and 0 < p < 1]
    return np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])


def fit_garch(r2: np.ndarray, var_long: float) -> tuple[float, float, np.ndarray]:
    """Best (α, β), plus the plausible alternatives: a fine grid around the best point weighted by how well
    each explains the data (likelihood ∝ exp(−nll/2)). Persistence is only known to about ±0.005 from a few
    thousand candles, and tail odds over a week are very sensitive to it, so the simulation draws each
    path's parameters from these weights instead of trusting one point estimate."""
    a, b = _grid(COARSE_ALPHA, COARSE_PERSISTENCE)
    i = int(np.argmin(_nll_grid(r2, var_long, a, b)))
    a0, p0 = float(a[i]), float(a[i] + b[i])
    fa = np.clip(a0 + np.arange(-6, 7) * 0.005, 0.002, 0.4)
    fp = np.clip(p0 + np.arange(-8, 9) * 0.002, 0.3, 0.999)
    a, b = _grid(sorted(set(fa.tolist())), sorted(set(fp.tolist())))
    nll = _nll_grid(r2, var_long, a, b)
    i = int(np.argmin(nll))
    w = np.exp(-(nll - nll[i]) / 2)
    keep = w > 1e-4
    params = np.column_stack([a[keep], b[keep], w[keep] / w[keep].sum()])
    return float(a[i]), float(b[i]), params


def _garch_path(r2: np.ndarray, var_long: float, a: float, b: float) -> np.ndarray:
    """Conditional variance before each step, plus the forecast for the step after the last."""
    omega = var_long * (1 - a - b)
    var = np.empty(len(r2) + 1)
    v = var_long
    for i, x in enumerate(r2.tolist()):
        var[i] = v
        v = omega + a * x + b * v
    var[-1] = v
    return var


def fit_model(c: Candles, season: np.ndarray | None = None) -> RiskModel | None:
    """`season`: time-of-day factors measured elsewhere — the 5-minute model borrows them from the hourly
    one, because 17 days of 5-minute candles pin the daily cycle down only to ±20%, 200 days of hourly
    candles to a few percent (and the cycle is the same whatever the candle size)."""
    if len(c) < MIN_CANDLES + 1:
        return None
    ref = c.close[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(c.close[1:] / ref)
        lo = np.log(c.low[1:] / ref)
        hi = np.log(c.high[1:] / ref)
    t = c.t[1:]
    ok = np.isfinite(r) & np.isfinite(lo) & np.isfinite(hi)
    r, lo, hi, t = r[ok], lo[ok], hi[ok], t[ok]
    if len(r) < MIN_CANDLES:
        return None
    mu = float(r.mean())                                   # no directional view: remove the history's drift
    r = r - mu
    lo = np.minimum(np.minimum(lo - mu, 0.0), r)           # a candle's low is at or below where it started and ended
    hi = np.maximum(np.maximum(hi - mu, 0.0), r)
    # take the time of day out first (a GARCH fitted to raw moves would chase the daily cycle instead)
    if season is None:
        season = seasonal_factors(t, r)
    else:                                                  # renormalise over this sample's hours
        season = season / math.sqrt(float(np.mean(season[season_bucket(t)] ** 2)))
    f = season[season_bucket(t)]
    r, lo, hi = r / f, lo / f, hi / f
    r2 = r * r
    var_long = float(r2.mean())
    a, b, params = fit_garch(r2, var_long)
    omega = var_long * (1 - a - b)
    var = _garch_path(r2, var_long, a, b)
    sd = np.sqrt(var[:-1])
    z = _centred(np.column_stack([r / sd, lo / sd, hi / sd]))
    return RiskModel(c.step_s, z, omega, a, b, float(var[-1]), var_long, season, int(c.t[-1]), float(c.close[-1]),
                     len(r), "fhs", params)


def _centred(z: np.ndarray) -> np.ndarray:
    """Residuals with exactly zero mean and unit variance. Even a tiny average in the pool compounds over
    hundreds of steps into a drift — a view on direction the planner must not have."""
    mean = float(z[:, 0].mean())
    close = z[:, 0] - mean
    low = np.minimum(np.minimum(z[:, 1] - mean, 0.0), close)
    high = np.maximum(np.maximum(z[:, 2] - mean, 0.0), close)
    scale = float(np.std(close)) or 1.0
    return np.column_stack([close, low, high]) / scale


def rough_model(sigma_1s_bps: float, step_s: int, now_ms: int = 0, seed: int = 7, n: int = 20_000,
                dof: float = 4.0) -> RiskModel:
    """Not enough candles: a fat-tailed (Student-t, 4 d.o.f.) random walk at the volatility seen in the live
    feed, with each step's low and high drawn from the Brownian bridge between its start and end."""
    rng = np.random.default_rng(seed)
    t = rng.standard_t(dof, n) * math.sqrt((dof - 2) / dof)
    u1, u2 = rng.random(n), rng.random(n)
    lo = (t - np.sqrt(t * t - 2 * np.log(u1))) / 2         # exact bridge extremes for unit variance
    hi = (t + np.sqrt(t * t - 2 * np.log(u2))) / 2
    var = (sigma_1s_bps / 10_000) ** 2 * step_s
    return RiskModel(step_s, _centred(np.column_stack([t, lo, hi])), var, 0.0, 0.0, var, var, np.ones(48),
                     now_ms, 0.0, 0, "rough")


@dataclass(frozen=True, slots=True)
class Paths:
    final: np.ndarray        # log move from entry to exit
    low: np.ndarray          # lowest log move on the way (≤ 0)
    high: np.ndarray         # highest log move on the way (≥ 0)
    best_alive: np.ndarray | None   # best move in the position's favour before it would have been liquidated
    steps: int
    step_s: int
    exit_kind: np.ndarray | None = None   # with a bracket: 0 still open at the end, 1 target, 2 stop, 3 liquidated
    exit_step: np.ndarray | None = None   # the candle it closed in (steps if it ran to the end)


def simulate(m: RiskModel, hours: float, v0: float | None = None, start_ms: int = 0, n_paths: int = N_PATHS,
             seed: int = 11, liq: tuple[int, float] | None = None,
             bracket: tuple[float | None, float | None] | None = None) -> Paths:
    """Simulate the hold. `liq` = (side, log distance to liquidation) also tracks, per path, the best
    price reached while the position was still open. `bracket` = (stop, target) log distances (needs `liq`'s
    side; either may be None) also records which closes the trade first and when. If one candle reaches both
    the stop (or liquidation) and the target, the loss is taken as first — the cautious reading of a wick.
    The random draws are the same with or without a bracket, so its odds match the rest of the plan."""
    steps = max(1, math.ceil(hours * 3600 / m.step_s))
    n_paths = int(min(n_paths, max(2_000, MAX_PATH_STEPS // steps)))
    rng = np.random.default_rng(seed)
    cum = np.zeros(n_paths)
    low = np.zeros(n_paths)
    high = np.zeros(n_paths)
    var = np.full(n_paths, m.var_next if v0 is None else v0)
    cap = VAR_CAP * m.var_long
    zc, zl, zh = m.resid[:, 0], m.resid[:, 1], m.resid[:, 2]
    if m.params is not None and len(m.params) > 1:
        g = rng.choice(len(m.params), n_paths, p=m.params[:, 2])
        alpha, beta = m.params[g, 0], m.params[g, 1]
        omega = m.var_long * (1 - alpha - beta)
    else:
        alpha, beta, omega = m.alpha, m.beta, m.omega
    step_ms = m.step_s * 1000
    season = m.season[season_bucket(start_ms + np.arange(steps, dtype=np.int64) * step_ms)]
    track = liq is not None
    if track:
        side, dist = liq
        best = np.zeros(n_paths)
        dead = np.zeros(n_paths, bool)
    if bracket is not None:
        if not track:
            raise ValueError("a bracket needs the side: pass liq=(side, distance or inf)")
        stop, target = bracket
        if stop is not None and stop >= dist:
            stop = None                                  # liquidation comes first: the stop never fires
        kind = np.zeros(n_paths, np.int8)
        exit_step = np.full(n_paths, steps, np.int32)
    for i in range(steps):
        k = rng.integers(0, len(zc), n_paths)
        sd = np.sqrt(var)
        s = sd * season[i]
        step_low = cum + zl[k] * s
        step_high = cum + zh[k] * s
        if track:
            adv = -step_low if side > 0 else step_high
            fav = step_high if side > 0 else -step_low
            hit = adv >= dist
            alive = ~dead & ~hit                        # liquidated in this candle: its best price came too late
            np.maximum(best, np.where(alive, fav, best), out=best)
            dead |= hit
            if bracket is not None:
                open_ = kind == 0
                stopped = open_ & (adv >= stop) if stop is not None else np.zeros(n_paths, bool)
                liqd = open_ & hit & ~stopped
                won = open_ & (fav >= target) & ~stopped & ~hit if target is not None else np.zeros(n_paths, bool)
                kind[stopped], kind[liqd], kind[won] = 2, 3, 1
                exit_step[stopped | liqd | won] = i + 1
        np.minimum(low, step_low, out=low)
        np.maximum(high, step_high, out=high)
        shock = zc[k] * sd                               # deseasonalised move drives the GARCH
        cum += zc[k] * s
        if m.alpha or m.beta:
            var = np.minimum(omega + alpha * shock * shock + beta * var, cap)
    return Paths(cum, low, high, best if track else None, steps, m.step_s,
                 kind if bracket is not None else None, exit_step if bracket is not None else None)


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


def liq_distance(entry: float, side: int, leverage: float, maint: float) -> float | None:
    """Log distance from entry to liquidation, positive; None if it can't be liquidated."""
    liq = liquidation_price(entry, side, leverage, maint)
    if liq is None:
        return None
    return math.log(entry / liq) if side > 0 else math.log(liq / entry)


def _adverse(paths: Paths, side: int) -> np.ndarray:
    """How far each path went against the position at its worst, as a positive log move."""
    return -paths.low if side > 0 else paths.high


def _favourable(paths: Paths, side: int) -> np.ndarray:
    return paths.high if side > 0 else -paths.low


def liq_probability(paths: Paths, side: int, entry: float, leverage: float, maint: float,
                    adverse: np.ndarray | None = None) -> float:
    dist = liq_distance(entry, side, leverage, maint)
    if dist is None:
        return 0.0
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
    prob: float              # chance price trades there before you exit (for "for": while still in the trade)
    kind: str                # "against" | "for" | "liq"


@dataclass(frozen=True, slots=True)
class Drawdown:
    move_pct: float          # signed price move against you at that point
    pnl: float
    liquidated: bool         # this point is at or past liquidation


@dataclass(frozen=True, slots=True)
class Bracket:
    """A stop and a take-profit: which closes the trade first, and what that's worth after costs."""
    stop_pct: float | None           # distance from entry, % (positive)
    stop_price: float | None
    target_pct: float | None
    target_price: float | None
    stop_beyond_liq: bool            # the stop sits past liquidation, so it can never fire
    p_target: float                  # target reached first
    p_stop: float                    # stop reached first
    p_liq: float                     # liquidated first (only without a working stop)
    p_time: float                    # neither: closed at the end of the hold
    pnl_target: float | None         # P&L when the target fills, after costs
    pnl_stop: float | None
    ev: float                        # average P&L over all paths, after costs
    p_profit: float
    breakeven_win: float | None      # share of target-vs-stop outcomes you need to win to break even
    hours_target: float | None       # median time to the target when it's hit
    hours_stop: float | None


def bracket_distances(side: int, stop_pct: float | None, target_pct: float | None) -> tuple[float | None, float | None]:
    """Percent distances from entry → positive log distances (a long's stop is below, a short's above)."""
    def d(pct, toward):
        if pct is None:
            return None
        move = toward * pct / 100
        return abs(math.log1p(move)) if move > -1 else None
    return d(stop_pct, -side), d(target_pct, side)


def _bracket(paths: Paths, side: int, entry: float, notional: float, stop_pct, target_pct, liq_dist,
             fees: float, entry_slip: float, exit_slip: float, funding: float, liquidated_pnl: float) -> Bracket:
    stop_d, target_d = bracket_distances(side, stop_pct, target_pct)
    beyond = stop_d is not None and liq_dist is not None and stop_d >= liq_dist
    kind, step = paths.exit_kind, paths.exit_step
    held = step / paths.steps                                       # share of the hold spent in the trade
    costs = fees + entry_slip + exit_slip + funding * held          # funding only for the time held
    pnl_t = _pnl(notional, side, side * target_d) if target_d is not None else 0.0
    pnl_s = _pnl(notional, side, -side * stop_d) if stop_d is not None and not beyond else 0.0
    pnl = np.select([kind == 1, kind == 2, kind == 3],
                    [pnl_t - costs, pnl_s - costs, np.full(len(kind), liquidated_pnl)],
                    _pnl(notional, side, paths.final) - costs)
    full = fees + entry_slip + exit_slip
    win = float(pnl_t - full - funding * 0.5) if target_d is not None else None     # funding: about half the hold
    loss = float(pnl_s - full - funding * 0.5) if stop_d is not None and not beyond else None
    be = (-loss / (win - loss)) if win is not None and loss is not None and win > 0 > loss else None
    hours = lambda mask: float(np.median(step[mask]) * paths.step_s / 3600) if mask.any() else None
    price = lambda d, toward: None if d is None else entry * math.exp(toward * d)
    return Bracket(
        stop_pct=stop_pct, stop_price=price(stop_d, -side), target_pct=target_pct, target_price=price(target_d, side),
        stop_beyond_liq=beyond,
        p_target=float(np.mean(kind == 1)), p_stop=float(np.mean(kind == 2)), p_liq=float(np.mean(kind == 3)),
        p_time=float(np.mean(kind == 0)), pnl_target=win, pnl_stop=loss, ev=float(pnl.mean()),
        p_profit=float(np.mean(pnl > 0)), breakeven_win=be,
        hours_target=hours(kind == 1), hours_stop=hours(kind == 2))


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
    drawdown: tuple[Drawdown, Drawdown]  # the median and 1-in-4 worst point before exit
    touches: tuple[Touch, ...]
    outcome: dict[str, float]            # P&L percentiles at exit after costs: p5 … p95
    prob_profit: float
    costs: Costs
    breakeven_pct: float                 # move your way needed to cover costs (negative: funding pays you)
    model: RiskModel
    vol_ratio: float                     # volatility expected now vs the coin's long-run average
    paths_n: int
    notes: tuple[str, ...] = field(default_factory=tuple)
    bracket: Bracket | None = None       # with a stop and/or target


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
              exit_slip_bps: float | None, funding_now: float | None, funding_avg: float | None,
              vol_ratio: float = 1.0, book_seen: bool = True, stop_pct: float | None = None,
              target_pct: float | None = None) -> Plan:
    known = max_leverage is not None
    max_lev = float(max_leverage or DEFAULT_MAX_LEVERAGE)
    maint = maintenance_rate(max_lev)
    notional = margin * leverage
    adverse = _adverse(paths, side)
    favourable = paths.best_alive if paths.best_alive is not None else _favourable(paths, side)

    liq = liquidation_price(entry, side, leverage, maint)
    liq_dist = liq_distance(entry, side, leverage, maint)
    liq_hit = adverse >= liq_dist if liq_dist is not None else np.zeros(len(adverse), bool)
    sigma = float(np.std(paths.final))

    # costs
    entry_fee = TAKER_FEE * notional
    fees = 2 * entry_fee
    slip_known = entry_slip_bps is not None and exit_slip_bps is not None
    entry_slip = (entry_slip_bps or 0) / 10_000 * notional
    slippage = entry_slip + (exit_slip_bps or 0) / 10_000 * notional
    funding = side * expected_funding_rate(hours, funding_now, funding_avg) * notional
    to_apr = lambda f: None if f is None else f * 24 * 365 * 100
    costs = Costs(fees, slippage, slip_known, funding, to_apr(funding_now), to_apr(funding_avg))

    # outcome at exit: liquidated paths lose the margin plus what entering cost (and any funding paid);
    # the rest pay all costs
    liquidated_pnl = -margin - entry_fee - entry_slip - max(funding, 0.0)
    pnl = np.where(liq_hit, liquidated_pnl, _pnl(notional, side, paths.final) - costs.total)
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
        touches.append(Touch((liq / entry - 1) * 100, liq, liquidated_pnl, float(liq_hit.mean()), "liq"))

    # the worst point before exit, stopping at liquidation (after it, there's no position)
    capped = np.minimum(adverse, liq_dist) if liq_dist is not None else adverse

    def drawdown(a: float) -> Drawdown:
        dead = liq_dist is not None and a >= liq_dist - 1e-12
        move = float(np.expm1(-a) * 100) if side > 0 else float(np.expm1(a) * 100)
        return Drawdown(move, liquidated_pnl if dead else float(_pnl(notional, side, -a if side > 0 else a)), dead)

    lo5, hi95 = np.percentile(paths.final, [5, 95])
    dd50, dd75 = np.percentile(capped, [50, 75])
    curve_levs = sorted({x for x in CURVE_LEVERAGES if x <= max_lev} | {float(leverage), max_lev})
    curve = tuple((lv, liq_probability(paths, side, entry, lv, maint, adverse)) for lv in curve_levs)

    notes = []
    if not known:
        notes.append(f"Hyperliquid's max leverage for {coin} hasn't loaded (it needs live mode), so this assumes "
                     f"{max_lev:.0f}× max and {maint * 100:.1f}% maintenance margin — the cautious case for "
                     f"majors, but check it on Hyperliquid.")
    if model.kind == "rough":
        notes.append("Rough estimate: not enough price history yet (live mode downloads it), so this uses a "
                     "fat-tailed random walk at the volatility seen in the feed.")
    if not book_seen:
        notes.append("No order book yet, so slippage isn't included.")
    elif not slip_known:
        notes.append("Your size goes beyond the visible order book, so slippage isn't included.")
    if funding_now is None and funding_avg is None:
        notes.append("No funding rate yet, so funding isn't included.")
    elif liq_dist is not None and funding > 0.02 * margin:
        notes.append(f"Funding of about ${funding:,.0f} comes out of your margin over this hold, which moves "
                     f"liquidation a little closer than shown.")

    bracket = None
    if (stop_pct is not None or target_pct is not None) and paths.exit_kind is not None:
        bracket = _bracket(paths, side, entry, notional, stop_pct, target_pct, liq_dist, fees, entry_slip,
                           slippage - entry_slip, funding, liquidated_pnl)
        if bracket.stop_beyond_liq:
            notes.append("Your stop is past the liquidation price, so liquidation would close the trade first.")

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
        drawdown=(drawdown(float(dd50)), drawdown(float(dd75))),
        touches=tuple(touches),
        outcome=outcome,
        prob_profit=float(np.mean(pnl > 0)),
        costs=costs,
        breakeven_pct=costs.total / notional * 100 if notional else 0.0,
        model=model,
        vol_ratio=vol_ratio,
        paths_n=len(paths.final),
        notes=tuple(notes),
        bracket=bracket,
    )

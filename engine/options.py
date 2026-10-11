"""
Options-implied odds for price prediction markets ("ETH above 2,600 at 08:00 Friday").

Deribit lists BTC, ETH and SOL options at many strikes and expiries. Their implied volatilities form a surface:
how big a move the options market expects by each date, and how that changes with the strike (the "smile":
crashes and squeezes usually cost more than a plain bell curve would say). A prediction market "above K at T"
is a digital option, so its fair price comes straight off that surface:

    P(S_T > K) = N(d2) - F * phi(d1) * sqrt(T) * dsigma/dK

The first term is the plain lognormal answer at the strike's own volatility; the second is the smile
correction (with a steep put skew, "above" lines a little over the price are worth more than N(d2) says).

Strikes are measured against Hyperliquid's own price: the forward for date T is Hyperliquid's spot times
Deribit's carry to T (its forward for T over its nearest forward), so a small gap between the two venues
doesn't leak into the odds.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
NAME = re.compile(r"^([A-Z]+)(?:_USDC)?-(\d{1,2})([A-Z]{3})(\d{2})-([\dd.]+)-([CP])$")
YEAR_S = 365.0 * 86_400
MIN_TAU_S = 60.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def parse_instrument(name: str) -> tuple[str, int, float, str] | None:
    """"BTC-27DEC26-100000-C" → ("BTC", expiry ms at 08:00 UTC, 100000.0, "C"); "XRP_USDC-27DEC26-0d5-C" → strike 0.5."""
    m = NAME.match(name or "")
    if not m:
        return None
    asset, day, mon, yy, strike, cp = m.groups()
    if mon not in MONTHS:
        return None
    try:
        expiry = datetime(2000 + int(yy), MONTHS[mon], int(day), 8, tzinfo=timezone.utc)
        k = float(strike.replace("d", "."))
    except ValueError:
        return None
    return asset, int(expiry.timestamp() * 1000), k, cp


@dataclass(slots=True)
class Smile:
    expiry_ms: int
    forward: float                                   # Deribit's forward price for this expiry
    points: list[tuple[float, float]] = field(default_factory=list)   # (log-moneyness ln(K/F), implied vol)

    def vol(self, k: float) -> float:
        """Implied vol at log-moneyness k: straight lines between strikes, flat past the last ones."""
        pts = self.points
        if k <= pts[0][0]:
            return pts[0][1]
        if k >= pts[-1][0]:
            return pts[-1][1]
        lo, hi = 0, len(pts) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if pts[mid][0] <= k:
                lo = mid
            else:
                hi = mid
        (k0, v0), (k1, v1) = pts[lo], pts[hi]
        return v0 + (v1 - v0) * (k - k0) / (k1 - k0)


@dataclass(slots=True)
class Surface:
    asset: str
    smiles: list[Smile]                              # by expiry, nearest first
    fetched_ms: int = 0

    def forward_ratio(self, t_ms: int) -> float:
        """Deribit's carry from now to t: its forward at t over its nearest forward."""
        s = self.smiles
        near = s[0].forward
        if t_ms <= s[0].expiry_ms:
            return 1.0
        if t_ms >= s[-1].expiry_ms:
            return s[-1].forward / near
        for a, b in zip(s, s[1:]):
            if a.expiry_ms <= t_ms <= b.expiry_ms:
                w = (t_ms - a.expiry_ms) / (b.expiry_ms - a.expiry_ms)
                return (a.forward + w * (b.forward - a.forward)) / near
        return 1.0

    def vol(self, k: float, t_ms: int, now_ms: int) -> float:
        """Implied vol at log-moneyness k for date t: total variance (vol² x time) interpolated in time between the
        expiries around t, which is how desks keep a surface free of calendar arbitrage."""
        s = self.smiles
        tau = max(MIN_TAU_S, (t_ms - now_ms) / 1000) / YEAR_S
        if t_ms <= s[0].expiry_ms:
            return s[0].vol(k)
        if t_ms >= s[-1].expiry_ms:
            return s[-1].vol(k)
        for a, b in zip(s, s[1:]):
            if a.expiry_ms <= t_ms <= b.expiry_ms:
                ta = max(MIN_TAU_S, (a.expiry_ms - now_ms) / 1000) / YEAR_S
                tb = max(MIN_TAU_S, (b.expiry_ms - now_ms) / 1000) / YEAR_S
                wa, wb = a.vol(k) ** 2 * ta, b.vol(k) ** 2 * tb
                w = wa + (wb - wa) * (tau - ta) / (tb - ta) if tb > ta else wb
                return math.sqrt(max(w, 1e-12) / tau)
        return s[-1].vol(k)


def build_surface(asset: str, rows: list[dict], now_ms: int, min_points: int = 4) -> Surface | None:
    """Deribit get_book_summary_by_currency rows (kind=option) → one asset's surface. Calls and puts at a strike
    share Deribit's mark vol; expiries already past, or with too few strikes to form a smile, are dropped."""
    by_exp: dict[int, dict] = {}
    for r in rows:
        p = parse_instrument(str(r.get("instrument_name", "")))
        if not p or p[0] != asset or p[1] <= now_ms:
            continue
        iv, fwd = r.get("mark_iv"), r.get("underlying_price")
        try:
            iv, fwd = float(iv) / 100, float(fwd)
        except (TypeError, ValueError):
            continue
        if not (0.01 < iv < 5) or fwd <= 0:
            continue
        e = by_exp.setdefault(p[1], {"fwd": [], "iv": {}})
        e["fwd"].append(fwd)
        e["iv"].setdefault(p[2], []).append(iv)
    smiles = []
    for exp, e in sorted(by_exp.items()):
        fwd = sorted(e["fwd"])[len(e["fwd"]) // 2]
        pts = sorted((math.log(k / fwd), sum(v) / len(v)) for k, v in e["iv"].items())
        if len(pts) >= min_points:
            smiles.append(Smile(exp, fwd, pts))
    return Surface(asset, smiles, now_ms) if smiles else None


def digital_above(surface: Surface, spot: float, line: float, t_ms: int, now_ms: int) -> float | None:
    """The options market's chance that the price is above `line` at t, measured from Hyperliquid's spot."""
    if not surface or not surface.smiles or not spot or not line or spot <= 0 or line <= 0:
        return None
    if t_ms <= now_ms:
        return 1.0 if spot >= line else 0.0
    tau = max(MIN_TAU_S, (t_ms - now_ms) / 1000) / YEAR_S
    fwd = spot * surface.forward_ratio(t_ms)
    k = math.log(line / fwd)
    sig = surface.vol(k, t_ms, now_ms)
    sd = sig * math.sqrt(tau)
    d1 = (-k + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    h = 0.005                                                  # the smile's slope at the strike, per unit of price
    dsig_dk = (surface.vol(math.log(line * (1 + h) / fwd), t_ms, now_ms)
               - surface.vol(math.log(line * (1 - h) / fwd), t_ms, now_ms)) / (2 * h * line)
    p = norm_cdf(d2) - fwd * norm_pdf(d1) * math.sqrt(tau) * dsig_dk
    return min(1.0, max(0.0, p))


def touch_prob(surface: Surface, spot: float, line: float, t_ms: int, now_ms: int) -> float | None:
    """The options market's chance the price touches `line` before t: about twice the chance of ending beyond it
    (the reflection rule), at the strike's own implied vol."""
    if not surface or not spot or not line or t_ms <= now_ms:
        return None
    end_beyond = digital_above(surface, spot, line, t_ms, now_ms)
    if end_beyond is None:
        return None
    return min(1.0, 2 * (end_beyond if line >= spot else 1 - end_beyond))


def implied_vol_at(surface: Surface, spot: float, line: float, t_ms: int, now_ms: int) -> float | None:
    if not surface or not spot or not line:
        return None
    fwd = spot * surface.forward_ratio(t_ms)
    return surface.vol(math.log(line / fwd), t_ms, now_ms)


def distribution(lines: list[float], above: list[float | None]) -> list[dict]:
    """A ladder of "above K" chances → the chance of ending in each band between lines. Bands come out negative
    when the ladder is out of order (a higher line priced above a lower one); they're kept, so that shows."""
    if not lines or any(p is None for p in above):
        return []
    pts = [1.0] + list(above) + [0.0]
    edges = [None] + list(lines) + [None]
    return [{"lo": edges[i], "hi": edges[i + 1], "p": pts[i] - pts[i + 1]} for i in range(len(lines) + 1)]

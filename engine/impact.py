"""
Flow efficiency: did price move as far as the order flow says it should have?

Order-flow share ("76% sell") says who was aggressive, not whether it worked.
The trader's question is what that aggression *did*. So we measure the coin's
normal price impact: basis points of move per $1M of net taker flow (buys
minus sells), and compare each window against it:

    expected move = λ(T) × net flow over the window
    impact ratio  = actual move ÷ expected move

    < 0      price went the other way: the aggressors were absorbed
    0–0.35   far less than usual: largely absorbed by passive orders
    0.35–2.5 about normal
    > 2.5    far more than the flow explains: a thin book, or a move led from
             elsewhere (most price discovery happens across several venues)

λ(T) is fitted per timeframe from live minute bars (Kyle's lambda, OLS through
the origin over rolling T-long stretches), because impact is not linear in
time: part of it fades, so 60 minutes of flow moves price less than 60 × one
minute's worth. Each timeframe waits for its own fit: 20 minutes of live bars
for 1m, about 40 for 10m, about 4 hours for 60m. Saved bars carry over across
restarts, so this is a one-off warm-up.

A ratio is only reported when the flow was big enough to matter: when it
"should" have moved price by at least 0.75 of a normal move for the window.
Below that, noise swamps the ratio.
"""
import math
from dataclasses import dataclass, field

from models.barModel import Bar
from models.explanationModel import FlowImpact

MIN_SAMPLES_1M = 20         # one-minute samples before λ(1m) is trusted
MIN_SAMPLES = 30            # rolling samples before a longer horizon's own λ is trusted
MIN_EXPECTED_SIGMA = 0.75   # flow must "explain" ≥ 0.75σ of the window before a ratio means anything
ABSORBED_BELOW = 0.35
OUTSIZED_ABOVE = 2.5


@dataclass(frozen=True, slots=True)
class ImpactModel:
    lam: dict[int, float] = field(default_factory=dict)       # horizon seconds → bps per $1M net flow
    samples: dict[int, int] = field(default_factory=dict)

    def lookup(self, seconds: int) -> tuple[float, str] | None:
        return (self.lam[seconds], "measured") if seconds in self.lam else None


def _runs(bars: list[Bar]) -> list[list[Bar]]:
    """Live one-minute bars split into gap-free runs (a gap = the app was off or the feed dropped)."""
    runs: list[list[Bar]] = []
    for b in bars:
        if not b.has_flow or b.span_s != 60 or b.open <= 0 or b.close <= 0:
            continue
        if runs and b.timestamp - runs[-1][-1].timestamp == 60_000:
            runs[-1].append(b)
        else:
            runs.append([b])
    return runs


def fit_impact(bars: list[Bar], horizons_s: list[int]) -> ImpactModel:
    runs = _runs(bars)
    lam: dict[int, float] = {}
    samples: dict[int, int] = {}
    for seconds in sorted(set(horizons_s)):
        k = max(1, seconds // 60)
        step = max(1, k // 10)
        sxy = sxx = 0.0
        n = 0
        for run in runs:
            if len(run) < k:
                continue
            prefix = [0.0]
            for b in run:
                prefix.append(prefix[-1] + (b.buy_notional - b.sell_notional) / 1e6)
            for i in range(0, len(run) - k + 1, step):
                f = prefix[i + k] - prefix[i]
                r = math.log(run[i + k - 1].close / run[i].open) * 10_000
                sxy += r * f
                sxx += f * f
                n += 1
        need = MIN_SAMPLES_1M if k == 1 else MIN_SAMPLES
        if n >= need and sxx > 0 and sxy > 0:
            lam[seconds] = sxy / sxx
            samples[seconds] = n
    return ImpactModel(lam, samples)


def assess(model: ImpactModel | None, seconds: int, buy_usd: float, sell_usd: float, move_bps: float,
           normal_move_bps: float, flow_coverage: float = 1.0) -> FlowImpact | None:
    """Compare the window's actual move with what its net flow normally does. None when it can't be judged."""
    if model is None or flow_coverage < 0.9:
        return None
    found = model.lookup(seconds)
    if found is None:
        return None
    lam, source = found
    net = buy_usd - sell_usd
    expected = lam * net / 1e6
    if abs(expected) < MIN_EXPECTED_SIGMA * normal_move_bps or expected == 0:
        return None
    ratio = move_bps / expected
    verdict = ("against" if ratio < 0 else "absorbed" if ratio < ABSORBED_BELOW
               else "outsized" if ratio > OUTSIZED_ABOVE else "normal")
    return FlowImpact(net_flow=net, expected_bps=expected, actual_bps=move_bps, ratio=ratio,
                      verdict=verdict, lam_bps_per_m=lam, source=source)

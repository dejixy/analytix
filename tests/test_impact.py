import random

from engine.explainer import explain
from engine.impact import MIN_SAMPLES_1M, ImpactModel, assess, fit_impact
from models.barModel import Bar
from signals import price_move
from signals.volumeImbalance import volume_imbalance
from tests.helpers import T0, make_slice, summary, trade

MIN = 60_000
T = T0 // MIN * MIN


def _bars(flows_musd, lam=5.0, start=T, price=3000.0):
    """Minute bars whose move is exactly λ × net flow, chained close-to-open."""
    out = []
    for i, f in enumerate(flows_musd):
        close = price * (1 + lam * f / 10_000)
        buy, sell = (f * 1e6, 0.0) if f >= 0 else (0.0, -f * 1e6)
        out.append(Bar(start + i * MIN, 60, price, max(price, close), min(price, close), close,
                       buy_notional=buy + 1e6, sell_notional=sell + 1e6))
        price = close
    return out


def test_lambda_is_recovered_per_horizon():
    rng = random.Random(3)
    flows = [rng.uniform(-1, 1) for _ in range(80)]          # varied net flow, −1..+1 $M per minute
    m = fit_impact(_bars(flows), [60, 600])
    assert abs(m.lam[60] - 5.0) < 0.05 and abs(m.lam[600] - 5.0) < 0.05
    assert m.samples[60] == 80 and m.samples[600] == 71


def test_gaps_break_the_rolling_windows_and_short_history_is_not_trusted():
    flows = [((i * 7) % 5 - 2) * 0.5 for i in range(MIN_SAMPLES_1M - 1)]
    assert 60 not in fit_impact(_bars(flows), [60]).lam
    a, b = _bars([1.0] * 15), _bars([1.0] * 15, start=T + 60 * MIN)   # two runs an hour apart
    m = fit_impact(a + b, [60, 600])
    assert m.samples[60] == 30 and 600 not in m.samples            # 10m needs 30 gap-free stretches


def test_each_timeframe_waits_for_its_own_fit():
    m = ImpactModel({60: 4.0}, {60: 50})
    assert m.lookup(60) == (4.0, "measured")
    assert m.lookup(600) is None                     # impact fades with time: 1m's λ would overstate 10m


def test_verdicts():
    m = ImpactModel({600: 4.0}, {600: 40})
    # $5M of net selling normally moves -20 bps over 10m; normal 10m move is ±16 bps
    sell = dict(buy_usd=1e6, sell_usd=6e6, normal_move_bps=16.0)
    assert assess(m, 600, move_bps=+3, **sell).verdict == "against"
    assert assess(m, 600, move_bps=-4, **sell).verdict == "absorbed"
    assert assess(m, 600, move_bps=-22, **sell).verdict == "normal"
    assert assess(m, 600, move_bps=-60, **sell).verdict == "outsized"
    # too little net flow to judge, or flow only seen for part of the window
    assert assess(m, 600, 1e6, 2e6, -4, 16.0) is None
    assert assess(m, 600, move_bps=-4, flow_coverage=0.5, **sell) is None


def test_absorption_is_called_out_from_measured_impact():
    # 65/35 selling (below the old 0.5-strength bar for absorption) that moved price up
    trades = [trade(T0 + i * 1000, sz=30 if i % 3 else 16, side="A" if i % 3 else "B") for i in range(60)]
    sl = make_slice(trades, summary(T0, 3000.0), summary(T0 + 60_000, 3000.3))
    sl = sl.__class__(**{**{f: getattr(sl, f) for f in sl.__dataclass_fields__}, "high": 3000.4, "low": 2999.9})
    flow = volume_imbalance(sl)
    move = price_move(sl)
    im = assess(ImpactModel({60: 6.0}, {60: 40}), 60, flow.metrics["buy_notional"], flow.metrics["sell_notional"],
                move.move_bps, move.expected_bps)
    assert im and im.verdict == "against"
    ex = explain("ETH", sl, move, [flow], im)
    assert "absorbed it" in ex.headline and ex.impact is im
    assert any(line.startswith("Impact: net selling of") and "The sellers were absorbed" in line for line in ex.narrative)

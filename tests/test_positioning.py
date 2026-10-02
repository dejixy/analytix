from dataclasses import replace

from engine.positioning import fit_positioning, percentile
from models.barModel import Bar
from signals.funding import funding
from tests.helpers import T0, ctx, make_slice, summary

MIN = 60_000
T = T0 // MIN * MIN


def _bars(n, oi_step=0.001, funding_of=lambda i: 0.00001):
    """Live minute bars whose OI grows by oi_step (0.1%) a minute, plus hourly funding readings."""
    oi, out = 100_000.0, []
    for i in range(n):
        oi *= 1 + oi_step * (1 + (i % 3))           # 0.1–0.3% a minute
        out.append(Bar(T + i * MIN, 60, 3000, 3000, 3000, 3000, buy_notional=1, sell_notional=1,
                       open_interest=oi, funding=funding_of(i)))
    return out


def test_percentiles_need_enough_history():
    m = fit_positioning(_bars(20), [60, 600])
    assert m.funding_history() == () and m.oi_history(600) == ()     # under a day of funding, < 30 samples
    assert percentile((), 1.0) is None


def test_funding_is_ranked_against_the_past_week_and_oi_moves_against_the_same_timeframe():
    bars = _bars(26 * 60, funding_of=lambda i: 0.00001 * (i // 60))     # funding rises every hour
    m = fit_positioning(bars, [60, 600])
    assert len(m.funding_history()) in (26, 27)                   # one reading per clock hour
    assert percentile(m.funding_history(), 0.00025) > 0.95
    hist = m.oi_history(60)
    assert 0.09 < hist[0] < 0.11 and 0.29 < hist[-1] < 0.31          # 1m OI moves are 0.1–0.3%


def test_signal_flags_an_unusual_oi_move_on_the_card_and_ranks_funding():
    m = fit_positioning(_bars(26 * 60, funding_of=lambda i: 0.00001 * (i // 60)), [60])
    sl = make_slice(book_start=summary(T0, 3000), book_end=summary(T0 + 60_000, 3006),
                    ctx_start=ctx(T0, oi=100_000), ctx_end=ctx(T0 + 60_000, oi=100_500, funding=0.00025))
    sl = replace(sl, funding_history=m.funding_history(), oi_history=m.oi_history(60))
    sig = funding(sl)                                                 # +0.5% OI in 1m: beyond anything on record
    assert sig.stat == "OI +0.50% · top 1%" and sig.metrics["oi_pct"] == 1.0
    assert "the biggest 1m OI move on record" in sig.summary
    assert "percentile of the past week" in sig.summary

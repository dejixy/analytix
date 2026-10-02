from engine.cascades import CascadeTracker, oi_sentence, recovery_text
from models.signalModel import Direction
from signals.liquidations import liquidations
from state import MarketState
from tests.helpers import BASELINE, T0, book, ctx, make_slice, trade

S = 1000


def _run(oi_after: float, recover_to: float, seconds: int = 120):
    """Three $60K sell sweeps at t=10–12s push ETH 3000 → 2990; OI then reads oi_after; from t=30s price sits at recover_to."""
    st, tr = MarketState("ETH"), CascadeTracker("ETH")
    for s in range(seconds):
        ts = T0 + s * S
        mid = 3000.0 if s <= 10 else 2995.0 if s == 11 else 2990.0 if s < 30 else recover_to
        st.apply(book(ts, mid=mid))
        st.apply(ctx(ts, oi=100_000.0 if s < 13 else oi_after))
        if 10 <= s <= 12:
            st.apply_many([trade(ts + 100, px=mid - 1, sz=20, side="A", h=f"x{s}")])
        tr.update(st, sweep_threshold=50_000, now_ms=ts + 500)
    return st, tr


def test_oi_falling_with_the_cascade_reads_as_likely_liquidations():
    st, tr = _run(oi_after=99_950.0, recover_to=2995.0)       # OI −50 coins ≈ −$150K vs $180K swept
    (info,) = tr.infos(st.now_ms)
    assert info.verdict == "likely" and abs(info.confirm_share - 150_000 / (3 * 20 * 2999)) < 0.01
    assert abs(info.move_bps - (2990 / 3000 - 1) * 10_000) < 0.01
    assert abs(info.recovered - 0.5) < 1e-6
    (ev,) = tr.events
    assert ev.kind == "cascade" and ev.direction is Direction.DOWN
    assert ev.title.startswith("3 sell sweeps in 2s ($180K) pushed price -0.33%")
    assert ev.detail.startswith("OI −$150K: likely liquidations · won back 50% in")


def test_flat_oi_reads_as_one_trader_and_the_signal_says_so():
    st, tr = _run(oi_after=100_000.0, recover_to=2985.0)
    (info,) = tr.infos(st.now_ms)
    assert info.verdict == "unlikely" and info.recovered < 0
    assert oi_sentence(info) == "Open interest barely moved — more likely one large trader than liquidations."
    assert recovery_text(info) == "Since then price has kept going past the cascade's low."
    sl = make_slice(list(st.trades), baseline=BASELINE, seconds=60)
    sl = sl.__class__(**{**{f: getattr(sl, f) for f in sl.__dataclass_fields__}, "cascades": (info,)})
    sig = liquidations(sl)
    assert sig.phrase.startswith("a sell-sweep cascade (3 sweeps") and "OI didn't fall" in sig.phrase
    assert "more likely one large trader" in sig.summary


def test_no_event_until_the_cascade_is_over_and_oi_is_pending_at_first():
    st, tr = _run(oi_after=99_950.0, recover_to=2995.0, seconds=15)   # 2–3s after the last sweep
    assert tr.events == []
    (info,) = tr.infos(st.now_ms)
    assert info.verdict is None and oi_sentence(info) == "Checking open interest…"

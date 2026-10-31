"""The profit harness (engine/harness.py): fills only at prices someone really traded, limit orders only when price
trades through them, fees by order type, one trade per coin at a time."""
import numpy as np
import pytest

from engine.harness import MAKER_BPS, TAKER_BPS, Market, Signal, one_at_a_time, summary, trade

T = 1_790_000_000_000


def market(buys, sells, quotes):
    """buys/sells: [(ms after T, price)] taker prints; quotes: [(ms, bid, ask)]."""
    m = Market.__new__(Market)
    m.coin = "ETH"
    arr = lambda rows, k: np.array([r[k] for r in rows], dtype=float if k else np.int64)
    m.buy_t, m.buy_px = arr([(T + a, p) for a, p in buys], 0), arr(buys, 1)
    m.sell_t, m.sell_px = arr([(T + a, p) for a, p in sells], 0), arr(sells, 1)
    m.q_t = np.array([T + q[0] for q in quotes], dtype=np.int64)
    m.q_bid, m.q_ask = arr(quotes, 1), arr(quotes, 2)
    return m


def test_market_orders_fill_at_the_next_print_on_their_side():
    m = market(buys=[(100, 100.1), (2_000, 100.3)], sells=[(1_000, 99.9), (70_000, 100.4)], quotes=[(0, 100.0, 100.1)])
    r = trade(m, Signal("ETH", T, 1, hold_ms=60_000), "market")
    assert r["gross"] == pytest.approx((100.4 / 100.3 - 1) * 1e4)       # in at 2.0 s (the 0.1 s buy was too early)
    assert r["fees"] == 2 * TAKER_BPS and r["net"] == pytest.approx(r["gross"] - 9)


def test_a_limit_buy_fills_only_when_a_sell_trades_through_it():
    q = [(0, 100.0, 100.1)]
    touch = market(buys=[(31_000, 100.2)], sells=[(5_000, 100.0), (45_000, 100.3)], quotes=q)
    assert trade(touch, Signal("ETH", T, 1, hold_ms=60_000), "limit in or skip, market out") == {"missed": True}
    through = market(buys=[], sells=[(5_000, 99.9), (66_000, 100.4)], quotes=q)
    r = trade(through, Signal("ETH", T, 1, hold_ms=60_000), "limit in or skip, market out")
    assert r["maker_in"] and r["fees"] == MAKER_BPS + TAKER_BPS
    assert r["gross"] == pytest.approx((100.4 / 100.0 - 1) * 1e4)       # bought at our bid, 100.0
    chase = trade(touch, Signal("ETH", T, 1, hold_ms=10_000), "limit in or chase, market out")
    assert not chase["maker_in"] and chase["fees"] == 2 * TAKER_BPS     # unfilled after 30 s: bought at the ask
    assert chase["gross"] == pytest.approx((100.3 / 100.2 - 1) * 1e4)


def test_the_freshest_quote_wins():
    m = market(buys=[(3_000, 100.6)], sells=[(2_000, 100.5)], quotes=[(0, 100.0, 100.1)])
    assert m.quote(T + 1_000) == (100.0, 100.1)
    assert m.quote(T + 4_000) == (100.5, 100.6)                         # prints after the book are newer


def test_one_trade_per_coin_at_a_time_and_summary():
    s = [Signal("ETH", T, 1, hold_ms=60_000), Signal("ETH", T + 30_000, 1, hold_ms=60_000),
         Signal("SOL", T + 30_000, -1, hold_ms=60_000), Signal("ETH", T + 61_000, 1, hold_ms=60_000)]
    kept = one_at_a_time(s)
    assert [(k.coin, k.t_ms - T) for k in kept] == [("ETH", 0), ("SOL", 30_000), ("ETH", 61_000)]
    rows = [{"missed": False, "t_ms": T + d * 86_400_000, "gross": 10.0, "net": n, "fees": 3.0} for d, n in
            enumerate([2.0, 4.0, 3.0, 5.0])] + [{"missed": True}, None]
    out = summary(rows, days=4, notional=1000)
    assert out["trades"] == 4 and out["fill"] == pytest.approx(0.8) and out["net"] == pytest.approx(3.5)
    assert out["usd_day"] == pytest.approx(14 * 1000 / 1e4 / 4) and out["t"] > 2


def test_regime_ideas_trade_with_or_against_the_last_minutes_at_each_half_hour():
    from engine.volforecast import Forecast
    from scripts.regimeHarness import ideas
    m0 = 1_790_000_000_000 // 60_000 // 30 * 30            # a :00 or :30 minute
    price = np.full(120, 100.0)
    price[25:30] = 101.0                                    # up into the half hour that ends minute 29
    price[57:60] = 99.0                                     # down into the next
    fc = {"ETH": Forecast("ETH", m0, np.zeros(120), (1.0, 2.0), {}, price, np.ones(120, bool))}
    out = ideas(fc)
    t30, t60 = (m0 + 30) * 60_000, (m0 + 60) * 60_000
    assert [(s.t_ms, s.side) for s in out["trend 5m"]] == [(t30, 1), (t60, -1)]
    assert [(s.t_ms, s.side) for s in out["revert 15m"]] == [(t30, -1), (t60, 1)]
    assert all(s.hold_ms == 30 * 60_000 for s in out["trend 15m"])

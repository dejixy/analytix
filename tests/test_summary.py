import math
import random
import statistics
from dataclasses import replace

from engine.summary import (
    LAYOUTS, LiquidityTracker, SummaryContext, efficiency, flow_row, funding_row, layout_for, liquidity_row,
    nice_size, positioning_row, slippage_bps, trend_row, vwap_row, who_row,
)
from models.bookModel import BookLevel, OrderBook
from models.explanationModel import FlowImpact, PriceMove, Significance
from models.orderModel import AggressiveOrder
from models.signalModel import Direction, SignalResult
from models.tradeModel import TradeSide
from signals.base import FlowAgg
from tests.helpers import T0, make_slice

S = 1000


def _move(bps=0.0, z=0.0, expected=10.0, high=3001.0, low=2999.0, start=3000.0):
    return PriceMove(start, start * (1 + bps / 1e4), high, low, bps, z, expected,
                     Significance.QUIET if abs(z) < 1.5 else Significance.SIGNIFICANT, Direction.of(bps, eps=0.5))


def _order(ts, side, usd, taker, twap=False):
    return AggressiveOrder(ts, side, usd, usd / 3000, 1, 1, 3000.0, 3000.0, taker=taker, engine=twap)


# ── layouts ────────────────────────────────────────────────────────────────
def test_each_timeframe_gets_rows_for_its_horizon():
    assert layout_for(60) == LAYOUTS["execution"] == ("flow", "who", "forced", "book", "liquidity")
    assert layout_for(600) == LAYOUTS["scalp"]
    assert layout_for(3600) == LAYOUTS["session"]
    assert layout_for(86400) == layout_for(604800) == LAYOUTS["swing"]


# ── trend efficiency ───────────────────────────────────────────────────────
def test_efficiency_is_calibrated_to_a_random_walk_and_a_straight_line():
    rng = random.Random(4)
    ers = []
    for _ in range(300):
        x, path = 0.0, []
        for i in range(600):
            x += rng.gauss(0, 1)
            path.append((i * S, x))
        ers.append(efficiency(tuple(path), 0, 599 * S))
    assert abs(statistics.mean(ers) - 0.183) < 0.02            # 1/√30
    line = tuple((i * S, 100 + i * 0.01) for i in range(600))
    assert abs(efficiency(line, 0, 599 * S) - 1.0) < 1e-9


def test_efficiency_only_measures_the_part_of_the_window_with_data():
    zigzag = tuple((T0 + i * S, 100 + (0.5 if (i // 7) % 2 else 0)) for i in range(300))   # 5 min of chop
    er_full = efficiency(zigzag, T0 - 3300 * S, T0 + 299 * S)                             # in a 60m window
    assert er_full is not None and er_full < 0.2                  # not inflated by the 55 empty minutes


def test_trend_tags_need_both_efficiency_and_a_real_move():
    line = tuple((T0 + i * S, 3000 + i * 0.05) for i in range(600))
    s = replace(make_slice(seconds=600), path=line)
    up = trend_row(s, _move(bps=100, z=3.0, high=3030, low=3000))
    assert up.tag == "uptrend" and up.lean is Direction.UP and up.value.startswith("efficiency 1.00 · 100% of range")
    weak = trend_row(s, _move(bps=3, z=0.5, high=3030, low=3000))
    assert weak.tag == "two-way"                                    # efficient but tiny: not a trend
    zig = tuple((T0 + i * S, 3001.5 + 1.5 * math.sin(i / 4.3)) for i in range(600))   # swings, goes nowhere
    # chop: going nowhere while swinging more than a normal range for recent volatility (10 bps normal move)
    chop = trend_row(replace(make_slice(seconds=600), path=zig), _move(high=3006, low=3000))
    assert chop.tag == "chop"
    assert trend_row(replace(make_slice(seconds=600), path=zig), _move(high=3001, low=3000)).tag == "quiet range"


# ── VWAP ───────────────────────────────────────────────────────────────────
def test_vwap_distance_is_scored_in_random_walk_units():
    s = replace(make_slice(seconds=600), path=((T0, 3000.0), (T0 + 600 * S, 3003.0)), vwap=3000.0)
    # +10 bps from VWAP; normal move 10 bps → 1 sd ≈ 5.8 bps → +1.7 sd: above value, not stretched
    m = vwap_row(s, _move(expected=10.0))
    assert m.value == "+0.10% vs 3,000.00" and m.tag == "above value" and m.lean is Direction.UP
    far = vwap_row(replace(s, vwap=2995.0), _move(expected=10.0))
    assert far.tag == "stretched"
    near = vwap_row(replace(s, vwap=3002.5), _move(expected=10.0))
    assert near.tag == "at value" and near.lean is Direction.NEUTRAL


# ── who ────────────────────────────────────────────────────────────────────
def test_who_spots_one_twap_working_the_selling():
    orders = [_order(T0 + i * 30 * S, TradeSide.SELL, 60_000, "0xtwap", twap=True) for i in range(10)]
    orders += [_order(T0 + i * 7 * S + 3, TradeSide.SELL, 5_000, f"0xs{i}") for i in range(40)]
    orders += [_order(T0 + i * 9 * S + 5, TradeSide.BUY, 4_000, f"0xb{i}") for i in range(30)]
    m = who_row(replace(make_slice(seconds=600), orders=sorted(orders, key=lambda o: o.timestamp),
                        twaps={("0xtwap", TradeSide.SELL): (T0, 60_000.0)}))
    assert m.value == "1 TWAP = 75% of selling" and m.tag == "TWAP" and m.lean is Direction.DOWN
    assert "about every 30s" in m.detail and "0xtwap" in m.detail and "TWAP fills: 75%" in m.detail


def test_who_reads_broad_flow_and_a_whale_quietly_buying_into_it():
    sells = [_order(T0 + i * S, TradeSide.SELL, 10_000, f"0xs{i}") for i in range(60)]
    buys = [_order(T0 + i * S, TradeSide.BUY, 10_000, f"0xb{i % 15}") for i in range(20)]
    broad = who_row(replace(make_slice(seconds=600), orders=sells + buys))
    assert broad.tag == "broad" and broad.value == "60 sellers · 15 buyers"
    whale = [_order(T0 + i * 20 * S, TradeSide.BUY, 50_000, "0xwhale") for i in range(6)]
    m = who_row(replace(make_slice(seconds=600), orders=sells + whale))
    assert m.value == "1 wallet = 100% of buying" and m.tag == "whale"


def test_who_needs_enough_trades_and_wallets():
    few = [_order(T0, TradeSide.BUY, 1_000, "0xa")] * 3
    assert who_row(replace(make_slice(seconds=60), orders=few)).tag == "quiet"
    anon = [_order(T0 + i, TradeSide.BUY, 1_000, None) for i in range(20)]
    assert who_row(replace(make_slice(seconds=60), orders=anon)).value == "wallets not in this data"


# ── flow ───────────────────────────────────────────────────────────────────
def _flow(buy, sell, strength=0.8, activity=1.0):
    d = Direction.UP if buy > sell else Direction.DOWN
    return SignalResult("volume_imbalance", "Order flow", (buy - sell) / (buy + sell), strength, d, "", "", "",
                        {"buy_notional": buy, "sell_notional": sell, "activity": activity})


def test_flow_row_reports_net_dollars_and_what_the_flow_did():
    im = FlowImpact(-5e6, -20.0, 3.0, -0.15, "against", 4.0, "measured")
    m = flow_row(make_slice(seconds=600), {"volume_imbalance": _flow(1e6, 6e6)}, im)
    assert m.value == "86% sell · net −$5.00M" and m.tag == "against flow" and m.lean is Direction.DOWN
    assert "normally moves price -0.20%; it moved +0.03%" in m.detail
    assert flow_row(make_slice(), {"volume_imbalance": _flow(5e6, 1e6, activity=2.5)}, None).tag == "heavy"


def test_long_window_flow_says_how_much_of_the_window_it_covers():
    s = replace(make_slice(seconds=86400), resolution="bar", flow_coverage=0.1,
                flow=FlowAgg(1e6, 3e6, 100, 8640.0, 0.1))
    m = flow_row(s, {"volume_imbalance": _flow(1e6, 3e6)}, None)
    assert m.partial and m.tag == "partial" and m.value == "75% sell · last 2.4 h only"


# ── positioning and funding ────────────────────────────────────────────────
def _funding(phrase, strength=0.6, oi=1.2, apr=11.0, hourly=0.0000125, pct=None, oi_pct=None):
    return SignalResult("funding", "Funding & OI", 0.6, strength, Direction.UP, "summary", phrase, "",
                        {"oi_change_pct": oi, "open_interest_usd": 1.0e9, "funding_apr": apr,
                         "funding_hourly": hourly, "funding_pct": pct, "oi_pct": oi_pct, "premium_bps": 1.0})


def test_positioning_names_who_is_moving_and_flags_unusual_oi():
    m = positioning_row(make_slice(seconds=3600), {"funding": _funding("new longs opening (OI +1.20%)", oi_pct=0.97)})
    assert m.tag == "longs in" and m.lean is Direction.UP
    assert m.value == "OI +1.20% (+$11.86M) · top 3%"
    m = positioning_row(make_slice(seconds=3600), {"funding": _funding("crowded longs being flushed (OI -2.00%)", oi=-2.0)})
    assert m.tag == "long flush" and m.lean is Direction.DOWN


def test_funding_row_says_what_holding_costs_and_whether_a_side_is_crowded():
    day = replace(make_slice(seconds=86400), label="24h")
    m = funding_row(day, {"funding": _funding("x", pct=0.95, apr=40.0, hourly=0.0000457)})
    assert m.value == "+40.0% APR · 0.110% per 24h" and m.tag == "crowded longs" and m.kind == "position"
    assert m.detail.startswith("Longs pay: holding a position for 24h costs 0.110%")
    calm = funding_row(day, {"funding": _funding("x", apr=11.0)})
    assert calm.tag == "normal" and calm.value == "+11.0% APR · 0.030% per 24h"


# ── liquidity ──────────────────────────────────────────────────────────────
def _book(depth=10.0):
    bids = tuple(BookLevel(round(2999.95 - i * 0.1, 2), depth, 2) for i in range(20))
    asks = tuple(BookLevel(round(3000.05 + i * 0.1, 2), depth, 2) for i in range(20))
    return OrderBook(T0, bids, asks)


def test_slippage_walks_the_book():
    book = _book(depth=10.0)                          # $30K a level
    # $100K buy: 3 full levels + a third of the 4th → average ≈ 3000.165 → ≈0.55 bps over mid
    assert abs(slippage_bps(book.asks, 100_000, 3000.0) - 0.55) < 0.02
    assert slippage_bps(book.asks, 10_000_000, 3000.0) is None          # more than the visible book holds
    assert nice_size(132_000) == 100e3 and nice_size(190_000) == 250e3


def test_liquidity_row_compares_with_the_usual_cost():
    tr = LiquidityTracker()
    for i in range(20):
        tr.update(_book(depth=10.0), T0 + i * 5 * S, sweep_threshold=100_000)
    usual = tr.usual()
    ctx = SummaryContext(True, _book(depth=2.0), 1.0, tr.size, usual)             # the book has thinned 5×
    m = liquidity_row(ctx)
    assert m.tag == "thin" and m.value.startswith("$100K:")
    assert liquidity_row(SummaryContext(False, None, 1.0, 100e3, usual)).value == "live only"
    tr.update(_book(), T0 + 200 * S, sweep_threshold=600_000)                   # a new yardstick restarts it
    assert tr.size == 500e3 and tr.usual() is None


def test_flow_absorbed_is_only_for_flow_that_barely_moved_price():
    im = FlowImpact(-5e6, -20.0, -3.0, 0.15, "absorbed", 4.0, "measured")
    assert flow_row(make_slice(seconds=600), {"volume_imbalance": _flow(1e6, 6e6)}, im).tag == "absorbed"


def test_funding_at_the_floor_is_not_crowded():
    from engine.positioning import percentile
    week = tuple(sorted([0.0000125] * 140 + [0.000005] * 28))      # a quiet week: mostly exactly at the floor
    pct = percentile(week, 0.0000125)
    assert 0.55 < pct < 0.65                                        # mid-rank, not "100th percentile"
    day = replace(make_slice(seconds=86400), label="24h")
    m = funding_row(day, {"funding": _funding("x", pct=0.97, apr=10.95)})
    assert m.tag == "normal"                                        # top of the week, but only at the floor rate

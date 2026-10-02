from engine.analyzer import Analyzer
from ingestion.backfill import candles_to_bars
from models.barModel import Bar
from state import MarketState
from storage.barStore import BarStore
from tests.helpers import T0, book, trade

MIN = 60_000
T = T0 // MIN * MIN          # align to a minute boundary


def test_state_rolls_minute_bars_with_flow_and_price():
    st = MarketState("ETH")
    st.apply(book(T + 1_000, mid=3000.0))
    st.apply_many([trade(T + 2_000, px=3000.05, sz=10, side="B"), trade(T + 3_000, px=2999.95, sz=4, side="A")])
    st.apply(book(T + 30_000, mid=3003.0))
    st.apply(book(T + MIN + 1_000, mid=3001.0))          # next minute → first bar closes
    st.apply(book(T + 2 * MIN + 1_000, mid=3002.0))      # and the second
    assert len(st.bars) == 2
    b = st.bars.oldest
    assert b.timestamp == T and b.span_s == 60 and b.source == "live"
    assert (b.open, b.high, b.close) == (3000.0, 3003.0, 3003.0)
    assert abs(b.buy_notional - 30000.5) < 1 and abs(b.sell_notional - 11999.8) < 1
    assert b.bid_notional is not None


def test_closed_bars_are_handed_to_the_persistence_hook():
    saved = []
    st = MarketState("ETH", on_bar=lambda coin, bar: saved.append((coin, bar.timestamp)))
    st.apply(book(T + 1_000))
    st.apply(book(T + MIN + 1_000))
    assert saved == [("ETH", T)]
    st.reset()
    assert st.on_bar is not None           # the hook survives a replay reset


def test_backfill_merges_history_without_overwriting_live_bars():
    st = MarketState("ETH")
    st.apply(book(T + 1_000, mid=3000.0))
    st.apply(book(T + MIN + 1_000, mid=3001.0))             # one live bar at T
    candles = [Bar(T - 10 * MIN, 300, 2990, 2995, 2985, 2992, source="candle"),
               Bar(T - 5 * MIN, 300, 2992, 2999, 2990, 2998, source="candle"),
               Bar(T, 300, 2998, 3004, 2997, 3001, source="candle")]   # overlaps the live bar → skipped
    assert st.merge_bars(candles) == 2
    assert [b.source for b in st.bars] == ["candle", "candle", "live"]


def test_candles_to_bars_parses_hyperliquid_format_and_attaches_funding():
    raw = [{"t": T, "T": T + 299_999, "s": "ETH", "i": "5m", "o": "3000.0", "c": "3010.5", "h": "3012.0",
            "l": "2998.0", "v": "120.5", "n": 900}]
    funding = [{"coin": "ETH", "fundingRate": "0.0000125", "premium": "0.0", "time": T - 1_000}]
    (bar,) = candles_to_bars(raw, funding)
    assert bar.span_s == 300 and bar.close == 3010.5 and bar.source == "candle"
    assert bar.funding == 0.0000125 and abs(bar.volume_usd - 120.5 * 3010.5) < 1e-6
    assert not bar.has_flow


def test_bar_store_round_trip(tmp_path):
    store = BarStore(tmp_path / "bars.sqlite")
    bar = Bar(T, 60, 1, 2, 0.5, 1.5, buy_notional=10, sell_notional=5, bid_notional=None, open_interest=7.0)
    store.save("ETH", bar)
    store.save("BTC", bar)
    assert store.load("ETH", T - 1) == [bar]
    store.prune(T + 1)
    assert store.load("ETH", 0) == []
    store.close()


def test_six_hour_window_reads_bars_and_price_from_before_the_window():
    st = MarketState("ETH")
    hist = [Bar(T - (400 - i) * MIN, 60, 3000 + i * 0.1, 3000 + i * 0.1, 3000 + i * 0.1, 3000 + i * 0.1,
                source="candle") for i in range(400)]
    st.apply(book(T + 1_000, mid=3050.0))
    st.merge_bars(hist)
    an = Analyzer(st)
    an.run(st.now_ms)
    ex = an.latest["6h"]
    start_close = hist[400 - 361].close        # the bar that closed just before the 6h window began
    assert abs(ex.move.start_price - start_close) < 0.2
    assert ex.move.end_price == 3050.0
    assert ex.flow_coverage == 0.0             # candles carry no order flow

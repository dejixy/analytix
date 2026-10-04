from ingestion.parsers import parse_message
from models.bookModel import OrderBook
from models.contextModel import AssetContext
from models.tradeModel import Trade, TradeSide
from state import MarketState

# Shapes copied from Hyperliquid's WebSocket docs (numbers arrive as strings).
TRADES = {"channel": "trades", "data": [
    {"coin": "ETH", "side": "B", "px": "3012.5", "sz": "1.25", "hash": "0xabc", "time": 1790000000123,
     "tid": 111, "users": ["0xBuyer", "0xSeller"]},
    {"coin": "ETH", "side": "B", "px": "3012.6", "sz": "0.75", "hash": "0xabc", "time": 1790000000123,
     "tid": 112, "users": ["0xBuyer", "0xOther"]},
]}
BOOK = {"channel": "l2Book", "data": {"coin": "ETH", "time": 1790000000200, "levels": [
    [{"px": "3012.4", "sz": "10.0", "n": 3}, {"px": "3012.3", "sz": "5.5", "n": 2}],
    [{"px": "3012.5", "sz": "8.0", "n": 4}, {"px": "3012.6", "sz": "2.0", "n": 1}],
]}}
CTX = {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {
    "funding": "0.0000125", "openInterest": "612345.5", "prevDayPx": "2950.0", "dayNtlVlm": "1843250000.0",
    "premium": "0.0001", "oraclePx": "3011.0", "markPx": "3012.4", "midPx": "3012.45",
    "impactPxs": ["3012.3", "3012.6"]}}}


def test_parse_trades():
    events = parse_message(TRADES, "ETH")
    assert len(events) == 2 and all(isinstance(e, Trade) for e in events)
    t = events[0]
    assert t.side is TradeSide.BUY and t.price == 3012.5 and t.size == 1.25
    assert t.hash == "0xabc"
    assert t.buyer is None          # both wallets only when liquidator matching is on…
    assert t.taker == "0xbuyer"     # …but the aggressor's is always kept ("B": the buyer took the ask)


def test_wallets_kept_when_liquidators_configured(monkeypatch):
    import ingestion.parsers as parsers
    monkeypatch.setattr(parsers, "LIQUIDATOR_ADDRESSES", {"0xother"})
    t = parse_message(TRADES, "ETH")[0]
    assert t.buyer == "0xbuyer" and t.seller == "0xseller"


def test_parse_book():
    (b,) = parse_message(BOOK, "ETH")
    assert isinstance(b, OrderBook)
    assert b.best_bid == 3012.4 and b.best_ask == 3012.5
    assert abs(b.mid - 3012.45) < 1e-9


def test_parse_ctx_leaves_timestamp_for_state():
    (c,) = parse_message(CTX, "ETH")
    assert isinstance(c, AssetContext) and c.timestamp == 0
    assert abs(c.funding_apr - 10.95) < 0.01


def test_other_coins_and_channels_ignored():
    assert parse_message({"channel": "pong"}, "ETH") == []
    assert parse_message({"channel": "subscriptionResponse", "data": {}}, "ETH") == []
    assert parse_message({**BOOK, "data": {**BOOK["data"], "coin": "BTC"}}, "ETH") == []


def test_state_stamps_context_with_exchange_clock_and_groups_orders():
    st = MarketState("ETH")
    for msg in (TRADES, BOOK, CTX):
        st.apply_many(parse_message(msg, "ETH"))
    assert st.now_ms == 1790000000200
    assert st.context.timestamp == st.now_ms
    assert len(st.orders) == 1                       # two fills, one taker order
    order = st.orders.newest
    assert order.fills == 2 and order.levels == 2


def test_duplicate_trades_after_reconnect_are_dropped():
    st = MarketState("ETH")
    st.apply_many(parse_message(TRADES, "ETH"))
    st.apply_many(parse_message(TRADES, "ETH"))      # resent snapshot
    assert len(st.trades) == 2 and st.dropped == 2


def test_taker_is_the_seller_on_an_aggressive_sell_and_engine_fills_are_not_merged():
    from models.orderModel import group_orders
    zero = "0x" + "0" * 64
    raw = {"channel": "trades", "data": [
        {"coin": "ETH", "side": "A", "px": "3000.0", "sz": "2", "hash": zero, "time": 1, "tid": 1, "users": ["0xMM", "0xTwapOne"]},
        {"coin": "ETH", "side": "A", "px": "2999.9", "sz": "1", "hash": zero, "time": 1, "tid": 2, "users": ["0xMM2", "0xTwapOne"]},
        {"coin": "ETH", "side": "A", "px": "3000.0", "sz": "3", "hash": zero, "time": 1, "tid": 3, "users": ["0xMM", "0xTwapTwo"]},
    ]}
    trades = parse_message(raw, "ETH")
    assert [t.taker for t in trades] == ["0xtwapone", "0xtwapone", "0xtwaptwo"] and all(t.zero_hash for t in trades)
    orders = sorted(group_orders(trades), key=lambda o: o.taker)
    assert [(o.taker, o.size, o.engine) for o in orders] == [("0xtwapone", 3.0, True), ("0xtwaptwo", 3.0, True)]

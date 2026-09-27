from models.bookModel import BookLevel, BookSummary, OrderBook
from models.contextModel import AssetContext
from models.orderModel import group_orders
from models.tradeModel import Trade, TradeSide
from signals.base import Baseline, WindowSlice

T0 = 1_790_000_000_000  # an arbitrary exchange timestamp (ms)

BASELINE = Baseline(sigma_1s_bps=1.0, notional_per_s=10_000.0, sweep_threshold=50_000.0, covered_s=900.0)


def trade(ts: int, px: float = 3000.0, sz: float = 1.0, side: str = "B", tid: int | None = None,
          h: str | None = None) -> Trade:
    return Trade(timestamp=ts, price=px, size=sz, side=TradeSide(side),
                 tid=tid if tid is not None else ts, hash=h if h is not None else f"h{ts}{side}{px}")


def summary(ts: int, mid: float = 3000.0, bid: float = 1e6, ask: float = 1e6) -> BookSummary:
    return BookSummary(timestamp=ts, mid=mid, best_bid=mid - 0.05, best_ask=mid + 0.05,
                       spread_bps=0.33, bid_notional=bid, ask_notional=ask)


def book(ts: int, mid: float = 3000.0, levels: int = 5, size: float = 10.0) -> OrderBook:
    bids = tuple(BookLevel(round(mid - 0.05 - i * 0.1, 2), size, 3) for i in range(levels))
    asks = tuple(BookLevel(round(mid + 0.05 + i * 0.1, 2), size, 3) for i in range(levels))
    return OrderBook(timestamp=ts, bids=bids, asks=asks)


def ctx(ts: int, oi: float = 100_000.0, funding: float = 0.0000125, mark: float = 3000.0) -> AssetContext:
    return AssetContext(timestamp=ts, funding=funding, open_interest=oi, mark_price=mark, oracle_price=mark)


def make_slice(trades=(), book_start=None, book_end=None, ctx_start=None, ctx_end=None,
               seconds: int = 60, coverage: float = 1.0, baseline: Baseline = BASELINE) -> WindowSlice:
    trades = list(trades)
    mids = [b.mid for b in (book_start, book_end) if b]
    return WindowSlice(
        label=f"{seconds // 60}m", seconds=seconds, start_ms=T0, end_ms=T0 + seconds * 1000,
        trades=trades, orders=group_orders(trades),
        book_start=book_start, book_end=book_end, ctx_start=ctx_start, ctx_end=ctx_end,
        high=max(mids) if mids else None, low=min(mids) if mids else None,
        coverage=coverage, baseline=baseline,
    )

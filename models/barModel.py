from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bar:
    """
    A compressed slice of market history — the storage tier for long windows.

    Live bars are built from the feed every minute and carry everything: price,
    who was aggressive, sweeps, book depth, open interest. Backfilled bars come
    from Hyperliquid's candle endpoint and carry price and total volume only —
    the public API has no history of trades by side, depth or OI.
    """
    timestamp: int                   # bar open, ms (exchange clock)
    span_s: int                      # 60 for live bars, 300 for backfilled 5m candles
    open: float
    high: float
    low: float
    close: float
    source: str = "live"             # "live" | "candle"
    volume_usd: float = 0.0          # total traded notional
    buy_notional: float = 0.0        # taker buys (live bars only)
    sell_notional: float = 0.0
    fills: int = 0
    sweeps_buy: int = 0
    sweeps_sell: int = 0
    sweep_notional_buy: float = 0.0
    sweep_notional_sell: float = 0.0
    bid_notional: float | None = None   # near-touch depth at the bar's close
    ask_notional: float | None = None
    open_interest: float | None = None
    funding: float | None = None     # hourly rate at the bar's close
    mark: float | None = None

    @property
    def end_ms(self) -> int:
        return self.timestamp + self.span_s * 1000

    @property
    def has_flow(self) -> bool:
        return self.source == "live"


class BarAccumulator:
    """The bar currently being built. Mutable on purpose — it becomes a frozen Bar when the minute closes."""

    __slots__ = ("start", "open", "high", "low", "close", "buy", "sell", "fills", "sw_b", "sw_s",
                 "swn_b", "swn_s", "bid", "ask", "oi", "funding", "mark")

    def __init__(self, start: int, price: float):
        self.start = start
        self.open = self.high = self.low = self.close = price
        self.buy = self.sell = 0.0
        self.fills = 0
        self.sw_b = self.sw_s = 0
        self.swn_b = self.swn_s = 0.0
        self.bid = self.ask = self.oi = self.funding = self.mark = None

    def price(self, p: float) -> None:
        self.close = p
        if p > self.high:
            self.high = p
        if p < self.low:
            self.low = p

    def freeze(self, span_s: int) -> Bar:
        return Bar(
            timestamp=self.start, span_s=span_s, open=self.open, high=self.high, low=self.low,
            close=self.close, source="live", volume_usd=self.buy + self.sell,
            buy_notional=self.buy, sell_notional=self.sell, fills=self.fills,
            sweeps_buy=self.sw_b, sweeps_sell=self.sw_s,
            sweep_notional_buy=self.swn_b, sweep_notional_sell=self.swn_s,
            bid_notional=self.bid, ask_notional=self.ask,
            open_interest=self.oi, funding=self.funding, mark=self.mark,
        )

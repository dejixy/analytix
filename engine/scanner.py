"""
The scanner: every Hyperliquid perp, ranked by how unusual it is right now.

Every coin is judged against its own normal, never against BTC's:

    price       the move over 15 minutes and 1 hour, in multiples of a normal move for that coin
                (from its last 7 days of hourly candles)
    volume      the last 24 hours vs its average day; for the most traded coins also the last 15 minutes
                vs a normal 15 minutes
    positions   open interest change over the last hour, vs how much it usually changes in an hour
    funding     how much one side is paying, per year
    vs index    how far the perp trades from the spot index

For the 30 most traded coins (and any coin with a dashboard open) the scanner also reads the trades:
which side is pushing, big orders, runs of big orders (cascades), liquidations and TWAP bots.

Each reading is scaled so that 1.0 means "worth a look"; a coin's score is its largest reading, and
the readings over 1.0 become the plain-words reasons shown next to it.
"""
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from models.orderModel import is_sweep
from models.tradeModel import TradeSide
from signals.base import fmt_usd
from signals.liquidations import find_cascades

TRADE_WINDOW_S = 900                 # the trade reads cover the last 15 minutes
HISTORY_S = 2 * 3600 + 120           # price and open interest samples kept per coin
OI_SAMPLE_MS = 5 * 60_000            # slower samples for "how much does open interest usually move in an hour"
OI_SAMPLES = 2 * 24 * 12             # two days of them
MIN_OI_SPAN_H = 6                    # hours of samples before the coin's own typical change is used
DEFAULT_OI_1H_PCT = 1.5              # until then: a typical hour moves open interest about this much
BIG_ORDER_FLOOR = 10_000             # USD: no order smaller than this counts as big, on any coin

# what counts as "worth a look" (score 1.0) for each reading
MOVE_Z = 2.0                         # 2× a normal move
DAY_VOLUME_X = 2.0                   # 2× its average day
FAST_VOLUME_X = 3.0                  # the last 15 minutes at 3× a normal 15 minutes
OI_X = 2.5                           # 2.5× a normal hour's open interest change
FUNDING_APR = 40.0                   # % a year, either side
PREMIUM_PCT = 0.15                   # % from the index
LIQ_SHARE = 0.05                     # liquidations in 15m worth 5% of a normal hour's volume
TWAP_SHARE = 0.10                    # a TWAP worth 10% of a normal hour's volume, per hour
CASCADE_SHARE = 0.05
ONE_SIDED = 0.70                     # 70%+ of 15 minutes of market orders on one side...
ONE_SIDED_VOLUME_X = 1.5             # ...while trading at least 1.5× normal
THIN_VOLUME = 500_000                # USD a day: below this a coin can look unusual from noise alone


@dataclass(frozen=True, slots=True)
class Baseline:
    """A coin's normal, from its last 7 days of hourly candles."""
    sigma_1h: float                  # a normal hourly move, as a log return
    vol_hour: float                  # median hourly volume, USD
    vol_day: float                   # average daily volume, USD
    fetched_ms: int


def baseline_from_candles(rows: list[dict], now_ms: int) -> Baseline | None:
    """Hyperliquid candleSnapshot rows (t, o, h, l, c, v) → the coin's normal. None if too little history."""
    try:
        c = np.array([float(r["c"]) for r in rows])
        v = np.array([float(r["v"]) for r in rows]) * c          # base units → USD
    except (KeyError, TypeError, ValueError):
        return None
    if len(c) < 48 or np.any(c <= 0):
        return None
    r = np.diff(np.log(c))
    mad = float(np.median(np.abs(r - np.median(r)))) * 1.4826
    if mad > 0:
        r = np.clip(r, -6 * mad, 6 * mad)                        # one spike shouldn't set the week's normal
    sigma = float(np.std(r))
    if not sigma > 0:
        return None
    return Baseline(sigma, float(np.median(v)), float(v.sum() / (len(v) / 24)), now_ms)


@dataclass(slots=True)
class CoinHistory:
    """Recent price and open interest samples for one coin, from the market polls."""
    samples: deque = field(default_factory=deque)           # (ms, price, oi in coins)
    oi_slow: deque = field(default_factory=lambda: deque(maxlen=OI_SAMPLES))   # (ms, oi in coins), every 5 min

    def add(self, t_ms: int, price: float | None, oi: float) -> None:
        if price:
            self.samples.append((t_ms, price, oi))
        while self.samples and t_ms - self.samples[0][0] > HISTORY_S * 1000:
            self.samples.popleft()
        if oi > 0 and (not self.oi_slow or t_ms - self.oi_slow[-1][0] >= OI_SAMPLE_MS):
            self.oi_slow.append((t_ms, oi))

    def _at(self, t_ms: int):
        """The newest sample at or before t_ms, if there's one within a minute of it."""
        best = None
        for s in self.samples:
            if s[0] > t_ms:
                break
            best = s
        return best if best and t_ms - best[0] <= 60_000 else None

    def change(self, now_ms: int, seconds: int, idx: int) -> float | None:
        """% change of price (idx 1) or open interest (idx 2) over the last `seconds`."""
        if not self.samples:
            return None
        then, now = self._at(now_ms - seconds * 1000), self.samples[-1]
        if then is None or not then[idx]:
            return None
        return (now[idx] / then[idx] - 1) * 100

    def typical_oi_1h(self) -> float:
        """How much open interest usually moves in an hour (median, %), once there are enough samples."""
        s = list(self.oi_slow)
        if len(s) < 13 or (s[-1][0] - s[0][0]) < MIN_OI_SPAN_H * 3_600_000:
            return DEFAULT_OI_1H_PCT
        chg = [abs(b[1] / a[1] - 1) * 100 for a, b in zip(s, s[12:]) if a[1] > 0]
        return max(float(np.median(chg)), 0.2) if chg else DEFAULT_OI_1H_PCT


@dataclass(frozen=True, slots=True)
class TradeReads:
    buy_usd: float
    sell_usd: float
    trades: int
    big_orders: int
    big_usd: float
    liquidated_usd: float
    cascades: tuple[dict, ...]        # {"side", "orders", "usd", "seconds", "ts"}
    twaps: tuple[dict, ...]           # {"side", "per_hour", "wallet"}


def trade_reads(state, now_ms: int, seconds: int = TRADE_WINDOW_S) -> TradeReads | None:
    """The last 15 minutes of one coin's trades (a MarketState fed from the trades feed)."""
    if state is None or not state.now_ms:
        return None
    span = min(seconds, state.orders.max_seconds)
    orders = [o for o in state.orders.window(span, state.now_ms)]
    if not orders:
        return TradeReads(0.0, 0.0, 0, 0, 0.0, 0.0, (), ())
    buy = sum(o.notional for o in orders if o.side is TradeSide.BUY)
    sell = sum(o.notional for o in orders if o.side is TradeSide.SELL)
    sizes = np.array([o.notional for o in orders])
    threshold = max(BIG_ORDER_FLOOR, float(np.percentile(sizes, 99)) if len(sizes) >= 200 else 2.5 * BIG_ORDER_FLOOR)
    twaps = state.engine.twaps
    big = [o for o in orders if is_sweep(o, threshold, twaps)]
    forced = state.engine.forced
    liquidated = sum(o.notional for o in orders if o.engine and (o.timestamp, o.taker) in forced)
    cascades = tuple({"side": "buy" if c.side is TradeSide.BUY else "sell", "orders": c.sweeps, "usd": c.notional,
                      "seconds": max(1.0, (c.end_ms - c.start_ms) / 1000), "ts": c.start_ms}
                     for c in find_cascades(big))
    tw = tuple({"side": "buy" if side is TradeSide.BUY else "sell", "wallet": wallet,
                "per_hour": slice_usd * 3_600_000 / interval if interval else 0.0}
               for (wallet, side), (last, slice_usd, interval) in twaps.items() if now_ms - last <= 5 * 60_000)
    return TradeReads(buy, sell, len(orders), len(big), float(sum(o.notional for o in big)), liquidated,
                      cascades, tuple(sorted(tw, key=lambda t: -t["per_hour"])))


@dataclass(slots=True)
class Row:
    coin: str
    price: float | None
    chg_15m: float | None
    chg_1h: float | None
    chg_24h: float | None
    volume_24h: float
    volume_x: float | None            # last 24h vs an average day
    fast_volume_x: float | None       # last 15 minutes vs a normal 15 minutes (traded coins)
    oi_usd: float
    oi_chg_1h: float | None
    funding_apr: float
    premium_pct: float | None
    buy_share: float | None           # traded coins: share of the last 15m's market orders that were buys
    net_usd: float | None
    liquidated_usd: float | None
    big_orders: int | None
    cascades: tuple = ()
    twaps: tuple = ()
    traded: bool = False              # trade reads available
    learning: bool = False            # no 7-day normal yet
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()       # which kinds of reading are unusual: move, volume, positions, funding, index, trades
    lean: str = "neutral"             # "up" | "down" | "neutral", for colouring


def _pct(x: float, d: int = 1) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.{d}f}%"


def score_coin(coin: str, ctx: dict, hist: CoinHistory | None, base: Baseline | None, reads: TradeReads | None,
               now_ms: int) -> Row:
    mark, oracle = ctx.get("mark"), ctx.get("oracle")
    chg_15 = hist.change(now_ms, 900, 1) if hist else None
    chg_1h = hist.change(now_ms, 3600, 1) if hist else None
    prev = ctx.get("prev_day")
    chg_24 = (mark / prev - 1) * 100 if mark and prev else None
    oi_usd = (ctx.get("oi") or 0.0) * (mark or 0.0)
    oi_1h = hist.change(now_ms, 3600, 2) if hist else None
    apr = (ctx.get("funding") or 0.0) * 24 * 365 * 100
    prem = (mark / oracle - 1) * 100 if mark and oracle else None
    vol24 = ctx.get("day_volume") or 0.0

    parts: list[tuple[float, str, str]] = []               # (score, reason, kind)
    volume_x = fast_x = None
    if base:
        for chg, mins in ((chg_15, 15), (chg_1h, 60)):
            if chg is not None:
                z = abs(math.log1p(chg / 100)) / (base.sigma_1h * math.sqrt(mins / 60))
                label = "a normal hour" if mins == 60 else "a normal 15 minutes"
                parts.append((z / MOVE_Z, f"price {_pct(chg)} in {'1h' if mins == 60 else '15m'}, "
                                          f"{z:.1f}× {label}", "move"))
        if base.vol_day > 0:
            volume_x = vol24 / base.vol_day
            parts.append((volume_x / DAY_VOLUME_X, f"{volume_x:.1f}× its usual daily volume", "volume"))
    if oi_1h is not None:
        typ = hist.typical_oi_1h()
        parts.append((abs(oi_1h) / (OI_X * typ), f"open interest {_pct(oi_1h)} in 1h", "positions"))
    parts.append((abs(apr) / FUNDING_APR, f"funding {abs(apr):.0f}% a year, {'longs' if apr >= 0 else 'shorts'} pay",
                  "funding"))
    if prem is not None:
        parts.append((abs(prem) / PREMIUM_PCT, f"{abs(prem):.2f}% {'above' if prem >= 0 else 'below'} the index",
                      "index"))

    buy_share = net = liq = None
    big = None
    if reads is not None:
        total = reads.buy_usd + reads.sell_usd
        buy_share = reads.buy_usd / total if total else None
        net = reads.buy_usd - reads.sell_usd
        liq, big = reads.liquidated_usd, reads.big_orders
        hour = base.vol_hour if base and base.vol_hour > 0 else None
        if hour:
            fast_x = total / (hour / 4)
            parts.append((fast_x / FAST_VOLUME_X, f"trading {fast_x:.1f}× faster than usual (last 15m)", "volume"))
            parts.append((liq / (LIQ_SHARE * hour), f"{fmt_usd(liq)} liquidated in 15m", "trades"))
            for c in reads.cascades[-1:]:
                parts.append((c["usd"] / (CASCADE_SHARE * hour),
                               f"run of {c['orders']} big {c['side']} orders: {fmt_usd(c['usd'])} in {c['seconds']:.0f}s",
                               "trades"))
            for t in reads.twaps[:1]:
                parts.append((t["per_hour"] / (TWAP_SHARE * hour),
                               f"TWAP bot {'buying' if t['side'] == 'buy' else 'selling'} about "
                               f"{fmt_usd(t['per_hour'])} an hour", "trades"))
            if buy_share is not None and fast_x >= ONE_SIDED_VOLUME_X and max(buy_share, 1 - buy_share) >= ONE_SIDED:
                side = buy_share >= 0.5
                share = buy_share if side else 1 - buy_share
                parts.append((1.0 + (share - ONE_SIDED) * 5, f"{share:.0%} {'buying' if side else 'selling'} in 15m",
                              "trades"))

    parts.sort(key=lambda p: -p[0])
    score = parts[0][0] if parts else 0.0
    reasons = [r for s, r, _ in parts if s >= 1.0][:3]
    if vol24 < THIN_VOLUME:                    # listed, but never ranked above coins people actually trade
        score = min(score, 0.99)
        if reasons:
            reasons.append(f"thin trading: {fmt_usd(vol24)} in 24h")
    move = chg_1h if chg_1h is not None else chg_24
    return Row(
        coin=coin, price=mark, chg_15m=chg_15, chg_1h=chg_1h, chg_24h=chg_24, volume_24h=vol24, volume_x=volume_x,
        fast_volume_x=fast_x, oi_usd=oi_usd, oi_chg_1h=oi_1h, funding_apr=apr, premium_pct=prem,
        buy_share=buy_share, net_usd=net, liquidated_usd=liq, big_orders=big,
        cascades=reads.cascades if reads else (), twaps=reads.twaps[:3] if reads else (),
        traded=reads is not None, learning=base is None, score=round(score, 3),
        reasons=tuple(reasons),
        flags=tuple(dict.fromkeys(k for s, _, k in parts if s >= 1.0)),
        lean="neutral" if move is None or abs(move) < 0.05 else "up" if move > 0 else "down",
    )

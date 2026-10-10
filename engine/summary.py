"""
The timeframe cards: what a professional would want to know about each horizon, in one glance.

Each card shows five rows chosen for its timeframe. Every row is a number (for the
pro), a one- or two-word verdict (for everyone), a bar, and a tooltip saying exactly
how it was worked out:

    1m    execution     Flow · Who · Forced · Book · Liquidity
    10m   scalp         Flow · Who · Trend · Avg price · Book
    60m   session       Trend · Avg price · Flow · Who · Positions
    6h+   swing         Trend · Avg price · Positions · Funding · Flow

  Flow         aggressive buying vs selling, net $, and what it *did*: price impact vs what
               that much net flow usually does (price held / rose or fell anyway / big reaction)
  Who          how concentrated the aggressive side is: one wallet, a TWAP algo, or many
               traders. Hyperliquid prints the wallet behind every trade; engine-executed
               fills (no tx hash) are split into TWAP slices and liquidations (engineFlow.py)
  Forced       sweeps, cascades and engine-executed liquidations, checked against open interest
  Book         resting depth near the price now, how it changed, and whether walls are real
  Liquidity    slippage of a large market order right now, vs its usual cost
  Trend        efficiency ratio (net move ÷ distance travelled over 30 equal steps) and
               position in the range. On simulated random walks: mean 0.18; "trend" fires
               on ~4% of noise windows and catches ~93% of real drifts; "choppy" ~4% of noise
  Avg price    price vs the window's VWAP, scored in units of how far a random market sits
               from its own VWAP (σ√T/√3): "far above/below" is beyond ~95% of random moments
  Positions    open-interest change, ranked against every open interest move of the same timeframe
  Funding      the rate, what holding this long costs, and whether one side is crowded
               (mid-rank percentile vs the past week, and meaningfully above the floor)

Everything comes from the window slice the explainer already built: one pass over the
orders and the price path, a few milliseconds even for 60m.
"""
import math
from collections import deque
from dataclasses import dataclass

from models.bookModel import BookLevel, OrderBook
from models.explanationModel import FlowImpact, Metric, PriceMove
from models.orderModel import is_twap_slice
from models.signalModel import Direction, SignalResult
from models.tradeModel import TradeSide
from signals.base import WindowSlice, fmt_px, fmt_usd
from signals.depthDelta import _wall_note

N_STEPS = 30                      # efficiency is measured over 30 equal steps on every timeframe
RANDOM_WALK_ER = 1 / math.sqrt(N_STEPS)
# Calibrated on simulated random walks: "trend" fires on ~4% of pure-noise windows and catches ~93% of
# real drifts; "chop" (going nowhere while covering at least a normal range) fires on ~4% of noise.
TREND_ER, TREND_Z = 0.45, 1.5
CHOP_ER, CHOP_RANGE = 0.10, 1.0
QUIET_RANGE = 0.6
# VWAP: the last price sits ~N(0, σ√T/√3) from a random walk's VWAP, so distances are scored in those units.
VWAP_SD = 1 / math.sqrt(3)
AT_VALUE_SD, STRETCHED_SD = 0.5, 2.0
RANGE_PER_SIGMA = math.sqrt(8 / math.pi)   # expected high–low range of a random walk, in σ·√T (Parkinson)
MIN_ORDERS_FOR_WHO = 8
FLOW_LEAN = 0.25

CROWDED_LONG_APR = 15.0            # Hyperliquid's floor is ~11% APR: "crowded" has to be meaningfully above it

LAYOUTS = {
    "execution": ("flow", "who", "forced", "book", "liquidity"),
    "scalp": ("flow", "who", "trend", "vwap", "book"),
    "session": ("trend", "vwap", "flow", "who", "positioning"),
    "swing": ("trend", "vwap", "positioning", "funding", "flow"),
}


def layout_for(seconds: int) -> tuple[str, ...]:
    if seconds <= 120:
        return LAYOUTS["execution"]
    if seconds <= 900:
        return LAYOUTS["scalp"]
    if seconds <= 3600:
        return LAYOUTS["session"]
    return LAYOUTS["swing"]


@dataclass(frozen=True, slots=True)
class SummaryContext:
    live: bool                          # the live window (False for a past moment via explain_at)
    book: OrderBook | None              # the live order book (liquidity is only meaningful "now")
    usual_sigma_1s_bps: float           # long-run volatility per √second, for the usual range
    liquidity_size: float               # USD size for the liquidity row
    liquidity_usual_bps: float | None   # that size's typical cost over the last 30 minutes


# ── helpers ────────────────────────────────────────────────────────────────
def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _ordinal(p: float) -> str:
    n = min(99, max(1, round(p * 100)))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "?")


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _lean(score: float, eps: float) -> Direction:
    return Direction.UP if score >= eps else Direction.DOWN if score <= -eps else Direction.NEUTRAL


def efficiency(path: tuple[tuple[int, float], ...], start_ms: int, end_ms: int, n: int = N_STEPS) -> float | None:
    """Kaufman efficiency ratio over n equal time steps: |net move| ÷ sum of |step moves|.
    A random walk scores ~1/√n (0.18 for n=30); a straight line 1.0. Only the stretch the path actually
    covers is measured: a window still filling up would otherwise count its empty start as a flat line."""
    if len(path) < 2:
        return None
    start_ms = max(start_ms, path[0][0])
    if end_ms - start_ms < 10_000:
        return None
    prices = []
    j = 0
    for k in range(n + 1):
        t = start_ms + (end_ms - start_ms) * k / n
        while j + 1 < len(path) and path[j + 1][0] <= t:
            j += 1
        prices.append(path[j][1])
    travelled = sum(abs(b - a) for a, b in zip(prices, prices[1:]))
    if travelled <= 0:
        return 0.0
    return abs(prices[-1] - prices[0]) / travelled


def slippage_bps(levels: tuple[BookLevel, ...], usd: float, mid: float) -> float | None:
    """Average fill vs mid for a market order of `usd`, walking the visible levels. None if the book is too thin."""
    if not levels or mid <= 0:
        return None
    left, cost, size = usd, 0.0, 0.0
    for l in levels:
        take = min(l.size, left / l.price)
        cost += take * l.price
        size += take
        left -= take * l.price
        if left <= 1e-6:
            return abs(cost / size - mid) / mid * 10_000
    return None


def _cost(bps: float | None, usd: float) -> str:
    """Slippage in dollars for an order of `usd`: "$2.40", "$310", or "?" when the book can't fill it."""
    if bps is None:
        return "?"
    d = bps / 10_000 * usd
    return f"${d:,.2f}" if d < 10 else f"${d:,.0f}"


def nice_size(usd: float) -> float:
    """A round order size near `usd`: 25K, 50K, 100K, 250K, 500K, 1M…"""
    steps = [25e3, 50e3, 100e3, 250e3, 500e3, 1e6, 2.5e6, 5e6, 10e6]
    return min(steps, key=lambda x: abs(math.log(x / max(usd, 1.0))))


class LiquidityTracker:
    """Keeps the typical cost of a large order over the last 30 minutes, sampled every 5 seconds."""

    def __init__(self, keep_ms: int = 30 * 60_000, every_ms: int = 5_000):
        self.keep_ms, self.every_ms = keep_ms, every_ms
        self.size = 0.0
        self._samples: deque[tuple[int, float]] = deque()
        self._last = 0

    def update(self, book: OrderBook | None, now_ms: int, sweep_threshold: float) -> None:
        if book is None or book.mid is None or now_ms - self._last < self.every_ms:
            return
        if not self.size or not 0.5 <= sweep_threshold / self.size <= 2.0:   # hysteresis: no flip-flopping
            self.size, self._samples = nice_size(sweep_threshold), deque()
        size = self.size
        buy, sell = slippage_bps(book.asks, size, book.mid), slippage_bps(book.bids, size, book.mid)
        if buy is not None and sell is not None:
            self._samples.append((now_ms, (buy + sell) / 2))
        while self._samples and now_ms - self._samples[0][0] > self.keep_ms:
            self._samples.popleft()
        self._last = now_ms

    def usual(self) -> float | None:
        if len(self._samples) < 12:                 # a minute of samples before calling anything thin or deep
            return None
        xs = sorted(c for _, c in self._samples)
        return xs[len(xs) // 2]


# ── the rows ───────────────────────────────────────────────────────────────
def flow_row(s: WindowSlice, signals: dict[str, SignalResult], impact: FlowImpact | None) -> Metric:
    f = signals.get("volume_imbalance")
    help_ = ("Market orders: how much was buying vs selling, and the net dollars. The tag says what that buying "
             "or selling did to price. Price held = price moved far less than this much buying or selling usually "
             "pushes it, because orders waiting in the book soaked it up. Rose anyway / fell anyway = price went "
             "the opposite way, so something else is driving it (another exchange, or a bigger player). Big "
             "reaction = price moved much more than usual for this much flow: a thin book, or a move that started "
             "on another exchange. Otherwise the tag says how busy trading is compared with normal.")
    if not f or not f.metrics or f.metrics.get("buy_notional", 0) + f.metrics.get("sell_notional", 0) <= 0:
        return Metric("flow", "Flow", "no trades yet", "", Direction.NEUTRAL, 0.0, help=help_)
    buy, sell = f.metrics["buy_notional"], f.metrics["sell_notional"]
    total, net = buy + sell, buy - sell
    side = "buy" if net >= 0 else "sell"
    share = max(buy, sell) / total
    activity = f.metrics.get("activity", 1.0)
    partial = s.resolution == "bar" and s.flow_coverage < 0.95
    value = f"{share:.0%} {side} · net {'+' if net >= 0 else '−'}{fmt_usd(abs(net))}"
    if partial:
        value = f"{share:.0%} {side} · last {_dur(s.flow.seconds if s.flow else 0)} only"
    if partial:
        tag = "partial"
    elif impact:
        tag = {"against": "rose anyway" if net < 0 else "fell anyway", "absorbed": "price held",
               "outsized": "big reaction", "normal": "normal"}[impact.verdict]
    else:
        tag = "busy" if activity >= 1.8 else "quiet" if activity <= 0.5 else "steady"
    detail = f"Bought {fmt_usd(buy)}, sold {fmt_usd(sell)}, at {activity:.1f}× the usual pace."
    if impact:
        detail += (f" This much net {'buying' if net >= 0 else 'selling'} usually moves price "
                   f"{impact.expected_bps / 100:+.2f}%. It moved {impact.actual_bps / 100:+.2f}%.")
    lean = f.direction if f.strength >= FLOW_LEAN else Direction.NEUTRAL
    return Metric("flow", "Flow", value, tag, lean, _clip(f.strength), help=help_, detail=detail, partial=partial)


def who_row(s: WindowSlice) -> Metric:
    help_ = ("Who is behind the buying or selling. Hyperliquid shows the wallet behind every trade, so this "
             "shows whether the busier side is one wallet (a whale), a TWAP bot, or many separate traders. A TWAP "
             "bot splits one big order into small pieces every 30 seconds or so, and keeps going whatever the "
             "price does. Many wallets on one side usually means real conviction. One bot selling into steady "
             "buyers is a very different market from a crowd selling.")
    orders = s.orders
    if len(orders) < MIN_ORDERS_FOR_WHO:
        return Metric("who", "Who", f"only {len(orders)} trades", "quiet", Direction.NEUTRAL, 0.0, help=help_)
    BUY, SELL = TradeSide.BUY, TradeSide.SELL
    twaps = s.twaps or {}
    tot = {BUY: 0.0, SELL: 0.0}
    twap = {BUY: 0.0, SELL: 0.0}
    usd: dict[TradeSide, dict[str, float]] = {BUY: {}, SELL: {}}
    for o in orders:                       # one lean pass: per-wallet dollars only
        side, n = o.side, o.notional
        tot[side] += n
        if o.engine and is_twap_slice(o, twaps):
            twap[side] += n
        t = o.taker
        if t is not None:
            d = usd[side]
            d[t] = d.get(t, 0.0) + n
    if not usd[BUY] and not usd[SELL]:
        return Metric("who", "Who", "no wallet data", "", Direction.NEUTRAL, 0.0, help=help_)
    dom = BUY if tot[BUY] >= tot[SELL] else SELL
    other = SELL if dom is BUY else BUY

    def concentration(side: TradeSide) -> float:
        ws = usd[side]
        if not ws or tot[side] <= 0:
            return 0.0
        return max(max(ws.values()), twap[side]) / tot[side]

    # Usually the dominant side tells the story, unless one wallet or TWAP is quietly working the other side
    # (a whale buying into broad selling is the more important fact).
    everything = tot[BUY] + tot[SELL]
    if (concentration(other) >= 0.35 and tot[other] >= 0.25 * everything
            and concentration(other) > concentration(dom) + 0.15):
        dom, other = other, dom
    word = "buying" if dom is BUY else "selling"
    wallets = usd[dom]
    if not wallets or tot[dom] <= 0:
        return Metric("who", "Who", "no wallet data", "", Direction.NEUTRAL, 0.0, help=help_)
    top_addr, top_usd = max(wallets.items(), key=lambda kv: kv[1])
    top_ts = [o.timestamp for o in orders if o.taker == top_addr and o.side is dom]   # second pass: one wallet
    top_n = len(top_ts)
    top_twap = sum(o.notional for o in orders
                   if o.taker == top_addr and o.side is dom and is_twap_slice(o, twaps))
    top_share = top_usd / tot[dom]
    twap_share = twap[dom] / tot[dom]
    top_is_twap = top_twap >= 0.5 * top_usd
    n_buy, n_sell = len(usd[BUY]), len(usd[SELL])
    if top_share >= 0.35:
        value = f"1 {'TWAP bot' if top_is_twap else 'wallet'} = {top_share:.0%} of {word}"
        tag = "TWAP" if top_is_twap else "whale" if top_share >= 0.5 else "few wallets"
    elif twap_share >= 0.3:
        value = f"TWAP bots = {twap_share:.0%} of {word}"
        tag = "TWAP"
    else:
        value = f"{n_sell} sellers · {n_buy} buyers"
        tag = "many wallets"
    cadence = rate = ""
    plan = twaps.get((top_addr, dom)) if top_is_twap else None
    if plan and len(plan) >= 3 and plan[2] > 0:                  # the classifier knows the TWAP's schedule
        cadence = f", about {fmt_usd(plan[1])} every {plan[2] / 1000:.0f}s"
        rate = f" (about {fmt_usd(plan[1] * 3_600_000 / plan[2])} an hour)"
    elif top_n >= 4 and top_share >= 0.2:                        # a pattern worth describing
        ts = sorted(top_ts)
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        cadence = f", about every {gaps[len(gaps) // 2] / 1000:.0f}s"
        span_s = (ts[-1] - ts[0]) / 1000
        if span_s >= 300:                                         # a pace needs a real stretch of time behind it
            rate = f" (about {fmt_usd(top_usd / span_s * 3600)} an hour)"
        elif span_s > 0:
            rate = f" within {span_s:.0f}s"
    who = "buyer" if dom is TradeSide.BUY else "seller"
    detail = (f"Biggest {who}: {_short(top_addr)}{' (a TWAP bot)' if top_is_twap else ''}, {fmt_usd(top_usd)} in "
              f"{top_n} order{'s' if top_n != 1 else ''}{cadence}{rate}. {n_sell} wallet{'s' if n_sell != 1 else ''} sold, {n_buy} bought. "
              f"TWAP bots: {twap_share:.0%} of the {word}.")
    dom_share = tot[dom] / everything
    lean = (Direction.UP if dom is TradeSide.BUY else Direction.DOWN) if dom_share >= 0.55 else Direction.NEUTRAL
    return Metric("who", "Who", value, tag, lean, _clip(max(top_share, twap_share)), help=help_, detail=detail)


def forced_row(s: WindowSlice, signals: dict[str, SignalResult]) -> Metric:
    help_ = ("Unusually big market orders that ate through several price levels (sweeps), and chains of them "
             "within seconds (cascades). That's what liquidations and stop runs look like. When the exchange "
             "itself force-closes accounts, those trades show up directly and are counted in dollars. Cascades "
             "are also checked against open interest (all open positions): if it fell, positions really were "
             "force-closed (liquidations); if not, it was more likely one big trader.")
    sig = signals.get("liquidations")
    if not sig or not sig.metrics:
        return Metric("forced", "Forced", "no trades yet", "", Direction.NEUTRAL, 0.0, help=help_)
    n = int(sig.metrics.get("sweeps_buy", 0) + sig.metrics.get("sweeps_sell", 0))
    liq = sig.metrics.get("engine_forced_usd", 0.0)
    accounts = int(sig.metrics.get("engine_forced_accounts", 0))
    if n == 0 and not liq:
        return Metric("forced", "Forced", "no big orders", "quiet", Direction.NEUTRAL, 0.0, help=help_,
                      detail=sig.summary)
    if liq:
        value = f"{sig.stat} · {fmt_usd(liq)}" if n else f"{fmt_usd(liq)} liquidated · {accounts} accounts"
        return Metric("forced", "Forced", value, "liquidations", sig.direction, _clip(max(sig.strength, 0.5)),
                      help=help_, detail=sig.summary)
    inside = [c for c in s.cascades if c.end_ms >= s.start_ms and c.start_ms <= s.end_ms]
    biggest = max(inside, key=lambda c: abs(c.move_bps), default=None)
    if biggest and sig.metrics.get("cascades", 0):
        tag = {"likely": "liquidations", "partly": "partly forced", "unlikely": "one trader"}.get(
            biggest.verdict or "", "checking" if not biggest.oi_settled else "cascade")
    else:
        tag = "big orders"
    return Metric("forced", "Forced", sig.stat, tag, sig.direction if sig.strength >= FLOW_LEAN else Direction.NEUTRAL,
                  _clip(sig.strength), help=help_, detail=sig.summary)


def book_row(s: WindowSlice, signals: dict[str, SignalResult], short: bool) -> Metric:
    help_ = ("Orders waiting in the book within 0.25% of the price: how much is on the buy side (bids) vs the "
             "sell side (asks) right now, and how each side changed over the window. Big walls that keep "
             "vanishing as price gets close are flagged as possibly fake.")
    d = signals.get("depth_delta")
    b = s.book_end
    if not d or not b or b.bid_notional + b.ask_notional <= 0:
        return Metric("book", "Book", "no book yet", "", Direction.NEUTRAL, 0.0, help=help_)
    imb = b.imbalance
    bids = (imb + 1) / 2
    split = f"{bids * 100:.0f}/{(1 - bids) * 100:.0f} bid/ask"
    value = f"{split} · {d.stat}" if short else f"{d.stat} · {split}"
    bait = _wall_note(s.walls, d.metrics.get("bid_change", 0.0), d.metrics.get("ask_change", 0.0)).startswith(" Careful")
    tag = "fake walls?" if bait else "more bids" if imb >= 0.2 else "more asks" if imb <= -0.2 else "balanced"
    lean = _lean(imb, 0.2) if not bait else Direction.NEUTRAL
    return Metric("book", "Book", value, tag, lean, _clip(abs(imb) / 0.6), help=help_, detail=d.summary)


def liquidity_row(ctx: SummaryContext) -> Metric:
    size = ctx.liquidity_size
    help_ = (f"What a {fmt_usd(size)} market order would lose to slippage right now, buying and selling, "
             "worked out from the visible order book. Compared with its usual cost over the last 30 minutes: "
             "thin = price is easier to push and stop losses fill worse; deep = big orders barely move it.")
    book = ctx.book
    if not ctx.live or book is None or book.mid is None:
        return Metric("liquidity", "Liquidity", "live only", "", Direction.NEUTRAL, 0.0, help=help_)
    buy, sell = slippage_bps(book.asks, size, book.mid), slippage_bps(book.bids, size, book.mid)
    value = f"{fmt_usd(size)}: {_cost(buy, size)} buy · {_cost(sell, size)} sell"
    if buy is None or sell is None:
        return Metric("liquidity", "Liquidity", value, "too big", Direction.NEUTRAL, 1.0, help=help_,
                      detail="The feed shows 20 price levels a side, and on one side they can't fill this size. "
                             "The real book may be deeper.")
    now = (buy + sell) / 2
    usual = ctx.liquidity_usual_bps
    if usual is None or usual <= 0:
        return Metric("liquidity", "Liquidity", value, "learning", Direction.NEUTRAL, 0.5, help=help_,
                      detail="Watching for 30 minutes before calling it thin or deep.")
    ratio = now / usual
    tag = "thin" if ratio >= 1.6 else "deep" if ratio <= 0.7 else "normal"
    return Metric("liquidity", "Liquidity", value, tag, Direction.NEUTRAL, _clip(ratio / 2), help=help_,
                  detail=f"Costs {ratio:.1f}× the usual right now ({now / 100:.3f}% vs {usual / 100:.3f}% usually).")


def trend_row(s: WindowSlice, move: PriceMove) -> Metric:
    help_ = ("One-way = how much of the distance price travelled actually went in one direction, measured over "
             "30 equal steps. A random market scores about 18%. Uptrend / downtrend = at least 45% one-way and a "
             "move of at least 1.5× a normal move; a random market does that less than 5% of the time, so the "
             "trend is real and buying dips (or selling rallies) tends to work. Choppy = 10% one-way or less "
             "while still swinging a full normal range: price is going nowhere fast, so trade the edges and "
             "don't chase. The second part says where price sits in the window's range.")
    er = efficiency(s.path, s.start_ms, s.end_ms)
    if er is None:
        return Metric("trend", "Trend", "not enough data", "", Direction.NEUTRAL, 0.0, help=help_)
    hi, lo = move.high, move.low
    last = s.path[-1][1] if s.path else move.end_price
    pos = _clip((last - lo) / (hi - lo)) if hi > lo else 0.5
    # Judge the range against the same yardstick as the z-score (recent volatility): on a busy day the
    # week-based ratio would call ordinary noise "chop".
    rng_bps = (hi / lo - 1) * 10_000 if lo > 0 else 0.0
    rr = rng_bps / (RANGE_PER_SIGMA * move.expected_bps) if move.expected_bps > 0 else 1.0
    if er >= TREND_ER and abs(move.z) >= TREND_Z:
        tag = "uptrend" if move.move_bps > 0 else "downtrend"
        lean = Direction.UP if move.move_bps > 0 else Direction.DOWN
    elif er <= CHOP_ER and rr >= CHOP_RANGE:
        tag, lean = "choppy", Direction.NEUTRAL
    elif rr < QUIET_RANGE:
        tag, lean = "quiet", Direction.NEUTRAL
    else:
        tag, lean = "sideways", Direction.NEUTRAL
    where = "near the high" if pos >= 0.8 else "near the low" if pos <= 0.2 else "mid-range"
    value = f"{er:.0%} one-way · {where}"
    detail = (f"A random market is about {RANDOM_WALK_ER:.0%} one-way. Range {fmt_px(lo)} to {fmt_px(hi)}, "
              f"{rr:.1f}× a normal range lately. Price {fmt_px(last)} is {pos:.0%} of the way up it. "
              f"The net move is {abs(move.z):.1f}× a normal move.")
    return Metric("trend", "Trend", value, tag, lean, _clip(er / 0.6), help=help_, detail=detail,
                  partial=s.coverage < 0.95)


def vwap_row(s: WindowSlice, move: PriceMove) -> Metric:
    help_ = ("The average price everyone paid in this window, weighted by size (VWAP). Above it, the average "
             "buyer is in profit and buyers are in control; below it, sellers are. The distance is compared with "
             "how far a random market usually sits from its own average: 'at average' is the middle 40%, 'far "
             "above' or 'far below' is further than 95% of random moments, and price often snaps back from there.")
    last = s.path[-1][1] if s.path else move.end_price
    if not s.vwap or not last:
        return Metric("vwap", "Avg price", "no trades yet", "", Direction.NEUTRAL, 0.0, help=help_)
    dev_bps = (last / s.vwap - 1) * 10_000
    sd = move.expected_bps * VWAP_SD
    z = dev_bps / sd if sd > 0 else 0.0
    tag = ("at average" if abs(z) < AT_VALUE_SD else ("far above" if z > 0 else "far below") if abs(z) >= STRETCHED_SD
           else "above average" if z > 0 else "below average")
    value = f"{dev_bps / 100:+.2f}% vs {fmt_px(s.vwap)}"
    detail = (f"Price is {abs(z):.1f}× the usual distance {'above' if z >= 0 else 'below'} the average for a "
              f"{s.label} window (usually about {sd / 100:.2f}%).")
    return Metric("vwap", "Avg price", value, tag, _lean(z, AT_VALUE_SD), _clip(abs(z) / 3), help=help_, detail=detail,
                  partial=s.coverage < 0.95)


_QUADRANT_TAG = {
    "new longs opening": ("new longs", Direction.UP),
    "shorts covering": ("shorts exit", Direction.UP),
    "new shorts opening": ("new shorts", Direction.DOWN),
    "longs closing": ("longs exit", Direction.DOWN),
    "positions building on both sides": ("both adding", Direction.NEUTRAL),
    "positions unwinding": ("both closing", Direction.NEUTRAL),
    "open interest roughly flat": ("flat", Direction.NEUTRAL),
}


def positioning_row(s: WindowSlice, signals: dict[str, SignalResult]) -> Metric:
    help_ = ("How open interest (the total size of all open positions) changed over the window, read with "
             "price. Open interest up and price up = new longs. Down and price up = shorts closing. Up and price "
             "down = new shorts. Down and price down = longs closing. 'Top 3%' means the change was bigger "
             "than 97% of past changes over this timeframe.")
    f = signals.get("funding")
    if not f or not f.metrics:
        return Metric("positioning", "Positions", "no data yet", "", Direction.NEUTRAL, 0.0, help=help_)
    m = f.metrics
    chg = m.get("oi_change_pct", 0.0)
    quadrant = f.phrase.split(" (open interest")[0] if f.phrase else ""
    if quadrant.startswith("crowded"):
        tag, lean = ("long squeeze", Direction.DOWN) if "longs" in quadrant else ("short squeeze", Direction.UP)
    else:
        tag, lean = _QUADRANT_TAG.get(quadrant, ("flat", Direction.NEUTRAL))
    pct = m.get("oi_pct")
    oi_usd = m.get("open_interest_usd", 0.0)
    delta_usd = oi_usd - oi_usd / (1 + chg / 100) if chg > -100 else 0.0
    value = f"{chg:+.2f}% ({'+' if delta_usd >= 0 else '−'}{fmt_usd(abs(delta_usd))})"
    if pct is not None and pct >= 0.9:
        value += f" · top {max(1, round((1 - pct) * 100))}%"
    partial = s.resolution == "bar" and s.flow_coverage < 0.95
    if partial:
        value = f"{chg:+.2f}% · last {_dur(s.seconds * s.flow_coverage)} only"
    return Metric("positioning", "Positions", value, tag if f.strength >= 0.2 else "flat",
                  lean if f.strength >= 0.2 else Direction.NEUTRAL, _clip(f.strength), help=help_,
                  detail=f.summary, partial=partial)


def funding_row(s: WindowSlice, signals: dict[str, SignalResult]) -> Metric:
    help_ = ("Funding: who pays whom every hour to hold a position, as a yearly rate, and what holding one for "
             "this whole timeframe costs. The tag compares it with this coin's past week. Crowded = one side is "
             "paying a lot to stay in, which can fuel a squeeze if price turns against them.")
    f = signals.get("funding")
    if not f or not f.metrics or "funding_apr" not in f.metrics:
        return Metric("funding", "Funding", "no funding data yet", "", Direction.NEUTRAL, 0.0, help=help_)
    apr, hourly = f.metrics["funding_apr"], f.metrics.get("funding_hourly", 0.0)
    pct = f.metrics.get("funding_pct")
    cost = abs(hourly) * s.seconds / 3600 * 100
    payer = "longs" if hourly >= 0 else "shorts"
    value = f"{payer} pay {cost:.3f}% per {s.label}"
    if pct is not None:
        tag = ("crowded longs" if pct >= 0.9 and apr >= CROWDED_LONG_APR
               else "crowded shorts" if pct <= 0.1 and apr < 0 else "normal")
        bar, kind = pct, "position"
        prem = f.metrics.get("premium_bps", 0) / 100
        detail = (f"{payer.capitalize()} pay {abs(apr):.1f}% a year. Holding a position for {s.label} costs {cost:.3f}% of its size. "
                  f"Higher than {pct:.0%} of the past week's hourly readings. "
                  f"Hyperliquid's price is {abs(prem):.3f}% {'above' if prem >= 0 else 'below'} the index price.")
    else:
        tag = "crowded longs" if apr >= 20 else "crowded shorts" if apr <= -20 else "normal"
        bar, kind = _clip(abs(apr) / 50), "fill"
        detail = (f"{payer.capitalize()} pay {abs(apr):.1f}% a year. Holding a position for {s.label} costs {cost:.3f}% of its size. "
                  "Not enough history yet to compare it with the past week, so crowded means above 20% a year.")
    return Metric("funding", "Funding", value, tag, Direction.NEUTRAL, bar, kind=kind, help=help_, detail=detail)


def summarize(s: WindowSlice, move: PriceMove, signals: list[SignalResult], impact: FlowImpact | None,
              ctx: SummaryContext) -> tuple[tuple[Metric, ...], float | None]:
    """The card's rows for this timeframe, plus its range vs the usual range."""
    by = {x.name: x for x in signals}
    usual = RANGE_PER_SIGMA * ctx.usual_sigma_1s_bps * math.sqrt(max(1.0, s.seconds * s.coverage))
    rng = (move.high / move.low - 1) * 10_000 if move.low > 0 else 0.0
    range_ratio = rng / usual if usual > 0 else None
    rows = []
    for key in layout_for(s.seconds):
        if key == "flow":
            rows.append(flow_row(s, by, impact))
        elif key == "who":
            rows.append(who_row(s))
        elif key == "forced":
            rows.append(forced_row(s, by))
        elif key == "book":
            rows.append(book_row(s, by, short=s.seconds <= 120))
        elif key == "liquidity":
            rows.append(liquidity_row(ctx))
        elif key == "trend":
            rows.append(trend_row(s, move))
        elif key == "vwap":
            rows.append(vwap_row(s, move))
        elif key == "positioning":
            rows.append(positioning_row(s, by))
        elif key == "funding":
            rows.append(funding_row(s, by))
    return tuple(rows), range_ratio

"""
Alerts: the moments worth a ping, checked once a second for every coin.

    cascade       a chain of forced flow, once its OI check is in (≥ min liquidated or swept)
    liquidation   one account force-closed by the engine (≥ min), its pieces added up
    twap          a wallet starts slicing a big order (≥ min an hour)
    level         a defended level breaks
    absorbed      heavy one-sided flow fails to move price on the 10m or 60m card
    volatility    the 10m range is ≥ N× its usual range
    price         price crosses a level you set (one-shot)
    position      a watched wallet's chance of liquidation rises past N% (raised by api/positions.py)

Every alert has a cooldown key (per coin and type, or per level / wallet where each one is
its own story), so a messy hour can't send twenty pings. Nothing fires in the first five
minutes after start-up except price alerts: the engine is still learning what's normal, and
TWAPs that were already running would all look new.

The checker only reads the pipeline; sending (Telegram, browser) lives in api/alerts.py.
"""
import html
import time
from dataclasses import dataclass, field

from models.orderModel import AggressiveOrder
from models.tradeModel import TradeSide
from signals.base import fmt_px, fmt_usd

KINDS = ("cascade", "liquidation", "twap", "level", "absorbed", "volatility", "price", "position")
DEFAULT_RULES: dict[str, dict] = {
    "cascade": {"on": True, "min_usd": 1_000_000},
    "liquidation": {"on": True, "min_usd": 500_000},
    "twap": {"on": True, "min_usd_per_hour": 1_000_000},
    "level": {"on": True},
    "absorbed": {"on": True},
    "volatility": {"on": True, "min_ratio": 2.5},
    "price": {"on": True},
    "position": {"on": True, "min_pct": 5.0},
}
DEFAULT_COOLDOWN_MIN = 15
WARMUP_MS = 5 * 60_000
LIQ_GROUP_MS = 60_000             # a liquidation's pieces (20% chunk, then the rest) arrive within this
ABSORB_WINDOWS = ("10m", "60m")
ABSORB_TAGS = {"price held": "price held", "rose anyway": "price rose anyway", "fell anyway": "price fell anyway"}


@dataclass(frozen=True, slots=True)
class Alert:
    id: str
    coin: str
    kind: str
    ts: int                       # exchange ms
    direction: str                # "up" | "down" | "neutral"
    title: str
    lines: tuple[str, ...] = ()

    def telegram_html(self) -> str:
        icon = {"up": "🔺", "down": "🔻"}.get(self.direction, "⚡" if self.kind == "volatility" else "•")
        if self.kind == "price":
            icon = "🎯"
        elif self.kind == "position":
            icon = "⚠️"
        body = "\n".join(html.escape(x) for x in self.lines)
        return f"{icon} <b>{html.escape(self.title)}</b>" + (f"\n{body}" if body else "")


@dataclass(slots=True)
class PriceLevel:
    id: str
    coin: str
    price: float
    created_ms: int
    triggered_ms: int | None = None


@dataclass(slots=True)
class _CoinMemory:
    first_ms: int | None = None
    last_mid: float | None = None
    seen_events: set[str] = field(default_factory=set)
    seen_cascades: set[int] = field(default_factory=set)
    seen_twaps: set[tuple] = field(default_factory=set)
    seen_liq: set[tuple] = field(default_factory=set)


class AlertEngine:
    def __init__(self, rules: dict | None = None, cooldown_min: float = DEFAULT_COOLDOWN_MIN,
                 coins: list[str] | None = None):
        self.rules = {k: {**v, **((rules or {}).get(k) or {})} for k, v in DEFAULT_RULES.items()}
        self.cooldown_ms = int(cooldown_min * 60_000)
        self.coins = list(coins or [])            # empty = every coin
        self.levels: list[PriceLevel] = []
        self._mem: dict[str, _CoinMemory] = {}
        self._last_fired: dict[tuple, int] = {}

    # ── one check per coin ───────────────────────────────────────────────
    def check(self, coin: str, pipe) -> list[Alert]:
        st, an = pipe.state, pipe.analyzer
        now = st.now_ms
        if not now:
            return []
        mem = self._mem.setdefault(coin, _CoinMemory())
        if mem.first_ms is None:
            mem.first_ms = now
        out: list[Alert] = []
        watching = not self.coins or coin in self.coins
        warm = now - mem.first_ms >= WARMUP_MS
        mid = st.mid

        if watching and self._on("price"):
            out += self._price(coin, mem.last_mid, mid, now)
        if mid:
            mem.last_mid = mid
        # always mark what's been seen, so switching a rule on doesn't replay the past
        new_cascades = [t for t in an.cascades.settled() if t.id not in mem.seen_cascades]
        mem.seen_cascades.update(t.id for t in new_cascades)
        new_levels = [e for e in an.market_events() if e.kind == "level_break" and e.id not in mem.seen_events]
        mem.seen_events.update(e.id for e in new_levels)
        new_twaps = [(k, v) for k, v in st.engine.twaps.items() if k not in mem.seen_twaps]
        mem.seen_twaps = set(st.engine.twaps)
        liqs = self._new_liquidations(st, mem, now)
        if not (watching and warm):
            return out

        ctx = self._context(an)
        if self._on("cascade"):
            for t in new_cascades:
                out += self._cascade(coin, t, st, now, ctx)
        if self._on("liquidation"):
            for (wallet, start), (side, usd, px) in liqs.items():
                if usd >= self.rules["liquidation"]["min_usd"]:
                    out += self._fire(Alert(
                        f"liq-{coin}-{wallet}-{start}", coin, "liquidation", now,
                        "down" if side is TradeSide.SELL else "up",
                        f"{coin}: a {fmt_usd(usd)} {'long' if side is TradeSide.SELL else 'short'} was liquidated",
                        (f"The exchange force-closed wallet {_short(wallet)} near {fmt_px(px)}.",) + ctx),
                        (coin, "liquidation", wallet))
        if self._on("twap"):
            for (wallet, side), (_, slice_usd, interval_ms) in new_twaps:
                per_hour = slice_usd * 3_600_000 / interval_ms if interval_ms else 0.0
                if per_hour >= self.rules["twap"]["min_usd_per_hour"]:
                    verb = "buying" if side is TradeSide.BUY else "selling"
                    out += self._fire(Alert(
                        f"twap-{coin}-{wallet}-{side.value}-{now}", coin, "twap", now,
                        "up" if side is TradeSide.BUY else "down",
                        f"{coin}: a wallet started {verb} about {fmt_usd(per_hour)} an hour",
                        (f"Wallet {_short(wallet)} {'buys' if side is TradeSide.BUY else 'sells'} {fmt_usd(slice_usd)} "
                         f"every {interval_ms / 1000:.0f}s (a TWAP bot), whatever the price does.",) + ctx),
                        (coin, "twap", wallet, side))
        if self._on("level"):
            for e in new_levels:
                down = e.direction.value == "down"
                where = f" {fmt_px(e.price)}" if e.price else " a defended level"
                why = (f"{'Buyers' if down else 'Sellers'} had defended it for {_mins(e.held_ms)}, soaking up "
                       f"{fmt_usd(e.amount or 0)} of {'selling' if down else 'buying'}." if e.held_ms else e.title)
                out += self._fire(Alert(
                    f"lvl-{e.id}", coin, "level", now, e.direction.value,
                    f"{coin} {'fell through' if down else 'broke above'}{where}", (why,) + ctx), (coin, "level", e.id))
        if self._on("absorbed"):
            for w in ABSORB_WINDOWS:
                ex = an.latest.get(w)
                flow = next((m for m in (ex.summary if ex else ()) if m.key == "flow"), None)
                if ex is None or flow is None or flow.partial or ex.coverage < 0.95 or flow.tag not in ABSORB_TAGS:
                    continue
                selling = "sell" in flow.value
                out += self._fire(Alert(
                    f"abs-{coin}-{w}-{now}", coin, "absorbed", now, flow.lean.value,
                    f"{coin}: heavy {'selling' if selling else 'buying'} on the {w} chart, but {ABSORB_TAGS[flow.tag]}",
                    (f"Market orders over {w}: {flow.value}. Price {_pct(ex.move.move_bps / 100)}.",
                     f"Big orders waiting in the book are soaking it up, often a sign of a patient "
                     f"{'buyer' if selling else 'seller'}.")),
                    (coin, "absorbed", w))
        if self._on("volatility"):
            ex = an.latest.get("10m")
            rr = ex.range_ratio if ex else None
            if ex is not None and rr is not None and ex.coverage >= 0.95 and rr >= self.rules["volatility"]["min_ratio"]:
                out += self._fire(Alert(
                    f"vol-{coin}-{now}", coin, "volatility", now, ex.move.direction.value,
                    f"{coin} is moving fast: the last 10 minutes covered {rr:.1f}× the normal range",
                    (f"Between {fmt_px(ex.move.low)} and {fmt_px(ex.move.high)}, now {fmt_px(ex.move.end_price)} "
                     f"({_pct(ex.move.move_bps / 100)} in 10m).",)), (coin, "volatility"))
        return out

    # ── pieces ───────────────────────────────────────────────────────────
    def _on(self, kind: str) -> bool:
        return bool(self.rules.get(kind, {}).get("on"))

    def _fire(self, alert: Alert, key: tuple) -> list[Alert]:
        last = self._last_fired.get(key)
        if last is not None and alert.ts - last < self.cooldown_ms:
            return []
        self._last_fired[key] = alert.ts
        return [alert]

    @staticmethod
    def _context(an) -> tuple[str, ...]:
        """One line of 10m context under most alerts: the move and the flow."""
        ex = an.latest.get("10m")
        if ex is None:
            return ()
        flow = next((m for m in ex.summary if m.key == "flow"), None)
        line = f"Now {fmt_px(ex.move.end_price)} ({_pct(ex.move.move_bps / 100)} in 10m)"
        if flow is not None and not flow.partial and flow.value:
            line += f". Last 10m of market orders: {flow.value}."
        return (line,)

    def _cascade(self, coin, t, st, now, ctx) -> list[Alert]:
        liquidated = sum(o.notional for o in _engine_forced(st, t.start_ms, t.end_ms + 1_000, t.side))
        size = max(liquidated, t.notional)
        if size < self.rules["cascade"]["min_usd"]:
            return []
        buy = t.side is TradeSide.BUY
        kind = "short" if buy else "long"
        verdict = {"likely": "Open interest fell, so these were real liquidations.",
                   "partly": "Open interest fell a little, so some of these were liquidations.",
                   "unlikely": "Open interest didn't fall, so this was likely one big trader, not liquidations."
                   }.get(t.verdict or "", "No open interest data to confirm liquidations.")
        dur = max(1, round((t.end_ms - t.start_ms) / 1000))
        title = (f"{coin}: {fmt_usd(liquidated)} of {kind}s liquidated in {dur}s" if liquidated >= 0.5 * t.notional
                 else f"{coin}: heavy {'buying' if buy else 'selling'}, {fmt_usd(t.notional)} in {dur}s")
        return self._fire(Alert(
            f"casc-{coin}-{t.id}", coin, "cascade", now, "up" if buy else "down", title,
            (f"{t.sweeps} big {'buy' if buy else 'sell'} orders in a row pushed price {_pct(t.move_bps / 100)}. "
             f"{verdict}",) + ctx),
            (coin, "cascade"))

    def _new_liquidations(self, st, mem: _CoinMemory, now: int) -> dict:
        """Engine-forced orders seen in the last minute, grouped per wallet, reported once per group."""
        forced = st.engine.forced
        groups: dict[tuple, list] = {}
        for o in _engine_forced(st, now - 2 * LIQ_GROUP_MS, now + 1, None):
            start = o.timestamp // LIQ_GROUP_MS * LIQ_GROUP_MS
            g = groups.setdefault((o.taker, start), [o.side, 0.0, o.last_price])
            g[1] += o.notional
            g[2] = o.last_price
        fresh = {}
        for key, (side, usd, px) in groups.items():
            if key in mem.seen_liq:
                continue
            if now - key[1] >= LIQ_GROUP_MS or usd >= self.rules["liquidation"]["min_usd"]:
                mem.seen_liq.add(key)                     # report a group once: when it's big enough, or done
                fresh[key] = (side, usd, px)
        if len(mem.seen_liq) > 5_000:
            mem.seen_liq = {k for k in mem.seen_liq if now - k[1] < 10 * LIQ_GROUP_MS}
        return fresh

    def _price(self, coin: str, prev: float | None, mid: float | None, now: int) -> list[Alert]:
        out = []
        if prev is None or mid is None or prev == mid:
            return out
        for lv in self.levels:
            if lv.coin != coin or lv.triggered_ms is not None:
                continue
            if (prev - lv.price) * (mid - lv.price) <= 0:
                up = mid > prev
                lv.triggered_ms = now
                out.append(Alert(f"px-{lv.id}", coin, "price", now, "up" if up else "down",
                                 f"{coin} crossed {'above' if up else 'below'} {fmt_px(lv.price)}",
                                 (f"Now {fmt_px(mid)}. This price alert won't fire again.",)))
        return out


def _engine_forced(st, start_ms: int, end_ms: int, side: TradeSide | None) -> list[AggressiveOrder]:
    span_s = max(1, (end_ms - start_ms) // 1000 + 1)
    forced = st.engine.forced
    return [o for o in st.orders.window(min(span_s, st.orders.max_seconds), end_ms)
            if o.engine and start_ms <= o.timestamp <= end_ms and (o.timestamp, o.taker) in forced
            and (side is None or o.side is side)]


def _pct(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.2f}%"


def _mins(ms: int | None) -> str:
    m = (ms or 0) / 60_000
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} hours"


def _short(wallet: str | None) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet and len(wallet) > 12 else (wallet or "a wallet")


def now_ms() -> int:
    return int(time.time() * 1000)

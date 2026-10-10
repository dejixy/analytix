"""
Alerts: the moments worth a ping, checked once a second for every coin.

    cascade       a chain of forced flow, once its OI check is in (≥ min liquidated or swept)
    liquidation   one account force-closed by the engine (≥ min), its pieces added up
    twap          a wallet starts slicing a big order (≥ min an hour)
    level         a defended level breaks
    absorbed      heavy one-sided flow fails to move price on the 10m or 60m card
    volatility    the 10m range is ≥ N× its usual range
    price         price crosses a level you set (one-shot)

Every alert has a cooldown key — per coin and type, or per level / wallet where each one is
its own story — so a messy hour can't send twenty pings. Nothing fires in the first five
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

KINDS = ("cascade", "liquidation", "twap", "level", "absorbed", "volatility", "price")
DEFAULT_RULES: dict[str, dict] = {
    "cascade": {"on": True, "min_usd": 1_000_000},
    "liquidation": {"on": True, "min_usd": 500_000},
    "twap": {"on": True, "min_usd_per_hour": 1_000_000},
    "level": {"on": True},
    "absorbed": {"on": True},
    "volatility": {"on": True, "min_ratio": 2.5},
    "price": {"on": True},
}
DEFAULT_COOLDOWN_MIN = 15
WARMUP_MS = 5 * 60_000
LIQ_GROUP_MS = 60_000             # a liquidation's pieces (20% chunk, then the rest) arrive within this
ABSORB_WINDOWS = ("10m", "60m")
ABSORB_TAGS = {"absorbed": "absorbed", "against flow": "moved against the flow"}


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
                        f"{coin} {'long' if side is TradeSide.SELL else 'short'} liquidated · {fmt_usd(usd)}",
                        (f"{_short(wallet)} force-closed near {fmt_px(px)}",) + ctx), (coin, "liquidation", wallet))
        if self._on("twap"):
            for (wallet, side), (_, slice_usd, interval_ms) in new_twaps:
                per_hour = slice_usd * 3_600_000 / interval_ms if interval_ms else 0.0
                if per_hour >= self.rules["twap"]["min_usd_per_hour"]:
                    verb = "buying" if side is TradeSide.BUY else "selling"
                    out += self._fire(Alert(
                        f"twap-{coin}-{wallet}-{side.value}-{now}", coin, "twap", now,
                        "up" if side is TradeSide.BUY else "down",
                        f"{coin} new TWAP {verb} ~{fmt_usd(per_hour)} an hour",
                        (f"{_short(wallet)} slicing {fmt_usd(slice_usd)} every {interval_ms / 1000:.0f}s",) + ctx),
                        (coin, "twap", wallet, side))
        if self._on("level"):
            for e in new_levels:
                out += self._fire(Alert(
                    f"lvl-{e.id}", coin, "level", now, e.direction.value, f"{coin} level broke",
                    (e.title, e.detail) + ctx), (coin, "level", e.id))
        if self._on("absorbed"):
            for w in ABSORB_WINDOWS:
                ex = an.latest.get(w)
                flow = next((m for m in (ex.summary if ex else ()) if m.key == "flow"), None)
                if ex is None or flow is None or flow.partial or ex.coverage < 0.95 or flow.tag not in ABSORB_TAGS:
                    continue
                out += self._fire(Alert(
                    f"abs-{coin}-{w}-{now}", coin, "absorbed", now, flow.lean.value,
                    f"{coin} {w}: heavy flow {ABSORB_TAGS[flow.tag]}",
                    (f"{w} flow: {flow.value}", f"price {ex.move.move_bps / 100:+.2f}% over {w}")),
                    (coin, "absorbed", w))
        if self._on("volatility"):
            ex = an.latest.get("10m")
            rr = ex.range_ratio if ex else None
            if ex is not None and rr is not None and ex.coverage >= 0.95 and rr >= self.rules["volatility"]["min_ratio"]:
                out += self._fire(Alert(
                    f"vol-{coin}-{now}", coin, "volatility", now, ex.move.direction.value,
                    f"{coin} volatility spike · 10m range {rr:.1f}× usual",
                    (f"10m range {fmt_px(ex.move.low)}–{fmt_px(ex.move.high)}, now {fmt_px(ex.move.end_price)} "
                     f"({ex.move.move_bps / 100:+.2f}%)",)), (coin, "volatility"))
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
        line = f"now {fmt_px(ex.move.end_price)} ({ex.move.move_bps / 100:+.2f}% in 10m)"
        if flow is not None and not flow.partial and flow.value:
            line += f" · {flow.value}"
        return (line,)

    def _cascade(self, coin, t, st, now, ctx) -> list[Alert]:
        liquidated = sum(o.notional for o in _engine_forced(st, t.start_ms, t.end_ms + 1_000, t.side))
        size = max(liquidated, t.notional)
        if size < self.rules["cascade"]["min_usd"]:
            return []
        buy = t.side is TradeSide.BUY
        kind = "short" if buy else "long"
        verdict = {"likely": "liquidations confirmed by OI", "partly": "partly liquidations",
                   "unlikely": "OI didn't fall: likely one trader"}.get(t.verdict or "", "OI unavailable")
        dur = max(1, round((t.end_ms - t.start_ms) / 1000))
        title = (f"{coin} {kind}-liquidation cascade · {fmt_usd(liquidated)} liquidated" if liquidated >= 0.5 * t.notional
                 else f"{coin} {'buy' if buy else 'sell'} sweep cascade · {fmt_usd(t.notional)}")
        return self._fire(Alert(
            f"casc-{coin}-{t.id}", coin, "cascade", now, "up" if buy else "down", title,
            (f"{t.sweeps} sweeps in {dur}s · price {t.move_bps / 100:+.2f}% · {verdict}",) + ctx),
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
                                 f"{coin} crossed {fmt_px(lv.price)} {'▲' if up else '▼'}",
                                 (f"now {fmt_px(mid)}",)))
        return out


def _engine_forced(st, start_ms: int, end_ms: int, side: TradeSide | None) -> list[AggressiveOrder]:
    span_s = max(1, (end_ms - start_ms) // 1000 + 1)
    forced = st.engine.forced
    return [o for o in st.orders.window(min(span_s, st.orders.max_seconds), end_ms)
            if o.engine and start_ms <= o.timestamp <= end_ms and (o.timestamp, o.taker) in forced
            and (side is None or o.side is side)]


def _short(wallet: str | None) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet and len(wallet) > 12 else (wallet or "a wallet")


def now_ms() -> int:
    return int(time.time() * 1000)

"""
Cascades, checked: were they really liquidations, and did the move stick?

A chain of same-side sweeps *looks* like liquidations, but one aggressive trader
leaves the same footprint. Open interest tells them apart: a liquidation closes
a position, so OI falls. If OI dropped by a good share of the cascade's size,
positions were force-closed; if it didn't move (or rose), it was more likely
one large trader. Not proof either way — a liquidated long can sell into a
new long's bid, leaving OI flat — so the wording stays "likely".

Then the aftermath. Forced selling that's fully absorbed tends to snap back;
selling that keeps going is real repositioning. So each cascade is followed for
15 minutes: how much of its move has price won back?

Each cascade becomes an event in the feed once its OI check is in (15s after
the last sweep, from a reading taken after the cascade), and its recovery
updates in place for the next 15 minutes.
"""
from dataclasses import dataclass

from config import CASCADE_MAX_GAP_MS
from models.explanationModel import MarketEvent
from models.signalModel import Direction
from models.tradeModel import TradeSide
from signals.base import CascadeInfo, fmt_usd
from signals.liquidations import find_cascades, is_sweep

SCAN_S = 120                    # sweeps re-scanned on each run
EXTREME_GRACE_MS = 10_000       # the cascade's extreme is its furthest price up to 10s after the last sweep
OI_SETTLE_MS = 15_000           # OI is read 15s after the last sweep (the feed updates every few seconds)
OI_GIVE_UP_MS = 60_000          # no OI reading a minute after the cascade: report it as unavailable
RECOVERY_TRACK_MS = 15 * 60_000
LIKELY_SHARE = 0.5              # OI fell by ≥ 50% of the cascade's size → likely liquidations
PARTLY_SHARE = 0.15
MAX_TRACKED = 30


@dataclass(slots=True)
class TrackedCascade:
    id: int
    side: TradeSide
    start_ms: int
    end_ms: int
    sweeps: int
    notional: float
    price_before: float
    extreme: float
    oi_before: float | None
    mark: float
    oi_after: float | None = None
    oi_settled: bool = False
    recovered: float | None = None
    last_ms: int = 0
    done: bool = False

    @property
    def move_bps(self) -> float:
        return (self.extreme / self.price_before - 1) * 10_000 if self.price_before else 0.0

    @property
    def oi_change_usd(self) -> float | None:
        if not self.oi_settled or self.oi_before is None or self.oi_after is None:
            return None
        return (self.oi_after - self.oi_before) * self.mark

    @property
    def confirm_share(self) -> float | None:
        d = self.oi_change_usd
        return None if d is None or self.notional <= 0 else -d / self.notional

    @property
    def verdict(self) -> str | None:
        share = self.confirm_share
        if share is None:
            return None
        return "likely" if share >= LIKELY_SHARE else "partly" if share >= PARTLY_SHARE else "unlikely"

    def info(self, now_ms: int) -> CascadeInfo:
        return CascadeInfo(side=self.side, start_ms=self.start_ms, end_ms=self.end_ms, oi_settled=self.oi_settled,
                           oi_change_usd=self.oi_change_usd, confirm_share=self.confirm_share,
                           verdict=self.verdict, move_bps=self.move_bps, recovered=self.recovered,
                           since_end_s=max(0.0, (now_ms - self.end_ms) / 1000))


def oi_sentence(c: CascadeInfo) -> str:
    if c.verdict is None:
        return "Open interest wasn't available around it." if c.oi_settled else "Checking open interest…"
    d = c.oi_change_usd or 0.0
    if c.verdict == "likely":
        return (f"Open interest fell {fmt_usd(-d)} across it (≈{c.confirm_share:.0%} of the cascade): "
                f"positions were force-closed — likely liquidations.")
    if c.verdict == "partly":
        return f"Open interest fell {fmt_usd(-d)} (≈{c.confirm_share:.0%} of the cascade): partly forced closes."
    moved = f"rose {fmt_usd(d)}" if d > 0 else "barely moved"
    return f"Open interest {moved} — more likely one large trader than liquidations."


def recovery_text(c: CascadeInfo, long_form: bool = True) -> str:
    if c.recovered is None:
        return ""
    when = _ago(c.since_end_s)
    r = c.recovered
    if r >= 0.95:
        short = "fully recovered"
    elif r <= 0.02:
        short = "no recovery" if r > -0.25 else "kept going"
    else:
        short = f"won back {r:.0%}"
    if not long_form:
        return f"{short} in {when}" if short != "kept going" else f"kept going past the cascade's {'low' if c.move_bps < 0 else 'high'}"
    if short == "kept going":
        return f"Since then price has kept going past the cascade's {'low' if c.move_bps < 0 else 'high'}."
    if short == "no recovery":
        return f"Since then price hasn't recovered any of the {c.move_bps / 100:+.2f}% ({when})."
    if short == "fully recovered":
        return f"Since then price has fully recovered the {c.move_bps / 100:+.2f}% ({when})."
    return f"Since then price has won back {r:.0%} of the {c.move_bps / 100:+.2f}% move ({when})."


def _ago(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f}m"


class CascadeTracker:
    def __init__(self, coin: str):
        self.coin = coin
        self._items: list[TrackedCascade] = []
        self._events: dict[int, MarketEvent] = {}
        self._next = 1

    def update(self, st, sweep_threshold: float, now_ms: int) -> None:
        twaps = st.engine.twaps
        sweeps = [o for o in st.orders.window(SCAN_S, now_ms) if is_sweep(o, sweep_threshold, twaps)]
        for c in find_cascades(sweeps):
            t = next((t for t in self._items if t.side is c.side and c.start_ms <= t.end_ms + CASCADE_MAX_GAP_MS
                      and c.end_ms >= t.start_ms), None)
            if t:
                t.end_ms = max(t.end_ms, c.end_ms)
                if c.start_ms <= t.start_ms:
                    t.start_ms, t.sweeps, t.notional = c.start_ms, c.sweeps, c.notional
                else:                                   # the scan window cut off its start: keep the larger tally
                    t.sweeps, t.notional = max(t.sweeps, c.sweeps), max(t.notional, c.notional)
                continue
            before = st.books.latest_at(c.start_ms - 1) or st.books.oldest
            ctx = st.contexts.latest_at(c.start_ms - 1)
            if before is None:
                continue
            self._items.append(TrackedCascade(
                self._next, c.side, c.start_ms, c.end_ms, c.sweeps, c.notional, before.mid, before.mid,
                ctx.open_interest if ctx else None, ctx.mark_price if ctx else before.mid))
            self._next += 1
        self._items = self._items[-MAX_TRACKED:]

        mid = st.mid
        for t in self._items:
            if t.done:
                continue
            ext_end = min(now_ms, t.end_ms + EXTREME_GRACE_MS)
            span_s = min(st.books.max_seconds, max(1, (ext_end - t.start_ms) // 1000 + 1))
            path = [b.mid for b in st.books.window(span_s, ext_end) if b.timestamp >= t.start_ms]
            if path:
                t.extreme = min(path + [t.extreme]) if t.side is TradeSide.SELL else max(path + [t.extreme])
            if not t.oi_settled and now_ms >= t.end_ms + OI_SETTLE_MS:
                ctx = st.contexts.latest_at(now_ms)
                if ctx and ctx.timestamp > t.end_ms:            # a reading taken after the cascade
                    t.oi_after = ctx.open_interest
                    t.oi_settled = True
                elif now_ms >= t.end_ms + OI_GIVE_UP_MS:        # no fresh OI: say so rather than guess
                    t.oi_settled = True
            span = t.price_before - t.extreme
            if mid and now_ms > t.end_ms + EXTREME_GRACE_MS and abs(span) > 0:
                t.recovered = (mid - t.extreme) / span
            t.last_ms = now_ms
            if now_ms - t.end_ms > RECOVERY_TRACK_MS:
                t.done = True
            if t.oi_settled:                                 # over and OI checked: publish / refresh its event
                self._events[t.id] = self._event(t, now_ms)
        while len(self._events) > MAX_TRACKED:
            del self._events[min(self._events)]

    def _event(self, t: TrackedCascade, now_ms: int) -> MarketEvent:
        info = t.info(now_ms)
        buy = t.side is TradeSide.BUY
        dur = max(1, round((t.end_ms - t.start_ms) / 1000))
        verdict = {"likely": "likely liquidations", "partly": "partly liquidations",
                   "unlikely": "likely one trader, not liquidations"}.get(t.verdict or "", "OI unavailable")
        oi = t.oi_change_usd
        oi_txt = f"OI {'+' if (oi or 0) >= 0 else '−'}{fmt_usd(abs(oi))}: " if oi is not None else ""
        rec = recovery_text(info, long_form=False)
        return MarketEvent(
            id=f"cascade-{self.coin}-{t.id}", kind="cascade", ts=t.start_ms,
            direction=Direction.UP if buy else Direction.DOWN,
            title=(f"{t.sweeps} {'buy' if buy else 'sell'} sweeps in {dur}s ({fmt_usd(t.notional)}) "
                   f"pushed price {t.move_bps / 100:+.2f}%"),
            detail=oi_txt + verdict + (f" · {rec}" if rec else ""),
            stat=f"{t.move_bps / 100:+.2f}%",
        )

    def infos(self, now_ms: int) -> list[CascadeInfo]:
        return [t.info(now_ms) for t in self._items]

    @property
    def events(self) -> list[MarketEvent]:
        return list(self._events.values())

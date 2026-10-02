"""
Forced flow: large sweeps and liquidation-style cascades.

Honest caveat — Hyperliquid's public feed does not label liquidations. A
market liquidation executes as an ordinary taker order from the liquidated
account. So this is a *heuristic*:

  1. Fills are stitched into taker orders on arrival (models/orderModel.py):
     fills from one order share the L1 tx hash, side and timestamp.
  2. A "sweep" is an order that is large versus recent history, or walks
     several price levels.
  3. A "cascade" is several same-side sweeps chained within a few seconds —
     the footprint of liquidations triggering more liquidations.

If you know liquidator addresses, set ANALYTIX_LIQUIDATORS and trades touching
them are reported as confirmed liquidations.
"""
from dataclasses import dataclass

from config import CASCADE_MAX_GAP_MS, CASCADE_MIN_SWEEPS
from models.orderModel import AggressiveOrder, is_sweep  # noqa: F401  (re-exported)
from models.signalModel import Direction, SignalResult
from models.tradeModel import TradeSide
from signals.base import CascadeInfo, WindowSlice, clip, fmt_usd

NAME, LABEL = "liquidations", "Forced flow"


@dataclass(frozen=True, slots=True)
class Cascade:
    side: TradeSide
    start_ms: int
    end_ms: int
    sweeps: int
    notional: float

    @property
    def duration_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000


def find_cascades(sweeps: list[AggressiveOrder]) -> list[Cascade]:
    cascades: list[Cascade] = []
    chain: list[AggressiveOrder] = []

    def close() -> None:
        if len(chain) >= CASCADE_MIN_SWEEPS:
            cascades.append(Cascade(chain[0].side, chain[0].timestamp, chain[-1].timestamp,
                                    len(chain), sum(o.notional for o in chain)))

    for o in sweeps:
        if chain and (o.side != chain[-1].side or o.timestamp - chain[-1].timestamp > CASCADE_MAX_GAP_MS):
            close()
            chain = []
        chain.append(o)
    close()
    return cascades


def _tracked(s: WindowSlice, c: Cascade) -> CascadeInfo | None:
    """The tracker's record of this cascade (OI check, recovery), if it has one."""
    return next((i for i in s.cascades if i.side is c.side and i.start_ms <= c.end_ms and i.end_ms >= c.start_ms), None)


def _from_bars(s: WindowSlice) -> SignalResult:
    """Long windows: sweeps were counted per minute bar as they happened; no cascade chaining."""
    a = s.sweep_agg
    if a is None or a.total_notional <= 0:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No live flow in this window yet.", stat="—")
    metrics = {"sweeps_buy": float(a.buy_n), "sweeps_sell": float(a.sell_n),
               "sweep_notional_buy": a.buy_notional, "sweep_notional_sell": a.sell_notional,
               "cascades": 0.0, "confirmed_liquidations": 0.0, "largest_order": 0.0,
               "sweep_threshold": s.baseline.sweep_threshold}
    if a.buy_n + a.sell_n == 0:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL,
                            "No sweeps in the live part of this window; flow was ordinary size.",
                            stat="no sweeps", metrics=metrics)
    score = clip((a.buy_notional - a.sell_notional) / (a.total_notional * 0.25))
    if s.flow_coverage < 0.95:
        score *= clip(s.flow_coverage * 2, 0.2, 1.0)
    side_word = "buy" if score >= 0 else "sell"
    n = a.buy_n if score >= 0 else a.sell_n
    summary = (f"{a.buy_n} buy sweeps ({fmt_usd(a.buy_notional)}) vs {a.sell_n} sell sweeps "
               f"({fmt_usd(a.sell_notional)}) in the live part of this window.")
    return SignalResult(NAME, LABEL, score, abs(score), Direction.of(score, eps=0.05), summary,
                        phrase=f"large {side_word} sweeps ({n}, {fmt_usd(a.buy_notional if score >= 0 else a.sell_notional)})",
                        stat=f"{n} {side_word} sweep{'' if n == 1 else 's'}", metrics=metrics)


def liquidations(s: WindowSlice) -> SignalResult:
    from engine.cascades import oi_sentence, recovery_text  # noqa: F401 — engine.cascades imports this module
    if s.resolution == "bar":
        return _from_bars(s)
    orders = s.orders
    total = sum(o.notional for o in orders)
    if total <= 0:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL, "No trades in this window.", stat="—")

    sweeps = [o for o in orders if is_sweep(o, s.baseline.sweep_threshold)]
    buy_sw = [o for o in sweeps if o.side is TradeSide.BUY]
    sell_sw = [o for o in sweeps if o.side is TradeSide.SELL]
    buy_n, sell_n = sum(o.notional for o in buy_sw), sum(o.notional for o in sell_sw)
    cascades = find_cascades(sweeps)
    confirmed = sum(o.notional for o in orders if o.confirmed_liquidation)

    # Net sweep flow as a share of all taker flow; 25% of volume in one-sided sweeps = full strength.
    score = clip((buy_n - sell_n) / (total * 0.25))
    strength = abs(score)
    biggest = max(cascades, key=lambda c: c.notional, default=None)
    if biggest:
        cascade_dir = 1.0 if biggest.side is TradeSide.BUY else -1.0
        if cascade_dir * score >= 0:
            strength = max(strength, clip(0.55 + 0.05 * biggest.sweeps, 0.0, 1.0))
            score = cascade_dir * strength

    metrics = {
        "sweeps_buy": float(len(buy_sw)),
        "sweeps_sell": float(len(sell_sw)),
        "sweep_notional_buy": buy_n,
        "sweep_notional_sell": sell_n,
        "cascades": float(len(cascades)),
        "confirmed_liquidations": confirmed,
        "largest_order": max((o.notional for o in orders), default=0.0),
        "sweep_threshold": s.baseline.sweep_threshold,
    }

    if not sweeps:
        return SignalResult(NAME, LABEL, 0.0, 0.0, Direction.NEUTRAL,
                            f"No sweeps above {fmt_usd(s.baseline.sweep_threshold)}; flow was ordinary size.",
                            stat="no sweeps", metrics=metrics)

    if biggest:
        kind = "short" if biggest.side is TradeSide.BUY else "long"
        side_word = "buy" if biggest.side is TradeSide.BUY else "sell"
        info = _tracked(s, biggest)
        size = f"{biggest.sweeps} sweeps, {fmt_usd(biggest.notional)}"
        if info and info.verdict == "likely":
            phrase = f"a {kind}-liquidation cascade ({size}; OI −{fmt_usd(-(info.oi_change_usd or 0))})"
        elif info and info.verdict == "unlikely":
            phrase = f"a {side_word}-sweep cascade ({size}; OI didn't fall)"
        else:
            phrase = f"a {kind}-liquidation-style cascade ({size})"
        stat = f"{biggest.sweeps}-sweep cascade"   # side is carried by the bar colour and the phrase
        summary = (
            f"{biggest.sweeps} {'buy' if biggest.side is TradeSide.BUY else 'sell'} sweeps chained within "
            f"{max(biggest.duration_s, 1):.0f}s ({fmt_usd(biggest.notional)}) — the footprint of forced "
            f"{kind} exits (heuristic: the public feed doesn't label liquidations)."
        )
        if info:
            summary += " " + oi_sentence(info)
            rec = recovery_text(info)
            if rec:
                summary += " " + rec
    else:
        side_word = "buy" if score >= 0 else "sell"
        n = len(buy_sw) if score >= 0 else len(sell_sw)
        phrase = f"large {side_word} sweeps ({n}, {fmt_usd(buy_n if score >= 0 else sell_n)})"
        stat = f"{n} {side_word} sweep{'' if n == 1 else 's'}"
        summary = (
            f"{len(buy_sw)} buy sweeps ({fmt_usd(buy_n)}) vs {len(sell_sw)} sell sweeps ({fmt_usd(sell_n)}) "
            f"above {fmt_usd(s.baseline.sweep_threshold)}."
        )
    if confirmed:
        phrase = f"confirmed liquidations ({fmt_usd(confirmed)})"
        stat = f"{fmt_usd(confirmed)} liquidated"
        summary += f" {fmt_usd(confirmed)} matched known liquidator addresses."

    return SignalResult(NAME, LABEL, score, strength, Direction.of(score, eps=0.05),
                        summary, phrase=phrase, stat=stat, metrics=metrics)

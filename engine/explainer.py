"""
Turns a price move + signal results into a ranked, plain-language explanation.

Logic, in order:
  1. Classify every signal against the move: supports / opposes / neutral.
  2. Rank supporters by weighted strength → headline names the top two.
     Weights depend on the horizon: sweeps matter most for 1m, positioning
     for 60m, so the same data yields a different thesis per timeframe.
  3. Strong opposers become "headwinds". Aggressive flow that *lost* (sellers
     hammering while price rose) is absorption, worth calling out by name.
  4. Confidence = how one-sided the evidence is.
  5. For windows longer than a minute, describe the move's shape: one sharp
     minute (a shock) or spread out (a grind / trend).
"""
from config import CROWDED_FUNDING_APR, MIN_DRIVER_STRENGTH, SIGNIFICANT_Z
from models.explanationModel import Alignment, Driver, Explanation, FlowImpact, PriceMove, Significance
from models.signalModel import Direction, SignalResult
from signals import weights_for
from signals.base import WindowSlice, fmt_px, fmt_usd
from models.tradeModel import TradeSide
from engine.levels import defended_level, level_sentence

ABSORPTION_STRENGTH = 0.5
IMPACT_ABSORPTION_STRENGTH = 0.3
HEADWIND_STRENGTH = 0.4
FADE_SHARE = 1.5     # one stretch moved ≥1.5× the net move → it spiked and mostly gave it back
BURST_SHARE = 0.6    # one minute delivered ≥60% of the net move → a shock
GRIND_SHARE = 0.4    # no minute delivered more than 40% → a grind


def span_words(seconds: int) -> str:
    """60 → 'minute', 1800 → '30-minute stretch', 3600 → 'hour', 50400 → '14-hour stretch'."""
    if seconds <= 60:
        return "minute"
    if seconds == 3600:
        return "hour"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}-hour stretch"
    return f"{seconds // 60}-minute stretch"


def _duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _align(sig: SignalResult, move: PriceMove) -> Alignment:
    if sig.strength < MIN_DRIVER_STRENGTH or move.significance is Significance.QUIET:
        return Alignment.NEUTRAL
    if sig.direction is Direction.NEUTRAL or move.direction is Direction.NEUTRAL:
        return Alignment.NEUTRAL
    return Alignment.SUPPORTS if sig.direction is move.direction else Alignment.OPPOSES


def _absorption_sentence(flow: SignalResult, move: PriceMove) -> str:
    aggressor = "Sellers" if flow.score < 0 else "Buyers"
    passive = "buy orders" if flow.score < 0 else "sell orders"
    pct = max(flow.metrics.get("buy_notional", 0), flow.metrics.get("sell_notional", 0))
    total = flow.metrics.get("buy_notional", 0) + flow.metrics.get("sell_notional", 0)
    share = pct / total * 100 if total else 0
    outcome = {
        Direction.UP: "price still rose",
        Direction.DOWN: "price still fell",
        Direction.NEUTRAL: "price barely moved",
    }[move.direction if move.significance is not Significance.QUIET else Direction.NEUTRAL]
    return (
        f"Absorbed: {aggressor.lower()} made up {share:.0f}% of market orders, yet {outcome}. {passive.capitalize()} "
        f"waiting in the book soaked it all up, often a sign of a patient big player on the other side."
    )


def _impact_sentence(label: str, im: FlowImpact) -> str:
    buying = im.net_flow > 0
    flow = "buying" if buying else "selling"
    head = (f"Impact: net {flow} of {fmt_usd(abs(im.net_flow))} usually moves price "
            f"about {im.expected_bps / 100:+.2f}% over {label}. It moved {im.actual_bps / 100:+.2f}%")
    return head + {
        "against": f", the other way, so orders waiting in the book soaked up all the {flow}.",
        "absorbed": f", only {im.ratio:.1f}× the usual, so orders waiting in the book soaked up most of the {flow}.",
        "normal": f", about the usual ({im.ratio:.1f}×).",
        "outsized": f", {im.ratio:.1f}× the usual: a thin book, or a move that started on another exchange.",
    }[im.verdict]


def _level(s: WindowSlice, flow: SignalResult):
    """The price passive orders defended against the aggressors (tick windows only: bars carry no fill prices)."""
    if not s.trades:
        return None
    lv = defended_level(s.trades, TradeSide.SELL if flow.score < 0 else TradeSide.BUY)
    return lv if lv and lv.absorbed >= s.baseline.sweep_threshold else None


def explain(coin: str, s: WindowSlice, move: PriceMove, signals: list[SignalResult],
            impact: FlowImpact | None = None) -> Explanation:
    by_name = {sig.name: sig for sig in signals}
    weights = weights_for(s.seconds)
    aligned = [(sig, _align(sig, move), weights.get(sig.name, 1.0) * sig.strength) for sig in signals]
    supporters = sorted([a for a in aligned if a[1] is Alignment.SUPPORTS], key=lambda a: -a[2])
    opposers = sorted([a for a in aligned if a[1] is Alignment.OPPOSES], key=lambda a: -a[2])
    sup_total = sum(w for _, _, w in supporters)
    opp_total = sum(w for _, _, w in opposers)

    drivers = [
        Driver(
            name=sig.name,
            label=sig.label,
            alignment=al,
            strength=round(sig.strength, 3),
            share=round(w / sup_total, 3) if al is Alignment.SUPPORTS and sup_total else 0.0,
            summary=sig.summary,
        )
        for sig, al, w in sorted(aligned, key=lambda a: (a[1] is not Alignment.SUPPORTS, -a[2]))
    ]

    pct = f"{move.move_pct:+.2f}%"
    level = None
    flow = by_name.get("volume_imbalance")
    quiet = move.significance is Significance.QUIET
    narrative: list[str] = []

    if quiet:
        range_bps = (move.high / move.low - 1) * 10_000 if move.low else 0.0
        absorbed = bool(flow and flow.strength >= ABSORPTION_STRENGTH and (
            abs(move.z) < 1.0 or (move.direction is not Direction.NEUTRAL and flow.direction is not move.direction)))
        # Measured impact is the sharper test: plenty of flow, far less movement than it normally buys.
        absorbed = absorbed or bool(flow and impact and impact.verdict in ("against", "absorbed")
                                    and flow.strength >= IMPACT_ABSORPTION_STRENGTH)
        if range_bps >= SIGNIFICANT_Z * move.expected_bps:
            headline = f"{coin} little changed over {s.label} ({pct}) after a {range_bps / 100:.2f}% round trip"
            narrative.append(
                f"Price swung between {move.low:,.2f} and {move.high:,.2f} and ended about where it started. The "
                f"shorter timeframes and the event log show each swing."
            )
        elif absorbed:
            side = "sellers" if flow.score < 0 else "buyers"
            passive = "buyers" if flow.score < 0 else "sellers"
            level = _level(s, flow)
            at = f" at {fmt_px(level.price)}" if level else ""
            headline = f"{coin} little changed over {s.label} ({pct}): {side} pushed, but {passive} held{at}"
            narrative.append(_absorption_sentence(flow, move))
            if level:
                narrative.append(level_sentence(level))
        else:
            headline = f"{coin} little changed over {s.label} ({pct}): buying and selling balanced"
        narrative.append(
            f"That's within normal noise: {abs(move.z):.1f}× a typical {s.label} move of "
            f"±{move.expected_bps / 100:.2f}%."
        )
        confidence = 0.0
    shape = ""
    if not quiet and s.seconds > 60 and move.burst_bps:
        share = move.burst_share
        shape = ("faded" if share >= FADE_SHARE else "burst" if share >= BURST_SHARE
                 else "grind" if share <= GRIND_SHARE else "mixed")

    if not quiet:
        top = [sig for sig, _, _ in supporters[:2]]
        flow_opposes = any(sig.name == "volume_imbalance" and sig.strength >= HEADWIND_STRENGTH
                           for sig, _, _ in opposers)
        verb = "rose" if move.direction is Direction.UP else "fell"
        words = span_words(move.burst_span_s)
        lead = {"burst": f"one sharp {words}, ", "grind": "a steady grind, ", "faded": "a spike that mostly faded, ",
                "mixed": ""}.get(shape, "")
        if top:
            reasons = " and ".join(sig.phrase for sig in top)
            headline = f"{coin} {pct} in {s.label}: {lead}driven by {reasons}"
        elif flow_opposes and flow:
            level = _level(s, flow)
            at = f" at {fmt_px(level.price)}" if level else ""
            headline = f"{coin} {pct} in {s.label}: {verb} despite {flow.phrase}, as orders waiting in the book soaked it up{at}"
        elif flow and flow.metrics.get("activity", 1.0) < 0.8:
            headline = f"{coin} {pct} in {s.label}: no clear driver, price drifted on light trading"
        else:
            headline = f"{coin} {pct} in {s.label}: no single driver, with buyers and sellers both active"
        narrative.append(
            f"A {move.significance.value} move: {abs(move.z):.1f}× a typical {s.label} move "
            f"(±{move.expected_bps / 100:.2f}%). Range {move.low:,.2f} to {move.high:,.2f}."
        )
        if shape == "faded":
            narrative.append(
                f"Shape: the sharpest {words} moved {move.burst_bps / 100:+.2f}%, but most of it was given back, "
                f"leaving only {pct}."
            )
        elif shape == "burst":
            narrative.append(
                f"Shape: {move.burst_bps / 100:+.2f}% of the {pct} came in a single {words}. A sudden jolt, "
                f"not a trend."
            )
        elif shape == "grind":
            narrative.append(
                f"Shape: spread out. The sharpest {words} was only {move.burst_bps / 100:+.2f}% of the {pct}, "
                f"so this is a steady trend, not a one-off jolt."
            )
        narrative += [sig.summary for sig, _, _ in supporters[:3]]
        for sig, _, _ in opposers:
            if sig.strength < HEADWIND_STRENGTH:
                continue
            if sig.name == "volume_imbalance":
                narrative.append(_absorption_sentence(sig, move))
                level = level or _level(s, sig)
                if level:
                    narrative.append(level_sentence(level))
            else:
                narrative.append(f"Pushing the other way ({sig.label.lower()}): {sig.summary}")
        confidence = sup_total / (sup_total + opp_total + 0.35) if sup_total else 0.0

    if impact:
        narrative.append(_impact_sentence(s.label, impact))

    fund = by_name.get("funding")
    fund_relevant = fund and (fund.strength >= MIN_DRIVER_STRENGTH
                              or abs(fund.metrics.get("funding_apr", 0.0)) >= CROWDED_FUNDING_APR)
    if fund_relevant and all(fund is not sig for sig, _, _ in supporters[:3]):
        narrative.append(f"Positioning: {fund.summary}")
    if s.coverage < 0.95:
        have = s.seconds * s.coverage
        narrative.append(f"Warming up: {_duration(have)} of {_duration(s.seconds)} of history so far.")
    if s.resolution == "bar" and s.flow_coverage < 0.95:
        narrative.append(
            f"Trades, the order book and open interest only cover the last {_duration(s.seconds * s.flow_coverage)} "
            f"of this window. They're recorded live, and Hyperliquid only provides past prices."
        )

    return Explanation(
        window=s.label,
        seconds=s.seconds,
        start_ms=s.start_ms,
        end_ms=s.end_ms,
        coverage=round(s.coverage, 3),
        move=move,
        headline=headline,
        narrative=narrative,
        drivers=drivers,
        confidence=round(confidence, 3),
        signals=by_name,
        shape=shape,
        shape_label={"burst": f"one sharp {span_words(move.burst_span_s)}", "grind": "steady grind",
                     "faded": "spike, mostly faded", "mixed": "mixed shape"}.get(shape, ""),
        flow_coverage=round(s.flow_coverage, 3),
        impact=impact,
        level=level,
    )

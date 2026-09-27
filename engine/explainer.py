"""
Turns a price move + signal results into a ranked, plain-language explanation.

Logic, in order:
  1. Classify every signal against the move: supports / opposes / neutral.
  2. Rank supporters by weighted strength → headline names the top two.
     Weights depend on the horizon: sweeps matter most for 1m, positioning
     for 15m — so the same data yields a different thesis per timeframe.
  3. Strong opposers become "headwinds". Aggressive flow that *lost* (sellers
     hammering while price rose) is absorption — worth calling out by name.
  4. Confidence = how one-sided the evidence is.
  5. For windows longer than a minute, describe the move's shape: one sharp
     minute (a shock) or spread out (a grind / trend).
"""
from config import CROWDED_FUNDING_APR, MIN_DRIVER_STRENGTH, SIGNIFICANT_Z
from models.explanationModel import Alignment, Driver, Explanation, PriceMove, Significance
from models.signalModel import Direction, SignalResult
from signals import weights_for
from signals.base import WindowSlice

ABSORPTION_STRENGTH = 0.5
HEADWIND_STRENGTH = 0.4
BURST_SHARE = 0.6    # one minute delivered ≥60% of the net move → a shock
GRIND_SHARE = 0.4    # no minute delivered more than 40% → a grind


def _align(sig: SignalResult, move: PriceMove) -> Alignment:
    if sig.strength < MIN_DRIVER_STRENGTH or move.significance is Significance.QUIET:
        return Alignment.NEUTRAL
    if sig.direction is Direction.NEUTRAL or move.direction is Direction.NEUTRAL:
        return Alignment.NEUTRAL
    return Alignment.SUPPORTS if sig.direction is move.direction else Alignment.OPPOSES


def _absorption_sentence(flow: SignalResult, move: PriceMove) -> str:
    aggressor = "Sellers" if flow.score < 0 else "Buyers"
    passive = "bids" if flow.score < 0 else "offers"
    pct = max(flow.metrics.get("buy_notional", 0), flow.metrics.get("sell_notional", 0))
    total = flow.metrics.get("buy_notional", 0) + flow.metrics.get("sell_notional", 0)
    share = pct / total * 100 if total else 0
    outcome = {
        Direction.UP: "price still rose",
        Direction.DOWN: "price still fell",
        Direction.NEUTRAL: "price barely moved",
    }[move.direction if move.significance is not Significance.QUIET else Direction.NEUTRAL]
    return (
        f"Absorption: {aggressor.lower()} took {share:.0f}% of taker volume yet {outcome} — "
        f"resting {passive} soaked up the flow, often the sign of a patient counterparty."
    )


def explain(coin: str, s: WindowSlice, move: PriceMove, signals: list[SignalResult]) -> Explanation:
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
    flow = by_name.get("volume_imbalance")
    quiet = move.significance is Significance.QUIET
    narrative: list[str] = []

    if quiet:
        range_bps = (move.high / move.low - 1) * 10_000 if move.low else 0.0
        absorbed = bool(flow and flow.strength >= ABSORPTION_STRENGTH and (
            abs(move.z) < 1.0 or (move.direction is not Direction.NEUTRAL and flow.direction is not move.direction)))
        if range_bps >= SIGNIFICANT_Z * move.expected_bps:
            headline = f"{coin} little changed over {s.label} ({pct}) after a {range_bps / 100:.2f}% round trip"
            narrative.append(
                f"Two-way swing between {move.low:,.2f} and {move.high:,.2f} — the shorter windows and the "
                f"event log show each leg."
            )
        elif absorbed:
            side = "sellers" if flow.score < 0 else "buyers"
            passive = "bids" if flow.score < 0 else "offers"
            headline = f"{coin} little changed over {s.label} ({pct}) — {side} pressed but {passive} absorbed it"
            narrative.append(_absorption_sentence(flow, move))
        else:
            headline = f"{coin} little changed over {s.label} ({pct}) — balanced two-way flow"
        narrative.append(
            f"The net move is within normal noise ({abs(move.z):.1f}σ; a typical {s.label} move is "
            f"±{move.expected_bps:.0f} bps)."
        )
        confidence = 0.0
    shape = ""
    if not quiet and s.seconds > 60 and move.burst_bps:
        share = move.burst_share
        shape = "burst" if share >= BURST_SHARE else "grind" if share <= GRIND_SHARE else "mixed"

    if not quiet:
        top = [sig for sig, _, _ in supporters[:2]]
        flow_opposes = any(sig.name == "volume_imbalance" and sig.strength >= HEADWIND_STRENGTH
                           for sig, _, _ in opposers)
        verb = "rose" if move.direction is Direction.UP else "fell"
        lead = {"burst": "one sharp minute, ", "grind": "a steady grind, ", "mixed": ""}.get(shape, "")
        if top:
            reasons = " and ".join(sig.phrase for sig in top)
            headline = f"{coin} {pct} in {s.label} — {lead}driven by {reasons}"
        elif flow_opposes and flow:
            headline = f"{coin} {pct} in {s.label} — {verb} despite {flow.phrase}: passive liquidity absorbed it"
        elif flow and flow.metrics.get("activity", 1.0) < 0.8:
            headline = f"{coin} {pct} in {s.label} — no clear driver; price drifted on light activity"
        else:
            headline = f"{coin} {pct} in {s.label} — no single dominant driver; two-way flow"
        narrative.append(
            f"A {move.significance.value} move: {abs(move.z):.1f}× the typical {s.label} move "
            f"(±{move.expected_bps:.0f} bps), range {move.low:,.2f}–{move.high:,.2f}."
        )
        if shape == "burst":
            narrative.append(
                f"Shape: {move.burst_bps / 100:+.2f}% of the {pct} came in a single minute — a shock, not a trend."
            )
        elif shape == "grind":
            narrative.append(
                f"Shape: spread out — the sharpest minute was only {move.burst_bps / 100:+.2f}% of the {pct}, "
                f"so this is a trend, not a one-off shock."
            )
        narrative += [sig.summary for sig, _, _ in supporters[:3]]
        for sig, _, _ in opposers:
            if sig.strength < HEADWIND_STRENGTH:
                continue
            if sig.name == "volume_imbalance":
                narrative.append(_absorption_sentence(sig, move))
            else:
                narrative.append(f"Headwind ({sig.label.lower()}): {sig.summary}")
        confidence = sup_total / (sup_total + opp_total + 0.35) if sup_total else 0.0

    fund = by_name.get("funding")
    fund_relevant = fund and (fund.strength >= MIN_DRIVER_STRENGTH
                              or abs(fund.metrics.get("funding_apr", 0.0)) >= CROWDED_FUNDING_APR)
    if fund_relevant and all(fund is not sig for sig, _, _ in supporters[:3]):
        narrative.append(f"Positioning: {fund.summary}")
    if s.coverage < 0.95:
        have = s.seconds * s.coverage
        narrative.append(f"Warming up — {have / 60:.1f} of {s.seconds / 60:.0f} minutes of history so far.")

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
    )

"""
Next-period outlook: a lean, an expected move and a typical range for the next
window-length of time ("what's likely over the next 1m / 10m / 6h?").

Honest framing, because it matters: short-horizon direction is mostly noise.
The evidence used here — book imbalance, who is aggressive, sweeps, momentum,
crowded funding and absorption — has a small edge at best. So the estimate is
deliberately conservative: the evidence shifts the centre of the range by at
most SKILL × σ, which caps the model's own odds at about 60%.

Only strong agreement earns the word "lean" (model odds ≥ ~55%). Anything
weaker is a "coin flip", with the small tilt shown in passing. Once enough
independent periods of leans have been scored, the odds shown are the leans'
real hit rate ("earned") instead of the model's estimate ("est.").
"""
import math

from engine.outlookTracker import MIN_PERIODS
from models.explanationModel import Explanation, Outlook, Significance, TrackRecord
from models.signalModel import Direction
from signals.base import clip

SKILL = 0.25            # at full conviction the centre moves a quarter of a typical move
STRONG_SCORE = 0.5      # evidence agreement needed to call a lean: model odds Φ(0.25 × 0.5) ≈ 55%
NO_TILT = 0.03          # below this there isn't even a tilt worth mentioning
PRIOR_WEIGHT = 0.25     # pulls the score toward neutral when little evidence is present
BASE_FUNDING_APR = 11.0 # Hyperliquid's resting funding (~0.00125%/h); crowding is judged against it

# How much each piece of evidence counts, by horizon. Microstructure (book,
# sweeps) matters for the next minute; positioning (crowded funding) for the
# next hours. The score is averaged over the evidence that is actually present
# (plus a small neutral prior), so a quiet funding rate doesn't silence the
# book and flow on long horizons.
WEIGHTS = {
    "short": {"book": 0.35, "flow": 0.25, "sweeps": 0.15, "momentum": 0.10, "crowding": 0.00, "absorption": 0.15},
    "medium": {"book": 0.20, "flow": 0.25, "sweeps": 0.10, "momentum": 0.10, "crowding": 0.15, "absorption": 0.20},
    "long": {"book": 0.10, "flow": 0.20, "sweeps": 0.05, "momentum": 0.10, "crowding": 0.35, "absorption": 0.20},
    "very_long": {"book": 0.05, "flow": 0.15, "sweeps": 0.05, "momentum": 0.10, "crowding": 0.50, "absorption": 0.15},
}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def components(ex: Explanation, book_imbalance: float | None) -> dict[str, tuple[float, str]]:
    """Each piece of evidence as (value in -1..+1, short reason). Positive = points up."""
    sig = ex.signals
    out: dict[str, tuple[float, str]] = {}

    if book_imbalance is not None:
        side = "bid" if book_imbalance > 0 else "ask"
        out["book"] = (clip(book_imbalance / 0.5), f"book {abs(book_imbalance):.0%} {side}-heavy")

    flow = sig.get("volume_imbalance")
    if flow and flow.strength > 0:
        imb = flow.metrics.get("imbalance", 0.0)
        buy, sell = flow.metrics.get("buy_notional", 0.0), flow.metrics.get("sell_notional", 0.0)
        share = (buy if imb >= 0 else sell) / (buy + sell) if buy + sell else 0.5
        # Flow seen over only part of a long window says less about it.
        cov = clip(ex.flow_coverage * 2, 0.2, 1.0)
        out["flow"] = (clip(imb / 0.4) * cov, f"{'buyers' if imb >= 0 else 'sellers'} took {share:.0%} of flow")

        opposite = ex.move.direction is not Direction.NEUTRAL and flow.direction is not ex.move.direction
        if flow.strength >= 0.5 and (ex.move.significance is Significance.QUIET or opposite):
            # Aggression that failed to move price: the passive side is winning.
            up = flow.score < 0
            out["absorption"] = (0.8 if up else -0.8,
                                 "sellers being absorbed by bids" if up else "buyers being absorbed by offers")

    liq = sig.get("liquidations")
    if liq and liq.strength > 0:
        out["sweeps"] = (clip(liq.score), f"{'buy' if liq.score > 0 else 'sell'} sweeps still hitting")

    if abs(ex.move.z) >= 1:
        out["momentum"] = (clip(ex.move.z / 4), f"momentum ({ex.move.move_pct:+.2f}% this {ex.window})")

    fund = sig.get("funding")
    if fund:
        apr = fund.metrics.get("funding_apr")
        if apr is not None and abs(apr - BASE_FUNDING_APR) >= 10:
            crowd = "longs" if apr > BASE_FUNDING_APR else "shorts"
            out["crowding"] = (-clip((apr - BASE_FUNDING_APR) / 40),
                               f"crowded {crowd} ({apr:+.0f}% APR funding) — {'pullback' if crowd == 'longs' else 'squeeze'} risk")
    return out


def track_line(window: str, t: TrackRecord) -> str:
    """The track record in words. The count is independent periods, not samples:
    sixty 10m leans taken a minute apart are judged on about six 10m stretches.
    Only leans are scored for direction — coin flips make no call."""
    n = "0" if t.periods == 0 else "<1" if t.periods < 1 else f"~{t.periods:.0f}"
    if t.hit_rate is None:
        line = f"scoring leans… {n} of {MIN_PERIODS} separate {window} periods checked"
        if t.range_rate is not None:
            line += f" · inside range {t.range_rate:.0%}"
        return line
    line = f"leans right {t.hit_rate:.0%} over {n} separate {window} periods"
    if t.range_rate is not None:
        line += f" · inside range {t.range_rate:.0%}"
    return line


def make_outlook(ex: Explanation, book_imbalance: float | None, sigma_1s_bps: float, horizon_key: str,
                 track: TrackRecord | None = None) -> Outlook:
    track = track or TrackRecord()
    w = WEIGHTS[horizon_key]
    comps = components(ex, book_imbalance)
    present = sum(w.get(k, 0.0) for k in comps if w.get(k, 0.0) > 0)
    score = clip(sum(w.get(k, 0.0) * v for k, (v, _) in comps.items()) / (present + PRIOR_WEIGHT))
    sigma_h = max(sigma_1s_bps, 1e-6) * math.sqrt(ex.seconds)
    expected = SKILL * score * sigma_h
    p_up = _phi(expected / sigma_h)
    tilt = Direction.UP if score >= NO_TILT else Direction.DOWN if score <= -NO_TILT else Direction.NEUTRAL
    lean = tilt if abs(score) >= STRONG_SCORE else Direction.NEUTRAL

    ranked = sorted(comps.items(), key=lambda kv: -abs(w.get(kv[0], 0.0) * kv[1][0]))
    ranked = [(k, v, r) for k, (v, r) in ranked if abs(w.get(k, 0.0) * v) >= 0.01]
    backing = [r for k, v, r in ranked if v * score > 0][:2]
    against = [r for k, v, r in ranked if v * score < 0][:1]
    if tilt is Direction.NEUTRAL:
        # No tilt: say whether the signals cancel out or are simply too faint to matter.
        ups = [r for k, v, r in ranked if v > 0][:1]
        downs = [r for k, v, r in ranked if v < 0][:1]
        backing, against = ups + downs, []
        why = (f"{ups[0]} vs {downs[0]} cancel out" if ups and downs
               else f"only faint signs: {backing[0]}" if backing else "book and flow balanced")
    else:
        why = ", ".join(backing) if backing else "book and flow balanced"
        if against:
            why += f" — but {against[0]}"

    swing = f"normal swing ±{sigma_h / 100:.2f}%"
    model_odds = p_up if tilt is not Direction.DOWN else 1 - p_up
    if lean is Direction.NEUTRAL:
        tilt_txt = "no tilt" if tilt is Direction.NEUTRAL else f"tilt {tilt.value} {model_odds:.1%}"
        odds, source = model_odds, "model"
        line = f"Next {ex.window}: coin flip · {tilt_txt} · {swing} — {why}"
    else:
        # Once the leans have a real track record, show it instead of the model's estimate.
        earned = track.hit_rate is not None
        odds, source = (track.hit_rate, "earned") if earned else (model_odds, "model")
        line = (f"Next {ex.window}: lean {lean.value} ({odds:.0%} {'earned' if earned else 'est.'}) · "
                f"expected {expected / 100:+.2f}% ({swing}) — {why}")
    reasons = backing + against
    return Outlook(
        horizon_s=ex.seconds, lean=lean, score=round(score, 3), expected_bps=expected, range_bps=sigma_h,
        p_up=round(p_up, 3), reasons=reasons, line=line, hit_rate=track.hit_rate, scored=track.called,
        periods=track.periods, range_rate=track.range_rate, track=track_line(ex.window, track),
        tilt=tilt, odds=round(odds, 3), odds_source=source,
    )

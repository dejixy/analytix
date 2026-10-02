"""
Funding and open-interest moves, measured against the coin's own history.

"+11% APR funding" or "OI −0.4% in 10m" mean little on their own: normal for
one coin is extreme for another, and normal shifts week to week. So both are
ranked against recent history:

  funding   hourly rates over the past week (backfilled on start, so this is
            ready immediately in live mode)
  OI moves  every |ΔOI| over the same timeframe in the saved live bars
            (rolling, gap-free stretches), so a 10m change is compared with
            other 10m changes

A percentile needs a day of hourly funding, or 30 OI samples for the timeframe.
"""
import bisect
from dataclasses import dataclass, field

from models.barModel import Bar

FUNDING_LOOKBACK_MS = 7 * 86_400_000
MIN_FUNDING_HOURS = 24
MIN_OI_SAMPLES = 30


@dataclass(frozen=True, slots=True)
class PositioningModel:
    funding: tuple[float, ...] = ()                                   # sorted hourly rates
    oi_moves: dict[int, tuple[float, ...]] = field(default_factory=dict)   # seconds → sorted |ΔOI %|

    def funding_history(self) -> tuple[float, ...]:
        return self.funding if len(self.funding) >= MIN_FUNDING_HOURS else ()

    def oi_history(self, seconds: int) -> tuple[float, ...]:
        arr = self.oi_moves.get(seconds, ())
        return arr if len(arr) >= MIN_OI_SAMPLES else ()


def percentile(sorted_values: tuple[float, ...], x: float) -> float | None:
    """Share of the history at or below x (0..1). None without history."""
    if not sorted_values:
        return None
    return bisect.bisect_right(sorted_values, x) / len(sorted_values)


def fit_positioning(bars: list[Bar], horizons_s: list[int]) -> PositioningModel:
    if not bars:
        return PositioningModel()
    newest = bars[-1].end_ms
    hourly: dict[int, float] = {}
    for b in bars:
        if b.funding is not None and newest - b.end_ms <= FUNDING_LOOKBACK_MS:
            hourly[b.end_ms // 3_600_000] = b.funding

    runs: list[list[Bar]] = []
    for b in bars:
        if not b.has_flow or not b.open_interest or b.span_s != 60:
            continue
        if runs and b.timestamp - runs[-1][-1].timestamp == 60_000:
            runs[-1].append(b)
        else:
            runs.append([b])
    moves: dict[int, tuple[float, ...]] = {}
    for seconds in sorted(set(horizons_s)):
        k = max(1, seconds // 60)
        step = max(1, k // 10)
        out = [abs(run[i].open_interest / run[i - k].open_interest - 1) * 100
               for run in runs for i in range(k, len(run), step)]
        if out:
            moves[seconds] = tuple(sorted(out))
    return PositioningModel(tuple(sorted(hourly.values())), moves)

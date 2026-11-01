"""
Scheduled US economic releases: the market's known loud moments. Their times are set weeks ahead, so a forecast of
how much price will move can use them without any look-ahead.

    EVENTS      (UTC ms, name, impact) for the releases inside the recordings, from public calendars
                (fedratecalc.com and isnow.ai agree on every date; US Eastern was UTC-4 until 1 Nov 2026)
    upcoming()  whether a High-impact release falls inside a window starting at each given time

The live news feed (api/news.py) adds this week's and next week's releases as it reads them (add_live), so the
live forecast sees upcoming releases too.
"""
import calendar as _cal
import time

import numpy as np


def _utc(s: str) -> int:
    return _cal.timegm(time.strptime(s, "%Y-%m-%d %H:%M")) * 1000


EVENTS = [
    (_utc("2026-10-14 12:30"), "CPI (Sep)", "High"),
    (_utc("2026-10-15 12:30"), "PPI and retail sales (Sep)", "High"),
    (_utc("2026-10-15 12:30"), "Initial jobless claims", "Medium"),
    (_utc("2026-10-22 12:30"), "Initial jobless claims", "Medium"),
    (_utc("2026-10-28 18:00"), "FOMC rate decision", "High"),
    (_utc("2026-10-28 18:30"), "FOMC press conference", "High"),
    (_utc("2026-10-29 12:30"), "GDP (Q3 advance) and PCE (Sep)", "High"),
    (_utc("2026-10-29 12:30"), "Initial jobless claims", "Medium"),
]


LIVE: dict[tuple[int, str], str] = {}          # (time, name) -> impact, from the live calendar feed


def add_live(events) -> None:
    """Releases read from the live calendar: [(UTC ms, name, impact)]."""
    for t, name, impact in events:
        LIVE[(int(t), name)] = impact


def event_times(impact: str = "High") -> np.ndarray:
    times = {t for t, _, imp in EVENTS if imp == impact} | {t for (t, _), imp in LIVE.items() if imp == impact}
    return np.array(sorted(times), dtype=np.int64)


def upcoming(t_ms: np.ndarray, window_ms: int, impact: str = "High") -> np.ndarray:
    """True where a release of that impact is due in (t, t + window]."""
    ev = event_times(impact)
    if not len(ev):
        return np.zeros(len(t_ms), dtype=bool)
    i = np.searchsorted(ev, t_ms, side="right")
    nxt = np.where(i < len(ev), ev[np.minimum(i, len(ev) - 1)], np.iinfo(np.int64).max)
    return nxt <= t_ms + window_ms

"""
What a basket needs to carry on after a restart: its open trades, the last 20 minutes of prices, the orders it's
in the middle of spotting. Saved every SAVE_S seconds and when the app stops (data/<basket>_live.json), loaded when
it starts, so a deploy doesn't lose a trade.

The seconds while the app was down aren't seen. A trade spanning them is still scored, from the prices on either
side: settle() is told the gap is a restart (stale_for), not a broken feed.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("analytix.livestate")

SAVE_S = 30
STALE_S = 5                     # settle()'s normal limit for a gap in the feed
KEEP_PAUSES = 10


def ring_dump(ring: dict[str, dict[int, list]]) -> dict:
    return {coin: [[s, *row] for s, row in rows.items()] for coin, rows in ring.items()}


def ring_load(d: dict | None) -> dict[str, dict[int, list]]:
    out: dict[str, dict[int, list]] = {}
    for coin, rows in (d or {}).items():
        try:
            out[coin] = {int(r[0]): [float(r[1]), float(r[2]), float(r[3])] for r in rows}
        except (TypeError, ValueError, IndexError):
            continue
    return out


def save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("could not save %s: %s", path.name, exc)


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("could not read %s; starting without it", path.name)
        return None


def pauses_after(saved: dict | None) -> list[int]:
    """The seconds (feed time) the app last saved before each recent restart: the gaps start there."""
    if not saved:
        return []
    ps = [int(p) for p in saved.get("pauses") or []] + [int(saved.get("saved_ms", 0)) // 1000]
    return [p for p in ps if p > 0][-KEEP_PAUSES:]


def stale_for(rows: dict[int, list], pauses: list[int], s_from: int, s_to: int) -> int:
    """settle()'s gap limit for a trade from s_from to s_to: the normal one, or long enough to cross a restart
    that falls inside it (from the last save to the first price after it)."""
    stale = STALE_S
    for p in pauses:
        if s_from - STALE_S <= p <= s_to:
            after = [s for s in rows if s > p]
            gap = (min(after) - p) if after else s_to - p
            stale = max(stale, gap + STALE_S)
    return stale

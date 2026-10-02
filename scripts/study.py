"""
Signal study: after each kind of signal, what did price actually do?

Replays recorded sessions through the full pipeline and logs every time a
signal fires, with the direction it implies. Then it looks up the price 1, 5,
15 and 60 minutes later and reports, per signal:

    n       independent occurrences (repeats within one timeframe-length are merged)
    hit     share where price moved the implied way
    avg     mean move in the implied direction, in bps (positive = it worked)
    t       avg ÷ its standard error — |t| under ~2 is indistinguishable from luck

Only what the engine knew at the time is used: a cascade is logged when its OI
check comes in, a level when it breaks, a wall when it goes.

    python -m scripts.record --minutes 720                      # record half a day first
    python -m scripts.study data/recordings/*.jsonl --coin ETH
    python -m scripts.study data/recordings/*.jsonl --csv study.csv

Read it honestly: a few hours of one coin is a handful of independent samples.
Promote a signal to "trust it" only when it holds across days and coins.
"""
import argparse
import bisect
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

from config import COIN, REPLAY_FILE
from ingestion.replay import replay_sync
from ingestion.synthetic import generate_session
from pipeline import Pipeline

HORIZONS_S = (60, 300, 900, 3600)
TICK_WINDOWS = ("1m", "10m", "60m")
FLOW_MIN_STRENGTH = 0.25


@dataclass(slots=True)
class Occurrence:
    signal: str          # e.g. "absorption (10m)"
    ts: int              # exchange ms when the engine knew
    direction: int       # +1 the signal implies up, −1 down
    price: float
    forward: dict[int, float | None] = field(default_factory=dict)   # horizon s → signed move in bps


class Collector:
    """Watches a pipeline as it replays and logs each signal the first time it fires."""

    def __init__(self, pipe: Pipeline):
        self.pipe = pipe
        self.occurrences: list[Occurrence] = []
        self.mids: list[tuple[int, float]] = []          # one mid per exchange second
        self._last_run = 0
        self._last_seen: dict[str, int] = {}
        self._events_seen: set[str] = set()
        self._exits_seen = 0

    def on_message(self, msg: dict) -> None:
        self.pipe.on_message(msg)
        an, st = self.pipe.analyzer, self.pipe.state
        if an.runs == self._last_run or st.mid is None:
            return
        self._last_run = an.runs
        now = st.now_ms
        if not self.mids or now // 1000 > self.mids[-1][0] // 1000:
            self.mids.append((now, st.mid))
        self._windows(now, st.mid)
        self._events(now, st.mid)
        self._walls(now, st.mid)

    def _log(self, signal: str, now: int, direction: int, price: float, every_s: int) -> None:
        last = self._last_seen.get(signal)
        if last is not None and now - last < every_s * 1000:
            self._last_seen[signal] = now          # still the same episode: extend it, don't count it again
            return
        self._last_seen[signal] = now
        self.occurrences.append(Occurrence(signal, now, direction, price))

    def _windows(self, now: int, mid: float) -> None:
        latest = self.pipe.analyzer.latest
        for w in TICK_WINDOWS:
            ex = latest.get(w)
            if ex is None or ex.coverage < 0.95:
                continue
            every = max(60, ex.seconds)
            if ex.level:
                self._log(f"absorption ({w})", now, 1 if ex.level.side == "bid" else -1, mid, every)
            im = ex.impact
            if im and im.verdict in ("against", "absorbed"):
                self._log(f"impact {im.verdict}: fade the flow ({w})", now, -1 if im.net_flow > 0 else 1, mid, every)
            elif im and im.verdict == "outsized":
                self._log(f"impact outsized: follow the move ({w})", now, 1 if im.actual_bps > 0 else -1, mid, every)
        dirs = []
        for w in TICK_WINDOWS:
            f = latest.get(w) and latest[w].signals.get("volume_imbalance")
            dirs.append(f.direction.value if f and f.strength >= FLOW_MIN_STRENGTH else "neutral")
        if dirs[0] != "neutral" and len(set(dirs)) == 1:
            self._log("flow aligned 1m–60m: follow it", now, 1 if dirs[0] == "up" else -1, mid, 600)

    def _events(self, now: int, mid: float) -> None:
        for ev in self.pipe.analyzer.market_events():
            if ev.id in self._events_seen:
                continue
            if ev.kind == "cascade":
                if ev.detail.startswith("checking OI"):
                    continue                                    # wait until the engine knows the verdict
                kind = "likely liquidations" if "likely liquidations" in ev.detail else \
                    "one trader" if "one trader" in ev.detail else "partly liquidations"
                sign = 1 if ev.direction.value == "up" else -1
                self.occurrences.append(Occurrence(f"cascade, {kind}: fade it", now, -sign, mid))
            elif ev.kind == "level_break":
                sign = 1 if ev.direction.value == "up" else -1
                self.occurrences.append(Occurrence("level broke: follow the break", now, sign, mid))
            self._events_seen.add(ev.id)

    def _walls(self, now: int, mid: float) -> None:
        exits = list(self.pipe.analyzer.walls.exits)
        for e in exits[self._exits_seen:] if len(exits) >= self._exits_seen else exits:
            toward = 1 if e.side == "ask" else -1               # price moving toward where the wall stood
            if e.outcome == "pulled_near":
                self.occurrences.append(Occurrence(f"{e.side} wall pulled near price: price goes through", now, toward, mid))
            elif e.outcome == "eaten":
                self.occurrences.append(Occurrence(f"{e.side} wall eaten: follow through", now, toward, mid))
        self._exits_seen = len(exits)


def attach_forward_returns(occ: list[Occurrence], mids: list[tuple[int, float]],
                           horizons_s: tuple[int, ...] = HORIZONS_S) -> None:
    times = [t for t, _ in mids]
    for o in occ:
        for h in horizons_s:
            target = o.ts + h * 1000
            i = bisect.bisect_left(times, target)
            if i >= len(times) or times[i] - target > max(5_000, h * 50):   # no price near enough to that moment
                o.forward[h] = None
                continue
            o.forward[h] = o.direction * (mids[i][1] / o.price - 1) * 10_000


@dataclass(frozen=True, slots=True)
class Row:
    signal: str
    n: int
    stats: dict[int, tuple[int, float, float, float] | None]   # horizon → (n, hit, avg bps, t)


def summarize(occ: list[Occurrence], horizons_s: tuple[int, ...] = HORIZONS_S) -> list[Row]:
    rows = []
    for signal in sorted({o.signal for o in occ}):
        group = [o for o in occ if o.signal == signal]
        stats: dict[int, tuple[int, float, float, float] | None] = {}
        for h in horizons_s:
            xs = [o.forward.get(h) for o in group if o.forward.get(h) is not None]
            if not xs:
                stats[h] = None
                continue
            avg = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - avg) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
            t = avg / (sd / math.sqrt(len(xs))) if sd > 0 else math.nan    # undefined for one sample
            hit = sum(1 for x in xs if x > 0) / len(xs)
            stats[h] = (len(xs), hit, avg, t)
        rows.append(Row(signal, len(group), stats))
    return rows


def run(paths: list[str], coin: str) -> tuple[list[Occurrence], list[Row]]:
    occ: list[Occurrence] = []
    for path in paths:                       # each file is its own session: fresh state, no stitching across gaps
        c = Collector(Pipeline(coin))
        replay_sync(path, c.on_message)
        attach_forward_returns(c.occurrences, c.mids)
        occ += c.occurrences
    return occ, summarize(occ)


def _label(h: int) -> str:
    return f"{h // 60}m"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", default=[str(REPLAY_FILE)])
    p.add_argument("--coin", default=COIN)
    p.add_argument("--csv", default=None, help="also write every occurrence with its forward moves")
    args = p.parse_args()
    if args.paths == [str(REPLAY_FILE)] and not Path(REPLAY_FILE).exists():
        generate_session(REPLAY_FILE)

    occ, rows = run(args.paths, args.coin)
    head = f"{'signal':<52}{'n':>4}" + "".join(f"{'│ ' + _label(h):>9}{'hit':>6}{'avg':>7}{'t':>6}" for h in HORIZONS_S)
    print(head)
    print("─" * len(head))
    for r in rows:
        line = f"{r.signal[:51]:<52}{r.n:>4}"
        for h in HORIZONS_S:
            s = r.stats[h]
            if s is None:
                line += f"{'│':>9}" + f"{'—':>6}" + " " * 13
            else:
                t = f"{'—':>6}" if math.isnan(s[3]) else f"{s[3]:>+6.1f}"
                line += f"{'│':>9}{s[1]:>6.0%}{s[2]:>+7.1f}{t}"
        print(line)
    print("\nhit = share that moved the implied way · avg = mean move that way, bps · t = avg ÷ standard error")
    print("Under ~30 occurrences or |t| < 2, treat it as not yet known.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["signal", "ts", "direction", "price"] + [f"fwd_{_label(h)}_bps" for h in HORIZONS_S])
            for o in occ:
                w.writerow([o.signal, o.ts, o.direction, o.price] + [o.forward.get(h) for h in HORIZONS_S])
        print(f"\nwrote {len(occ)} occurrences to {args.csv}")


if __name__ == "__main__":
    main()

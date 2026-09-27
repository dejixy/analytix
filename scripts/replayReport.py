"""
Run a recorded session through the full pipeline as fast as possible and
print every significant move with its explanation.

    python -m scripts.replayReport data/recordings/eth_20260924_1420.jsonl
    python -m scripts.replayReport                # uses the synthetic sample
"""
import argparse
import time
from datetime import datetime, timezone

from config import COIN, REPLAY_FILE
from ingestion.replay import replay_sync
from ingestion.synthetic import generate_session
from pipeline import Pipeline


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", nargs="?", default=str(REPLAY_FILE))
    p.add_argument("--coin", default=COIN, help="which coin in the recording to analyse")
    p.add_argument("--window", default=None, help="only show events for this window, e.g. 1m")
    args = p.parse_args()

    from pathlib import Path
    if not Path(args.path).exists() and Path(args.path) == REPLAY_FILE:
        generate_session(REPLAY_FILE)

    pipe = Pipeline(args.coin)
    t0 = time.perf_counter()
    n = replay_sync(args.path, pipe.on_message)
    elapsed = time.perf_counter() - t0
    st = pipe.state
    print(f"{n:,} messages, {pipe.analyzer.runs:,} engine ticks in {elapsed:.1f}s "
          f"({st.dropped} dropped as duplicate/out-of-order)\n")

    events = sorted(pipe.analyzer.events.recent(10_000, args.window), key=lambda e: e.peak_ms)
    for ev in events:
        ex = ev.explanation
        print(f"[{fmt_ts(ev.peak_ms)} UTC] {ev.window:>3}  z={ex.move.z:+.1f}  conf={ex.confidence:.0%}")
        print(f"   {ex.headline}")
        for line in ex.narrative:
            print(f"     · {line}")
        print()


if __name__ == "__main__":
    main()

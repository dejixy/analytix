"""
Build the fast store from the recordings: one compressed numpy file per coin per day (storage/tickStore.py).

    python -m scripts.buildStore                   # every day not built yet, or whose hours changed since
    python -m scripts.buildStore --days 3          # only the last 3 days
    python -m scripts.buildStore --rebuild         # everything again

Run it once to convert the history, then again whenever you like (a daily scheduled run is ideal): finished days are
skipped, and today is rebuilt from its finished hours. Studies that support the store read it instead of the gzipped
JSON, in seconds instead of minutes.

On the server, from /opt/analytix (the store must be somewhere you can write; data/ belongs to the analytix user):
    ANALYTIX_STORE_DIR=~/analytix-store nice -n 19 .venv/bin/python -m scripts.buildStore
and pass the same ANALYTIX_STORE_DIR to the studies.
"""
import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from config import RECORD_DIR, STORE_DIR
from storage.tickStore import built_sources, hours_by_day, sources_of, write_day


def _build(store: str, day: str, paths: list[str]) -> tuple[str, list[str], float]:
    t0 = time.time()
    written = write_day(Path(store), day, [Path(p) for p in paths])
    return day, written, time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(RECORD_DIR)])
    ap.add_argument("--out", default=str(STORE_DIR), help="defaults to ANALYTIX_STORE_DIR or data/store")
    ap.add_argument("--days", type=int, default=0, help="only the newest N days (0 = all)")
    ap.add_argument("--jobs", type=int, default=2, help="days built in parallel")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    store = Path(args.out).expanduser()
    try:
        store.mkdir(parents=True, exist_ok=True)
        (store / ".write-test").touch()
        (store / ".write-test").unlink()
    except OSError as exc:
        sys.exit(f"can't write to {store} ({exc}). Set ANALYTIX_STORE_DIR (or --out) to a folder you own, "
                 f"e.g. ANALYTIX_STORE_DIR=~/analytix-store")

    by_day = hours_by_day(args.paths)
    todo = sorted(by_day)[-args.days:] if args.days else sorted(by_day)
    todo = [d for d in todo if args.rebuild or built_sources(store, d) != sources_of(by_day[d])]
    print(f"{len(by_day)} days of recordings, {len(todo)} to build into {store}", file=sys.stderr)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        jobs = [pool.submit(_build, str(store), d, [str(p) for p in by_day[d]]) for d in todo]
        for fut in as_completed(jobs):
            day, written, secs = fut.result()
            print(f"  {day}: {len(by_day[day])} hours -> {', '.join(written) or 'nothing'} ({secs:.0f}s)",
                  file=sys.stderr)
    size = sum(f.stat().st_size for f in store.rglob("*.npz")) / 1e6
    print(f"done in {time.time() - t0:.0f}s · store now {size:,.0f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()

"""
Record the live Hyperliquid feed for replay and for the signal study.

    python -m scripts.record                         # every coin in ANALYTIX_COINS, until stopped
    python -m scripts.record --minutes 30            # a short capture
    python -m scripts.record --coins ETH,BTC

Recordings go to data/recordings/ as one file per hour: the hour being written is plain
.jsonl, finished hours are gzipped. Stop with Ctrl+C (or `systemctl stop` on a server);
nothing is lost but the last moment. Run it again and it carries on.

Then:
    python -m scripts.study                          # what price did after each signal and card tag
    ANALYTIX_MODE=replay ANALYTIX_REPLAY_FILE=data/recordings/<file> uvicorn api.app:app

Recording alongside the dashboard instead: start the backend with ANALYTIX_RECORD=1.
"""
import argparse
import asyncio
import logging
import signal
import sys
import time

from config import COINS, RECORD_DIR, RECORD_MIN_FREE_GB
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.recorder import HourlyRecorder

STATUS_EVERY_S = 60


def _status(rec: HourlyRecorder, status: FeedStatus, started: float, minutes: float | None) -> str:
    up = time.time() - started
    parts = [f"up {up / 3600:.1f}h" if up >= 3600 else f"up {up / 60:.0f}m",
             f"{status.messages:,} messages",
             "connected" if status.connected else f"reconnecting ({status.last_error or 'waiting'})",
             f"this hour {rec.hour_bytes / 1e6:.0f} MB"]
    if rec.paused:
        parts.append("PAUSED: disk nearly full")
    if minutes is not None:
        parts.append(f"{max(minutes * 60 - up, 0) / 60:.1f} min left")
    return " · ".join(parts)


async def record(coins: list[str], minutes: float | None, folder, min_free_gb: float) -> None:
    rec = HourlyRecorder(folder, "-".join(coins).lower(), min_free_gb=min_free_gb)
    status = FeedStatus(mode="live")
    client = HyperliquidClient(coins, on_message=lambda msg: None, status=status, recorder=rec)
    task = asyncio.create_task(client.run())
    stop = asyncio.Event()
    try:                                             # systemd sends SIGTERM; Windows has no signal handlers here
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        pass
    started = time.time()
    tty = sys.stdout.isatty()
    print(f"recording {', '.join(coins)} to {folder} — Ctrl+C to stop", flush=True)
    last_line = 0.0
    try:
        while not stop.is_set() and (minutes is None or time.time() - started < minutes * 60):
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if tty:
                print("\r" + _status(rec, status, started, minutes) + "   ", end="", flush=True)
            elif time.time() - last_line >= STATUS_EVERY_S:       # a log file or journal: one line a minute
                print(_status(rec, status, started, minutes), flush=True)
                last_line = time.time()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        rec.close()
        print(f"\nstopped · last file {rec.current_path}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--coins", default=",".join(COINS), help="comma separated, e.g. ETH,BTC")
    p.add_argument("--minutes", type=float, default=None, help="stop after this long (default: run until stopped)")
    p.add_argument("--dir", default=str(RECORD_DIR), help="where the hourly files go")
    p.add_argument("--min-free-gb", type=float, default=RECORD_MIN_FREE_GB,
                   help="pause recording when free disk falls below this")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    try:
        asyncio.run(record(coins, args.minutes, args.dir, args.min_free_gb))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""
Record the live Hyperliquid feed to JSONL for later replay.

    python -m scripts.record --minutes 30                       # every coin in ANALYTIX_COINS
    python -m scripts.record --coins BTC --out data/recordings/btc.jsonl

Replay it through the dashboard:
    ANALYTIX_MODE=replay ANALYTIX_REPLAY_FILE=data/recordings/<file>.jsonl uvicorn api.app:app
Or print every significant move:
    python -m scripts.replayReport data/recordings/<file>.jsonl
"""
import argparse
import asyncio
import time
from datetime import datetime, timezone

from config import COINS, ROOT
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.recorder import JsonlRecorder


async def record(coins: list[str], minutes: float, out: str) -> None:
    rec = JsonlRecorder(out)
    status = FeedStatus(mode="live")
    client = HyperliquidClient(coins, on_message=lambda msg: None, status=status, recorder=rec)
    task = asyncio.create_task(client.run())
    deadline = time.time() + minutes * 60
    try:
        while time.time() < deadline:
            await asyncio.sleep(5)
            left = deadline - time.time()
            print(f"\r{status.messages:,} messages · connected={status.connected} · "
                  f"{max(left, 0) / 60:.1f} min left   ", end="", flush=True)
    finally:
        task.cancel()
        rec.close()
        print(f"\nsaved {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coins", default=",".join(COINS), help="comma separated, e.g. ETH,BTC")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    out = args.out or str(ROOT / "data" / "recordings" / f"{'-'.join(coins).lower()}_{stamp}.jsonl")
    asyncio.run(record(coins, args.minutes, out))


if __name__ == "__main__":
    main()

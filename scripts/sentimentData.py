"""
Sentiment Check's long history, downloaded or topped up (ingestion/longhist.py): five years of 1-minute candles,
open interest and funding, and three years of 1-second candles, for BTC, ETH, SOL and HYPE.

    python -m scripts.sentimentData                       every coin and kind (the first run takes about an hour)
    python -m scripts.sentimentData --coins BTC --kinds m1

On the server: bash ~/analytix/scripts/run.sh sentiment (in the background, as the app's user, into
/opt/analytix/data/longhist).
"""
import argparse
import sys
import time

from ingestion import longhist as L


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--kinds", default="m1,oi,funding,s1")
    args = ap.parse_args()
    t0 = time.time()

    def say(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    say(f"Sentiment Check history into {L.FOLDER}")
    L.update([c.strip().upper() for c in args.coins.split(",") if c.strip()],
             tuple(k.strip() for k in args.kinds.split(",") if k.strip()), say=say)
    say(f"done in {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    sys.exit(main())

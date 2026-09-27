"""
Generate the synthetic demo session (25 minutes of ETH in Hyperliquid wire format).

    python -m scripts.generateSample [--out data/sampleSession.jsonl] [--seed 7]
"""
import argparse

from config import REPLAY_FILE
from ingestion.synthetic import SCRIPT, generate_session


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(REPLAY_FILE))
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    path = generate_session(args.out, seed=args.seed)
    print(f"wrote {path}")
    print("scripted regimes:")
    for r in SCRIPT:
        print(f"  {r.start_s // 60:02d}:{r.start_s % 60:02d}–{r.end_s // 60:02d}:{r.end_s % 60:02d}  "
              f"{r.name:<17} {r.story}")


if __name__ == "__main__":
    main()

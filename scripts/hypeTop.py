"""
The biggest wallets in the latest HYPE holder map snapshot (scripts/hypeMap.py), with their group, so they can be
looked up on Hypurrscan (https://hypurrscan.io/address/<address>) and named. Names go in a labels file, JSON
{"0xaddress": "Binance"}, which the next map run reads:  bash scripts/run.sh map --labels ~/hype-labels.json

    python -m scripts.hypeTop             # the 20 biggest
    python -m scripts.hypeTop --n 50
"""
import argparse
import json
import sys
from pathlib import Path

from engine import hypemap as H

KNOWN = {H.ASSISTANCE_FUND: "Assistance Fund", H.HYPER_EVM: "HyperEVM bridge (everyone's HYPE on HyperEVM)"}


def fmt(x: float) -> str:
    return f"{x / 1e6:.2f}M" if abs(x) >= 1e6 else f"{x / 1e3:.1f}k" if abs(x) >= 1e3 else f"{x:.0f}"


def top(snap: dict, n: int = 20, labels: dict | None = None) -> list[str]:
    supply = snap.get("supply") or 0
    labels = {k.lower(): v for k, v in (labels or {}).items()}
    rows = sorted(snap["wallets"].items(), key=lambda kv: -(kv[1]["spot"] + kv[1]["staked"] + kv[1]["pending"]))[:n]
    out = [f"  {'#':>3}  {'address':<42}  {'HYPE':>8}{'of circ.':>9}{'spot':>9}{'staked':>9}{'leaving':>9}  group"]
    for i, (a, w) in enumerate(rows, 1):
        total = w["spot"] + w["staked"] + w["pending"]
        name = labels.get(a) or KNOWN.get(a, "")
        out.append(f"  {i:>3}  {a:<42}  {fmt(total):>8}{(total / supply if supply else 0):>9.1%}{fmt(w['spot']):>9}"
                   f"{fmt(w['staked']):>9}{fmt(w['pending']):>9}  {w['group']}" + (f" · {name}" if name and name not in w["group"] else ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--dir", default=str(Path.home() / "hype-holders"))
    ap.add_argument("--labels", default="")
    args = ap.parse_args()
    snaps = sorted(Path(args.dir).glob("snapshot_*.json"))
    if not snaps:
        sys.exit(f"no snapshot in {args.dir}: run the map first (bash scripts/run.sh map)")
    snap = json.loads(snaps[-1].read_text())
    labels = json.loads(Path(args.labels).read_text()) if args.labels else {}
    print(f"The {args.n} biggest HYPE wallets in {snaps[-1].name} (look any up at https://hypurrscan.io/address/<address>)")
    print("\n".join(top(snap, args.n, labels)))


if __name__ == "__main__":
    main()

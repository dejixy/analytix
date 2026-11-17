"""
Nigerian bookmakers' football odds, read on a laptop in Nigeria and sent to the Analytix server through the SSH tunnel
(Bet9ja and BetKing only answer Nigerian addresses, so the server abroad can't read them itself).

Standalone: it needs only Python 3.10+ and one package, not the rest of Analytix.

    pip install NaijaBet_Api
    ssh -N analytix                                  (in another window: the tunnel)
    python naijaOdds.py                              (once)
    python naijaOdds.py --loop                       (every 20 minutes until you stop it with Ctrl+C)

Options: --every 20 (minutes), --server http://localhost:8000, --books bet9ja,betking,nairabet.
If ANALYTIX_BETS_TOKEN is set on the server, set the same value here (or pass --token).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

KEYS = ("home", "draw", "away", "home_or_draw", "home_or_away", "draw_or_away")


def split_match(name: str) -> tuple[str, str] | None:
    for sep in (" - ", " vs ", " v ", " – "):
        if sep in name:
            h, a = name.split(sep, 1)
            return h.strip(), a.strip()
    return None


def read_book(name: str) -> list[dict]:
    from NaijaBet_Api.bookmakers import Bet9ja, Betking, Nairabet
    cls = {"bet9ja": Bet9ja, "betking": Betking, "nairabet": Nairabet}[name]
    bk = cls()
    rows = bk.get_all()
    if getattr(bk, "errors", None):
        print(f"  {name}: {len(bk.errors)} leagues failed ({', '.join(str(k) for k in list(bk.errors)[:4])})")
    out = []
    for r in rows:
        teams = split_match(str(r.get("match") or ""))
        if not teams or not r.get("time"):
            continue
        odds = {k: r.get(k) for k in KEYS if r.get(k)}
        if odds:
            out.append({"home": teams[0], "away": teams[1], "t_ms": int(float(r["time"]) * 1000),
                        "league": str(r.get("league") or ""), "odds": odds})
    return out


def push(server: str, token: str, book: str, rows: list[dict]) -> str:
    body = json.dumps({"book": book, "rows": rows, "sent_ms": int(time.time() * 1000)}).encode()
    req = urllib.request.Request(f"{server.rstrip('/')}/api/bets/odds", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Bets-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()[:200]


def once(args) -> None:
    for book in args.books.split(","):
        book = book.strip().lower()
        try:
            rows = read_book(book)
        except Exception as exc:                                  # noqa: BLE001 - one book down, the others still go
            print(f"  {book}: not read ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        try:
            reply = push(args.server, args.token, book, rows)
            print(f"  {book}: {len(rows)} games sent · server: {reply}")
        except urllib.error.URLError as exc:
            print(f"  {book}: {len(rows)} games read, but the server didn't answer ({exc}). Is the tunnel (ssh -N analytix) open?")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every", type=float, default=20)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--books", default="bet9ja,betking,nairabet")
    ap.add_argument("--token", default=os.getenv("ANALYTIX_BETS_TOKEN", ""))
    args = ap.parse_args()
    try:
        import NaijaBet_Api  # noqa: F401
    except ImportError:
        sys.exit("NaijaBet_Api isn't installed: run  pip install NaijaBet_Api")
    while True:
        print(time.strftime("%H:%M:%S"), "reading the Nigerian books")
        once(args)
        if not args.loop:
            break
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()

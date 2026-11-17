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

Internationals: NaijaBet_Api only knows ten club leagues, so for Bet9ja this also finds the international competitions
(Nations League, qualifiers, friendlies...) in Bet9ja's own list of competitions and reads those. --no-intl turns it off;
--bet9ja-groups 123,456 adds competitions by hand (the number at the end of a competition's address on Bet9ja's site);
--find lists what it found and saves Bet9ja's list to bet9ja_sports.json, without sending anything.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
import os
import re
import sys
import time
import urllib.error
import urllib.request

KEYS = ("home", "draw", "away", "home_or_draw", "home_or_away", "draw_or_away")
INTL_WORDS = ("international", "nations league", "friendl", "world cup", "wc qual", "qualif", "africa cup", "afcon",
              "copa america", "gold cup", "asian cup", "euro 20", "uefa euro")
INTL_SKIP = ("women", "club", "u21", "u23", "u20", "u19", "u17", "youth", "volley", "basket", "futsal", "beach", "esoccer",
             "e-soccer", "esport", "virtual", "srl", "simulated", "handball", "hockey", "rugby", "cricket", "tennis", "darts",
             "baseball", "american football", "aussie", "snooker", "badminton", "boxing", "mma", "golf", "cycling", "motor",
             "water polo", "waterpolo", "floorball", "bandy", "special", "outright", "winner")
NAME_KEYS = ("GN", "G_DESC", "DS", "DESC", "S_DESC", "N", "NAME", "Name", "name")
ID_KEYS = ("GID", "G_ID", "ID", "Id", "id")


def intl_groups(data, cap: int = 30) -> dict[int, str]:
    """{group id: "Soccer / International / UEFA Nations League"} for every competition in Bet9ja's list of sports that
    looks like men's international football. The list's layout isn't documented, so this walks all of it: a node with a
    name and a numeric id (its own, or the key it sits under) counts, judged on its whole path of names."""
    out: dict[int, str] = {}

    def walk(x, key=None, trail=""):
        if isinstance(x, dict):
            name = next((x[k].strip() for k in NAME_KEYS if isinstance(x.get(k), str) and x[k].strip()), None) or next(
                (v.strip() for k, v in x.items() if isinstance(v, str) and v.strip() and str(k).upper().endswith(("DESC", "NAME"))), None)
            gid = next((x[k] for k in ID_KEYS if str(x.get(k, "")).isdigit()), None)
            if gid is None and key is not None and str(key).isdigit():
                gid = key
            here = f"{trail} / {name}" if name else trail
            low = here.lower()
            if name and gid and any(w in low for w in INTL_WORDS) and not any(w in low for w in INTL_SKIP):
                out.setdefault(int(gid), here.strip(" /"))
            for k, v in x.items():
                walk(v, k, here)
        elif isinstance(x, list):
            for v in x:
                walk(v, None, trail)

    walk(data)
    return dict(list(out.items())[:cap])


class Group:
    """Stands in for NaijaBet_Api's league enum: any Bet9ja competition by its group id."""
    def __init__(self, gid: int):
        self.gid, self.name = gid, f"group {gid}"

    def to_endpoint(self, site: str) -> str:
        from NaijaBet_Api.id import endpoints
        return endpoints[site]["leagues"].format(leagueid=self.gid)


B9_ODDS = {"home": "S_1X2_1", "draw": "S_1X2_X", "away": "S_1X2_2", "home_or_draw": "S_DC_1X", "home_or_away": "S_DC_12",
           "draw_or_away": "S_DC_X2"}


def bet9ja_rows(data: dict) -> list[dict]:
    """A Bet9ja competition's games, read directly: NaijaBet_Api's name tables "correct" national teams into clubs'
    and countries' wrong names (Spain becomes Sweden), so its normaliser isn't used here. Kick-offs are Lagos time."""
    out = []
    for e in (data.get("D") or {}).get("E") or []:
        try:
            t = datetime.strptime(str(e["STARTDATE"])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1)))
        except (KeyError, ValueError):
            continue
        o = e.get("O") or {}
        odds = {}
        for k, src in B9_ODDS.items():
            try:
                if o.get(src) is not None:
                    odds[k] = float(o[src])
            except (TypeError, ValueError):
                pass
        if e.get("DS") and odds:
            out.append({"match": e["DS"], "league": e.get("GN") or "", "time": int(t.timestamp()), "match_id": e.get("ID"), **odds})
    return out


def bet9ja_sports(bk) -> dict:
    from NaijaBet_Api.id import endpoints
    bk._warm_sync()
    res = bk.session.get(endpoints["bet9ja"]["sports"], timeout=30)
    res.raise_for_status()
    return res.json()


def bet9ja_intl(bk, extra: list[int], find: bool = False) -> list[dict]:
    """Raw rows from Bet9ja's international competitions (found in its list, plus any given by hand)."""
    groups: dict[int, str] = {}
    try:
        data = bet9ja_sports(bk)
        groups = intl_groups(data)
        if find:
            with open("bet9ja_sports.json", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            print("  saved Bet9ja's list of sports to bet9ja_sports.json")
    except Exception as exc:                                      # noqa: BLE001 - the club leagues still go
        print(f"  bet9ja: couldn't read its list of competitions ({type(exc).__name__}: {str(exc)[:100]})")
    for g in extra:
        groups.setdefault(g, f"group {g} (given by hand)")
    if find or not groups:
        print(f"  bet9ja: {len(groups)} international competitions" + (":" if groups else
              ". Add one by hand with --bet9ja-groups and the number at the end of its address on Bet9ja's site"))
        for g, name in groups.items():
            print(f"    {g}  {name}")
    rows, failed = [], 0
    for g in groups:
        try:
            bk._warm_sync()
            res = bk.session.get(Group(g).to_endpoint("bet9ja"), timeout=30)
            res.raise_for_status()
            rows += bet9ja_rows(res.json())
        except Exception:                                         # noqa: BLE001 - not every node is a competition
            failed += 1
    rows = list({r["match_id"] or r["match"]: r for r in rows}.values())    # a game can sit in two lists
    print(f"  bet9ja internationals: {len(rows)} games from {len(groups) - failed} of {len(groups)} competitions")
    return rows


def split_match(name: str) -> tuple[str, str] | None:
    for sep in (" - ", " vs ", " v ", " – "):
        if sep in name:
            h, a = name.split(sep, 1)
            return h.strip(), a.strip()
    return None


def read_book(name: str, intl: bool = True, extra: list[int] | None = None, find: bool = False) -> list[dict]:
    from NaijaBet_Api.bookmakers import Bet9ja, Betking, Nairabet
    cls = {"bet9ja": Bet9ja, "betking": Betking, "nairabet": Nairabet}[name]
    bk = cls()
    rows = bk.get_all()
    if getattr(bk, "errors", None):
        print(f"  {name}: {len(bk.errors)} leagues failed ({', '.join(str(k) for k in list(bk.errors)[:4])})")
    if name == "bet9ja" and (intl or extra):
        seen = {r.get("match_id") for r in rows}
        rows += [r for r in bet9ja_intl(bk, extra or [], find) if r.get("match_id") not in seen]
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
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode()[:200]


def once(args) -> None:
    for book in args.books.split(","):
        book = book.strip().lower()
        try:
            rows = read_book(book, not args.no_intl, args.bet9ja_groups, args.find)
            if args.find:
                print(f"  {book}: {len(rows)} games read (not sent: --find)")
                continue
        except Exception as exc:                                  # noqa: BLE001 - one book down, the others still go
            print(f"  {book}: not read ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        try:
            reply = push(args.server, args.token, book, rows)
            print(f"  {book}: {len(rows)} games sent · server: {reply}")
        except urllib.error.HTTPError as exc:
            print(f"  {book}: {len(rows)} games read, but the server refused them (HTTP {exc.code}: {exc.read().decode()[:120]})")
        except OSError as exc:                                    # refused, timed out, tunnel down
            print(f"  {book}: {len(rows)} games read, but the server didn't answer ({type(exc).__name__}: {exc})."
                  " Is the tunnel (ssh -N analytix) open? Restart it if the laptop slept or changed network")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every", type=float, default=20)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--books", default="bet9ja,betking,nairabet")
    ap.add_argument("--token", default=os.getenv("ANALYTIX_BETS_TOKEN", ""))
    ap.add_argument("--no-intl", action="store_true", help="don't look for Bet9ja's international competitions")
    ap.add_argument("--bet9ja-groups", default="", help="Bet9ja competition ids (or their addresses), comma-separated")
    ap.add_argument("--find", action="store_true", help="list the international competitions found; send nothing")
    args = ap.parse_args()
    args.bet9ja_groups = [int(re.findall(r"\d+", g)[-1]) for g in args.bet9ja_groups.split(",") if re.findall(r"\d+", g)]
    if args.find:
        args.books = "bet9ja"
    try:
        import NaijaBet_Api  # noqa: F401
    except ImportError:
        sys.exit("NaijaBet_Api isn't installed: run  pip install NaijaBet_Api")
    while True:
        print(time.strftime("%H:%M:%S"), "reading the Nigerian books")
        try:
            once(args)
        except Exception as exc:                                  # noqa: BLE001 - in a loop, try again next time
            print(f"  failed: {type(exc).__name__}: {exc}")
        if not args.loop:
            break
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()

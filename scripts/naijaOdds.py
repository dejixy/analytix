"""
Nigerian bookmakers' football odds, read on a laptop in Nigeria and sent to the Analytix server through the SSH tunnel
(Bet9ja and BetKing only answer Nigerian addresses, so the server abroad can't read them itself).

Standalone: it needs only Python 3.10+ and one package, not the rest of Analytix.

    pip install NaijaBet_Api
    ssh -N analytix                                  (in another window: the tunnel)
    python naijaOdds.py                              (once)
    python naijaOdds.py --loop                       (every 20 minutes until you stop it with Ctrl+C)

Options: --every 20 (minutes), --server http://localhost:8000, --books bet9ja,betking,nairabet,sportybet.
If ANALYTIX_BETS_TOKEN is set on the server, set the same value here (or pass --token).

More competitions: NaijaBet_Api only knows ten club leagues, so for Bet9ja this also finds, in Bet9ja's own list of
competitions, the Champions League, Europa League and Conference League, the Eredivisie, Portugal's, Scotland's,
Belgium's, Turkey's and Greece's top leagues, and the international competitions (Nations League, qualifiers,
friendlies...), and reads those. --no-intl leaves out the internationals, --no-extra everything beyond the ten;
--bet9ja-groups 123,456 adds competitions by hand (the number at the end of a competition's address on Bet9ja's site);
--find lists what it found and saves Bet9ja's list to bet9ja_sports.json, without sending anything.

SportyBet isn't in NaijaBet_Api: it's read straight from the feed its website uses, by Sportradar's competition numbers
(the same numbers NaijaBet_Api keeps for it). --find --books sportybet shows what came back and saves a sample to
sportybet_sample.json.

Over/under 2.5 goals: read from Bet9ja (its feed carries it beside the win prices; NaijaBet_Api drops it, so it's taken
from the same replies) and SportyBet. Each read says how many games had it; --find also saves one Bet9ja competition's
reply to bet9ja_group_sample.json.
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

KEYS = ("home", "draw", "away", "home_or_draw", "home_or_away", "draw_or_away", "over_2_5", "under_2_5")
INTL_WORDS = ("international", "nations league", "friendl", "world cup", "wc qual", "qualif", "africa cup", "afcon",
              "copa america", "gold cup", "asian cup", "euro 20", "uefa euro")
INTL_SKIP = ("women", "club", "u21", "u23", "u20", "u19", "u17", "youth", "volley", "basket", "futsal", "beach", "esoccer",
             "e-soccer", "esport", "virtual", "srl", "simulated", "handball", "hockey", "rugby", "cricket", "tennis", "darts",
             "baseball", "american football", "aussie", "snooker", "badminton", "boxing", "mma", "golf", "cycling", "motor",
             "water polo", "waterpolo", "floorball", "bandy", "special", "outright", "winner")
# The competitions beyond NaijaBet_Api's ten: each is a (where, what) pair, both found in its path of names, so a
# namesake elsewhere (Denmark's Superligaen, Northern Ireland's Premiership) doesn't count
CLUBS = {"Champions League": (("international clubs", "uefa", "europe"), ("champions league",)),
         "Europa League": (("international clubs", "uefa", "europe"), ("europa league",)),
         "Conference League": (("international clubs", "uefa", "europe"), ("conference league",)),
         "Eredivisie": (("netherlands", "holland"), ("eredivisie",)),
         "Liga Portugal": (("portugal",), ("primeira", "liga portugal", "betclic")),
         "Scottish Premiership": (("scotland",), ("premiership",)),
         "Belgian Pro League": (("belgium",), ("pro league", "jupiler", "first division a")),
         "Super Lig": (("turkey", "türkiye", "turkiye"), ("super lig", "süper lig", "superlig")),
         "Greek Super League": (("greece",), ("super league",))}
CLUB_SKIP = tuple(w for w in INTL_SKIP if w != "club") + ("afc ", "caf ", "concacaf", "liga portugal 2", "super league 2",
                                                          "qualif", "challenger")
FUTURES = ("antepost", "outright", "top goalscorer", "to reach", "w/o ", "relegation")   # Bet9ja's futures, not matches
NAME_KEYS = ("GN", "G_DESC", "DS", "DESC", "S_DESC", "N", "NAME", "Name", "name")
ID_KEYS = ("GID", "G_ID", "ID", "Id", "id")


def intl_groups(data, cap: int = 40) -> dict[int, str]:
    """{group id: "Soccer / International / UEFA Nations League"} for every competition in Bet9ja's list of sports that
    looks like men's international football."""
    return find_groups(data, lambda low: any(w in low for w in INTL_WORDS) and not any(w in low for w in INTL_SKIP), cap)


def club_target(low: str) -> str | None:
    """Which of CLUBS a path of names is, if any."""
    if any(w in low for w in CLUB_SKIP):
        return None
    return next((name for name, (where, what) in CLUBS.items() if any(w in low for w in where) and any(w in low for w in what)), None)


def club_groups(data, cap: int = 40) -> dict[int, str]:
    """The same for the European cups and the six leagues beyond NaijaBet_Api's ten."""
    return find_groups(data, lambda low: club_target(low) is not None, cap)


def find_groups(data, match, cap: int = 40) -> dict[int, str]:
    """{group id: its path of names} for every football competition in Bet9ja's list of sports whose path `match`es,
    leaving out its futures sections (Antepost). The list's layout isn't documented, so this walks all of it: a node with a
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
            if (name and gid and "soccer" in [s.strip() for s in low.split(" / ")] and not any(w in low for w in FUTURES)
                    and match(low)):                      # plain "Soccer": not "Antepost Soccer" or "Players Soccer"
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
           "draw_or_away": "S_DC_X2", "over_2_5": "S_OU@2.5_O", "under_2_5": "S_OU@2.5_U"}
GOAL_KEYS = ("over_2_5", "under_2_5")


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


def bet9ja_extra(bk, extra: list[int], find: bool = False, intl: bool = True) -> list[dict]:
    """Raw rows from Bet9ja's competitions beyond the library's ten (found in its list, plus any given by hand)."""
    groups: dict[int, str] = {}
    try:
        data = bet9ja_sports(bk)
        groups = {**club_groups(data), **(intl_groups(data) if intl else {})}
        if find:
            with open("bet9ja_sports.json", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            print("  saved Bet9ja's list of sports to bet9ja_sports.json")
    except Exception as exc:                                      # noqa: BLE001 - the club leagues still go
        print(f"  bet9ja: couldn't read its list of competitions ({type(exc).__name__}: {str(exc)[:100]})")
    for g in extra:
        groups.setdefault(g, f"group {g} (given by hand)")
    if find:
        missing = [n for n in CLUBS if not any(club_target(p.lower()) == n for p in groups.values())]
        if missing:
            print(f"  bet9ja: not found in its list: {', '.join(missing)}")
    if find or not groups:
        print(f"  bet9ja: {len(groups)} more competitions" + (":" if groups else
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
    print(f"  bet9ja cups, more leagues and internationals: {len(rows)} games from {len(groups) - failed} of {len(groups)} competitions")
    return rows


# ── SportyBet ────────────────────────────────────────────────────────────────
SPORTY = {"Premier League": 17, "Championship": 18, "League One": 24, "League Two": 25, "LaLiga": 8, "Serie A": 23,
          "Bundesliga": 35, "Ligue 1": 34, "Eredivisie": 37, "Liga Portugal": 238, "Premiership": 36, "Pro League": 38,
          "Super Lig": 52, "Super League 1": 185, "Champions League": 7, "Europa League": 679, "Conference League": 34480}
SPORTY_EVENTS = "https://www.sportybet.com/api/ng/factsCenter/pcEvents"
SPORTY_UPCOMING = ("https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1%2C10%2C18"
                   "&pageSize=100&pageNum={page}&option=1")
SPORTY_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                  "Accept": "application/json", "Content-Type": "application/json", "Referer": "https://www.sportybet.com/ng/",
                  "clientid": "web", "platform": "web"}
SPORTY_IDS = {"1": {"1": "home", "2": "draw", "3": "away"}, "10": {"9": "home_or_draw", "10": "home_or_away", "11": "draw_or_away"},
              "18": {"12": "over_2_5", "13": "under_2_5"}}                # over/under, only its total=2.5 line
SPORTY_DESC = {"home": "home", "1": "home", "draw": "draw", "x": "draw", "away": "away", "2": "away",
               "home or draw": "home_or_draw", "1x": "home_or_draw", "1/x": "home_or_draw",
               "home or away": "home_or_away", "12": "home_or_away", "1/2": "home_or_away",
               "draw or away": "draw_or_away", "x2": "draw_or_away", "x/2": "draw_or_away",
               "over 2.5": "over_2_5", "under 2.5": "under_2_5"}


def sporty_rows(data, league: str = "") -> list[dict]:
    """Every match in a SportyBet reply, whatever its nesting: a node with homeTeamName and awayTeamName is a match, its
    markets "1" (1X2), "10" (double chance) and "18" at total=2.5 (over/under 2.5 goals) give the odds."""
    out = []

    def walk(x, lg):
        if isinstance(x, dict):
            lg = x.get("name") if isinstance(x.get("name"), str) and isinstance(x.get("events"), list) else lg
            if x.get("homeTeamName") and x.get("awayTeamName"):
                odds = {}
                for m in x.get("markets") or []:
                    if str(m.get("id")) == "18" and str(m.get("specifier") or "").replace(" ", "") != "total=2.5":
                        continue
                    ids = SPORTY_IDS.get(str(m.get("id")), {})
                    for o in m.get("outcomes") or []:
                        if o.get("isActive") == 0:
                            continue
                        k = ids.get(str(o.get("id"))) or (SPORTY_DESC.get(str(o.get("desc", "")).strip().lower())
                                                          if str(m.get("id")) in SPORTY_IDS else None)
                        try:
                            if k and float(o.get("odds")) > 1:
                                odds[k] = float(o["odds"])
                        except (TypeError, ValueError):
                            pass
                t = x.get("estimateStartTime")
                if odds and t:
                    out.append({"match": f"{x['homeTeamName']} - {x['awayTeamName']}", "time": int(t) / 1000,
                                "league": lg or "", "match_id": x.get("eventId"), **odds})
                return
            for v in x.values():
                walk(v, lg)
        elif isinstance(x, list):
            for v in x:
                walk(v, lg)

    walk(data, league)
    return out


def read_sportybet(find: bool = False) -> list[dict]:
    import requests
    s = requests.Session()
    s.headers.update(SPORTY_HEADERS)
    rows, failed, sample = [], [], None
    for name, tid in SPORTY.items():
        body = [{"sportId": "sr:sport:1", "marketId": "1,10,18", "tournamentId": [[f"sr:tournament:{tid}"]]}]
        try:
            r = s.post(SPORTY_EVENTS, json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            sample = sample or data
            got = sporty_rows(data, name)
            rows += got
            if find:
                print(f"    {name}: {len(got)} games")
        except Exception as exc:                                  # noqa: BLE001 - one competition down, the others still go
            failed.append(f"{name} ({type(exc).__name__})")
        time.sleep(0.4)
    if not rows:                                                  # the per-competition feed didn't answer: the upcoming list
        for page in range(1, 6):
            try:
                r = s.get(SPORTY_UPCOMING.format(page=page), timeout=30)
                r.raise_for_status()
                data = r.json()
                sample = sample or data
                got = sporty_rows(data)
                if not got:
                    break
                rows += got
            except Exception as exc:                              # noqa: BLE001
                failed.append(f"upcoming page {page} ({type(exc).__name__})")
                break
    if find and sample is not None:
        with open("sportybet_sample.json", "w", encoding="utf-8") as fh:
            json.dump(sample, fh)
        print("  saved one SportyBet reply to sportybet_sample.json")
    if failed:
        print(f"  sportybet: {len(failed)} reads failed ({', '.join(failed[:4])})")
    return list({r["match_id"] or r["match"]: r for r in rows}.values())


def split_match(name: str) -> tuple[str, str] | None:
    for sep in (" - ", " vs ", " v ", " – "):
        if sep in name:
            h, a = name.split(sep, 1)
            return h.strip(), a.strip()
    return None


def read_book(name: str, intl: bool = True, extra: list[int] | None = None, find: bool = False, more: bool = True) -> list[dict]:
    if name == "sportybet":
        rows = read_sportybet(find)
    else:
        from NaijaBet_Api.bookmakers import Bet9ja, Betking, Nairabet
        cls = {"bet9ja": Bet9ja, "betking": Betking, "nairabet": Nairabet}[name]
        bk = cls()
        goals: dict = {}
        if name == "bet9ja":
            keep_goals(bk, goals, find)
        rows = bk.get_all()
        for r in rows:
            r.update({k: v for k, v in goals.get(r.get("match_id"), {}).items() if not r.get(k)})
        if getattr(bk, "errors", None):
            print(f"  {name}: {len(bk.errors)} leagues failed ({', '.join(str(k) for k in list(bk.errors)[:4])})")
        if name == "bet9ja" and (more or extra):
            seen = {r.get("match_id") for r in rows}
            rows += [r for r in bet9ja_extra(bk, extra or [], find, intl and more) if r.get("match_id") not in seen]
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


def keep_goals(bk, into: dict, find: bool = False) -> None:
    """NaijaBet_Api's Bet9ja reader keeps only the win and double-chance prices: read over/under 2.5 from the same
    replies as they're parsed ({match id: {over_2_5, under_2_5}})."""
    parse = bk.normalizer

    def normalizer(data):
        for r in bet9ja_rows(data):
            got = {k: r[k] for k in GOAL_KEYS if r.get(k)}
            if got:
                into[r.get("match_id")] = got
        if find and not os.path.exists("bet9ja_group_sample.json"):
            with open("bet9ja_group_sample.json", "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            print("  saved one Bet9ja competition's reply to bet9ja_group_sample.json")
        return parse(data)
    bk.normalizer = normalizer


def goals_line(book: str, rows: list[dict]) -> str:
    n = sum(1 for r in rows if r["odds"].get("over_2_5") and r["odds"].get("under_2_5"))
    return f"  {book}: over/under 2.5 on {n} of {len(rows)} games"


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
            rows = read_book(book, not args.no_intl, args.bet9ja_groups, args.find, not args.no_extra)
            if book in ("bet9ja", "sportybet"):
                print(goals_line(book, rows))
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
    ap.add_argument("--books", default="bet9ja,betking,nairabet,sportybet")
    ap.add_argument("--token", default=os.getenv("ANALYTIX_BETS_TOKEN", ""))
    ap.add_argument("--no-intl", action="store_true", help="leave out Bet9ja's international competitions")
    ap.add_argument("--no-extra", action="store_true", help="only NaijaBet_Api's ten leagues: no cups, extra leagues or internationals")
    ap.add_argument("--bet9ja-groups", default="", help="Bet9ja competition ids (or their addresses), comma-separated")
    ap.add_argument("--find", action="store_true", help="list the international competitions found; send nothing")
    args = ap.parse_args()
    args.bet9ja_groups = [int(re.findall(r"\d+", g)[-1]) for g in args.bet9ja_groups.split(",") if re.findall(r"\d+", g)]
    if args.find and "--books" not in sys.argv:
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

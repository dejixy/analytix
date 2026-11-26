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

Without the tunnel: send to the public link instead. On the server, `bash scripts/run.sh token` makes the token and
prints the command; run it once with --save (it remembers the link and token in naijaOdds.json beside this script) and
from then on `python naijaOdds.py --loop` is enough, on the laptop or on a phone that stays on (Termux on Android).

More competitions: NaijaBet_Api only knows ten club leagues, so for Bet9ja this also finds, in Bet9ja's own list of
competitions, the Champions League, Europa League and Conference League, the Eredivisie, Portugal's, Scotland's,
Belgium's, Turkey's and Greece's top leagues, and the international competitions (Nations League, qualifiers,
friendlies...), and reads those. --no-intl leaves out the internationals, --no-extra everything beyond the ten;
--bet9ja-groups 123,456 adds competitions by hand (the number at the end of a competition's address on Bet9ja's site);
--find lists what it found and saves Bet9ja's list to bet9ja_sports.json, without sending anything.

SportyBet isn't in NaijaBet_Api: it's read straight from the feed its website uses, by Sportradar's competition numbers
(the same numbers NaijaBet_Api keeps for it). --find --books sportybet shows what came back and saves a sample to
sportybet_sample.json.

SportyBet booking codes: SportyBet only answers Nigerian addresses, so the combo builder's codes are made here. With
--loop, between reads it looks every 15 seconds for slips the page asked a code for, books each on SportyBet (no account
or stake: the same "book bet" its website has) and sends the code back. --try-code makes one code for a single bet and
prints SportyBet's answer, to check it works. The same loop loads the tickets the combo builder's Vet is given (a
booking code in, its games and SportyBet's odds back); --try-vet CODE loads one and prints what came back.

The combo builder's other markets (draw no bet, over/under 0.5 to 4.5, each team's goals, both teams to score, a
result or over x, total corners) are read from SportyBet (markets 11, 18, 19, 20, 29 and 166) and from whatever of them
Bet9ja's competition lists carry (S_DNB, S_OU, S_HAOU, S_GGNG, S_CHANCEMIXOU, S_CHANCEMIXGGOU, S_OUCORNERS), and sent
under the server's own codes; each read says how many games had them.

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
# The combo builder's other markets, sent under the server's own codes (engine/goals.py, engine/corners.py)
MORE = re.compile(r"^(DNB[12]|[OU][0-4]5|[HA][OU][0-2]5|GG|NG|[12X]\|O[12]5|C[HA]?[OU]\d{1,2}5|S[HTV][HA]?[OU]\d{1,2}5"
                  r"|AH[12][MP]\d\d|EH\d\d[1X2]|PS\d{1,12}|SUB[YN]|UP[12][12])$")


def b9_more(o: dict) -> dict:
    """Bet9ja's keys for the combo builder's other markets, as the server's codes: draw no bet (S_DNB), over/under
    0.5-4.5 (S_OU), each team's goals (S_HAOU), both score (S_GGNG), a result or over x (S_CHANCEMIXOU at 1.5,
    S_CHANCEMIXGGOU at 2.5) and total corners (S_OUCORNERS)."""
    out = {}
    for k, v in o.items():
        m = re.match(r"^S_([A-Z]+)(?:@([0-9.]+))?_(\w+)$", str(k))
        if not m:
            continue
        name, line, res = m.group(1), m.group(2), m.group(3)
        half = re.fullmatch(r"(\d+)\.5", line or "")
        n = half.group(1) if half else None
        code = None
        if name == "DNB" and res in ("1", "2"):
            code = f"DNB{res}"
        elif name == "OU" and n and int(n) <= 4 and res in ("O", "U") and n != "2":
            code = f"{res}{n}5"
        elif name == "HAOU" and n and int(n) <= 2 and res in ("OH", "UH", "OA", "UA"):
            code = f"{res[1]}{res[0]}{n}5"
        elif name == "GGNG" and res in ("Y", "N"):
            code = "GG" if res == "Y" else "NG"
        elif name in ("CHANCEMIXOU", "CHANCEMIXGGOU") and n in ("1", "2") and res in ("1orOv", "2orOv", "XorOv"):
            code = f"{res[0]}|O{n}5" if (name == "CHANCEMIXOU") == (n == "1") else None
        elif name == "OUCORNERS" and n and 5 <= int(n) <= 14 and res in ("O", "U"):
            code = f"C{res}{n}5"
        try:
            if code and MORE.match(code) and float(v) > 1:
                out[code] = float(v)
        except (TypeError, ValueError):
            pass
    return out


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
            out.append({"match": e["DS"], "league": e.get("GN") or "", "time": int(t.timestamp()), "match_id": e.get("ID"),
                        "more": b9_more(o), **odds})
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
# 1X2, double chance, draw no bet, 3-way and Asian handicap, over/under, home/away goals, GG/NG, anytime goalscorer,
# corners, home/draw/away or over, goalkeeper saves (and who starts in goal), shots and shots on target (match, home, away),
# a goal scored by a substitute, 1Up and 2Up
SPORTY_MARKETS = ("1,10,11,14,16,18,19,20,29,40,166,854,856,858,800297,830297,900393,900394,900546,900547,900552,900553,"
                  "900606,60200,60100")
SCORERS_KEPT = 8                 # each game's shortest-priced anytime goalscorers sent
SPORTY_UPCOMING = ("https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId="
                   + SPORTY_MARKETS.replace(",", "%2C") + "&pageSize=100&pageNum={page}&option=1")
SPORTY_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                  "Accept": "application/json", "Content-Type": "application/json", "Referer": "https://www.sportybet.com/ng/",
                  "clientid": "web", "platform": "web"}
SPORTY_SHARE = "https://www.sportybet.com/api/ng/orders/share"
SPORTY_IDS = {"1": {"1": "home", "2": "draw", "3": "away"}, "10": {"9": "home_or_draw", "10": "home_or_away", "11": "draw_or_away"}}


def sporty_more(mid: str, spec: str, oid: str, desc: str) -> str | None:
    """A SportyBet outcome as a key the server knows: over_2_5 / under_2_5, or one of the combo builder's codes."""
    spec, d = spec.replace(" ", ""), desc.strip().lower()
    half = re.fullmatch(r"total=(\d+)\.5", spec)
    n = half.group(1) if half else None
    side = "O" if oid == "12" or d.startswith("over") else "U" if oid == "13" or d.startswith("under") else None
    if mid == "18" and n and side and int(n) <= 4:
        return ("over_2_5" if side == "O" else "under_2_5") if n == "2" else f"{side}{n}5"
    if mid in ("19", "20") and n and side and int(n) <= 2:
        return f"{'H' if mid == '19' else 'A'}{side}{n}5"
    if mid == "11":
        return "DNB1" if oid == "4" or d in ("home", "1") else "DNB2" if oid == "5" or d in ("away", "2") else None
    if mid == "29":
        return "GG" if oid == "74" or d == "yes" else "NG" if oid == "76" or d == "no" else None
    if mid == "166" and n and side and 5 <= int(n) <= 14:
        return f"C{side}{n}5"
    if mid in SPORTY_SHOTS and n and side:
        return f"{SPORTY_SHOTS[mid]}{side}{n}5"
    if mid in SPORTY_OR and n in ("1", "2") and (oid == "74" or d == "yes"):
        return f"{SPORTY_OR[mid]}|O{n}5"
    if mid == "16" and oid in ("1714", "1715"):                    # Asian handicap: the line is the home side's
        h = re.fullmatch(r"hcp=([+-]?\d+(?:\.\d+)?)", spec)
        if h:
            line = float(h.group(1)) * (1 if oid == "1714" else -1)
            t = round(abs(line) * 10)
            if t in (5, 10, 15, 20, 25, 30, 35) and abs(abs(line) * 10 - t) < 1e-6:
                return f"AH{'1' if oid == '1714' else '2'}{'M' if line < 0 else 'P'}{t:02d}"
    if mid == "14" and oid in ("1711", "1712", "1713"):            # three-way handicap h:a
        h = re.fullmatch(r"hcp=(\d):(\d)", spec)
        if h:
            return f"EH{h.group(1)}{h.group(2)}{'1X2'[int(oid) - 1711]}"
    if mid in ("60200", "60100") and oid in ("1", "3"):              # 1X2 - 1Up / 2Up: home or away
        return f"UP{'1' if mid == '60200' else '2'}{'1' if oid == '1' else '2'}"
    if mid == "900606":                                            # a goal scored by a substitute: yes / no
        return "SUBY" if oid == "74" or d == "yes" else "SUBN" if oid == "76" or d == "no" else None
    if mid == "40" and oid.startswith("sr:player:") and oid[10:].isdigit():   # anytime goalscorer
        return f"PS{oid[10:]}"
    if mid == "800297":                                            # a goalkeeper's saves, k or more
        p = oid.split(":")
        if len(p) == 6 and p[4].isdigit() and p[5] in ("1", "2") and 1 <= int(p[4]) <= 8:
            return f"SV{'H' if p[5] == '1' else 'A'}O{int(p[4]) - 1}5"
    return None


SPORTY_SHOTS = {"900394": "SH", "900393": "ST", "900552": "SHH", "900553": "SHA", "900546": "STH", "900547": "STA"}
SPORTY_OR = {"854": "1", "856": "X", "858": "2"}                  # home / draw / away or over x.5 goals


def _keepers(markets: list) -> set:
    """The two goalkeepers expected to start, from SportyBet's "most saves" pairing (empty if it isn't there)."""
    out = set()
    for m in markets:
        if str(m.get("id")) == "830297":
            for o in m.get("outcomes") or []:
                p = str(o.get("id")).split(":")
                if len(p) == 4:
                    out.add(p[3])
    return out
SPORTY_DESC = {"home": "home", "1": "home", "draw": "draw", "x": "draw", "away": "away", "2": "away",
               "home or draw": "home_or_draw", "1x": "home_or_draw", "1/x": "home_or_draw",
               "home or away": "home_or_away", "12": "home_or_away", "1/2": "home_or_away",
               "draw or away": "draw_or_away", "x2": "draw_or_away", "x/2": "draw_or_away"}


def sporty_rows(data, league: str = "") -> list[dict]:
    """Every match in a SportyBet reply, whatever its nesting: a node with homeTeamName and awayTeamName is a match, its
    markets "1" (1X2) and "10" (double chance) give the win prices, and the rest of SPORTY_MARKETS the combo builder's."""
    out = []

    def walk(x, lg):
        if isinstance(x, dict):
            lg = x.get("name") if isinstance(x.get("name"), str) and isinstance(x.get("events"), list) else lg
            if x.get("homeTeamName") and x.get("awayTeamName"):
                odds, sel, more, players = {}, {}, {}, {}
                starters, keeper = _keepers(x.get("markets") or []), {}
                for m in x.get("markets") or []:
                    mid = str(m.get("id"))
                    ids = SPORTY_IDS.get(mid, {})
                    for o in m.get("outcomes") or []:
                        if o.get("isActive") == 0:
                            continue
                        if mid == "800297":                       # one goalkeeper a side: the expected starter
                            p = str(o.get("id")).split(":")
                            if len(p) != 6 or (starters and p[3] not in starters) or keeper.setdefault(p[5], p[3]) != p[3]:
                                continue
                        k = ids.get(str(o.get("id"))) or (SPORTY_DESC.get(str(o.get("desc", "")).strip().lower())
                                                          if mid in SPORTY_IDS else None)
                        k = k or sporty_more(mid, str(m.get("specifier") or ""), str(o.get("id")), str(o.get("desc") or ""))
                        try:
                            if k and float(o.get("odds")) > 1:
                                (more if MORE.match(k) else odds)[k] = float(o["odds"])
                                if k.startswith("PS"):
                                    players[k] = str(o.get("desc") or "")
                                sel[k] = {"event": x.get("eventId"), "market": str(m.get("id")),
                                          "specifier": str(m.get("specifier") or ""), "outcome": str(o.get("id"))}
                        except (TypeError, ValueError):
                            pass
                cut = sorted((k for k in more if k.startswith("PS")), key=lambda k: more[k])[SCORERS_KEPT:]
                for k in cut:                                     # only each game's shortest-priced scorers travel
                    more.pop(k, None)
                    sel.pop(k, None)
                    players.pop(k, None)
                t = x.get("estimateStartTime")
                if odds and t:
                    out.append({"match": f"{x['homeTeamName']} - {x['awayTeamName']}", "time": int(t) / 1000,
                                "league": lg or "", "match_id": x.get("eventId"), "sporty_ids": sel, "more": more,
                                "players": players, **odds})
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
        body = [{"sportId": "sr:sport:1", "marketId": SPORTY_MARKETS, "tournamentId": [[f"sr:tournament:{tid}"]]}]
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


def sporty_session():
    import requests
    s = requests.Session()
    s.headers.update(SPORTY_HEADERS)
    return s


def sporty_code(session, selections: list[dict]) -> dict:
    """Book a slip on SportyBet: {code, url, deadline_ms, missing} or {error}. selections: [{eventId, marketId,
    specifier, outcomeId}]."""
    try:
        r = session.post(SPORTY_SHARE, json={"selections": selections}, timeout=30)
        body = r.json() if r.content else {}
    except Exception as exc:                                      # noqa: BLE001 - reported back to the page
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    d = body.get("data") if isinstance(body, dict) else None
    if r.status_code != 200 or not isinstance(d, dict) or not d.get("shareCode"):
        msg = (body.get("message") or body.get("msg")) if isinstance(body, dict) else None
        return {"error": f"HTTP {r.status_code}" + (f": {str(msg)[:120]}" if msg else "")}
    missing = []
    for o in d.get("unavailableOutcomes") or []:
        if isinstance(o, dict):
            missing.append(" ".join(str(o.get(k) or "") for k in ("homeTeamName", "awayTeamName", "marketDesc", "desc")).strip()
                           or str(o.get("eventId") or "a bet"))
    return {"code": d["shareCode"], "url": d.get("shareURL"), "deadline_ms": d.get("deadline"), "missing": missing}


def sporty_ticket(data: dict) -> list[dict]:
    """A loaded booking code's games: [{event, market, specifier, outcome, odds, active, home, away, t_ms, league,
    market_desc, desc, status}] from SportyBet's answer (data.outcomes: each game with the market and outcome booked)."""
    legs = []
    for ev in data.get("outcomes") or []:
        if not isinstance(ev, dict):
            continue
        cat = ((ev.get("sport") or {}).get("category") or {})
        league = " ".join(x for x in (cat.get("name"), (cat.get("tournament") or {}).get("name")) if x)
        for m in ev.get("markets") or []:
            for o in (m or {}).get("outcomes") or []:
                legs.append({"event": ev.get("eventId"), "market": str(m.get("id") or ""), "specifier": str(m.get("specifier") or ""),
                             "outcome": str(o.get("id") or ""), "odds": o.get("odds"), "active": o.get("isActive", 1),
                             "home": ev.get("homeTeamName"), "away": ev.get("awayTeamName"), "t_ms": ev.get("estimateStartTime"),
                             "league": league, "market_desc": m.get("desc") or m.get("name"), "desc": o.get("desc"),
                             "status": ev.get("matchStatus")})
    booked = {(str(x.get("eventId")), str(x.get("marketId")), str(x.get("outcomeId")))
              for x in ((data.get("ticket") or {}).get("selections") or []) if isinstance(x, dict)}
    return [x for x in legs if (str(x["event"]), x["market"], x["outcome"]) in booked] if booked else legs


def sporty_load(session, code: str) -> dict:
    """A booking code's ticket from SportyBet: {legs, deadline_ms} or {error}."""
    try:
        r = session.get(f"{SPORTY_SHARE}/{code}", timeout=30)
        body = r.json() if r.content else {}
    except Exception as exc:                                      # noqa: BLE001 - reported back to the page
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    d = body.get("data") if isinstance(body, dict) else None
    if r.status_code != 200 or not isinstance(d, dict) or not d.get("outcomes"):
        msg = (body.get("message") or body.get("msg")) if isinstance(body, dict) else None
        return {"error": "code not found or expired" if r.status_code == 200 and not msg else f"HTTP {r.status_code}"
                + (f": {str(msg)[:120]}" if msg else "")}
    legs = sporty_ticket(d)
    return {"legs": legs, "deadline_ms": d.get("deadline")} if legs else {"error": "no games on that code"}


def server_call(server: str, token: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"{server.rstrip('/')}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Bets-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


_last_error = [""]


def serve_codes(args, session) -> None:
    """The slips the page asked a SportyBet code for: book each and send the code back."""
    try:
        jobs = server_call(args.server, args.token, "/api/bets/code-jobs") or []
        _last_error[0] = ""
    except Exception as exc:                                      # noqa: BLE001 - the tunnel may be down; the next look
        msg = f"{type(exc).__name__}: {str(exc)[:120]}"
        if isinstance(exc, urllib.error.HTTPError):
            msg = f"HTTP {exc.code}" + (" (the server is on an older version: pull and restart it)" if exc.code in (404, 405)
                                        else " (wrong or missing token)" if exc.code == 403 else "")
        if msg != _last_error[0]:                                 # said once, not every 15 seconds
            print(time.strftime("%H:%M:%S"), f"  couldn't look for SportyBet code requests: {msg}")
            _last_error[0] = msg
        return
    if not isinstance(jobs, list):
        return
    for job in jobs:
        got = sporty_code(session, job.get("selections") or [])
        print(time.strftime("%H:%M:%S"), f"SportyBet code for a {len(job.get('selections') or [])}-game slip:",
              got.get("code") or got.get("error"))
        try:
            server_call(args.server, args.token, "/api/bets/code-jobs", {"id": job["id"], **got})
        except Exception as exc:                                  # noqa: BLE001
            print(f"  couldn't send it back ({type(exc).__name__})")


def serve_vets(args, session) -> None:
    """The booking codes the page's Vet was given: load each ticket from SportyBet and send its games back."""
    try:
        jobs = server_call(args.server, args.token, "/api/bets/vet-jobs") or []
    except Exception:                                             # noqa: BLE001 - an older server, or the tunnel is down
        return
    for job in jobs if isinstance(jobs, list) else []:
        got = sporty_load(session, str(job.get("code") or ""))
        print(time.strftime("%H:%M:%S"), f"SportyBet ticket {job.get('code')} for the Vet:",
              f"{len(got['legs'])} games" if got.get("legs") else got.get("error"))
        try:
            server_call(args.server, args.token, "/api/bets/vet-jobs", {"id": job["id"], **got})
        except Exception as exc:                                  # noqa: BLE001
            print(f"  couldn't send it back ({type(exc).__name__})")


def try_vet(code: str) -> None:
    """One booking code loaded from SportyBet, with what came back printed."""
    s = sporty_session()
    r = s.get(f"{SPORTY_SHARE}/{code.strip().upper()}", timeout=30)
    print(f"{code}: HTTP {r.status_code}")
    print(r.text[:800])
    got = sporty_load(s, code.strip().upper())
    for x in got.get("legs") or []:
        print(f"  {x['home']} v {x['away']}: {x['market_desc']} {x['desc']} at {x['odds']}")
    if got.get("error"):
        print("error:", got["error"])


def try_code() -> None:
    """One SportyBet code for a single bet from its current list, with SportyBet's whole answer printed."""
    rows = read_sportybet()
    row = next((r for r in rows if (r.get("sporty_ids") or {}).get("home")), None)
    if not row:
        sys.exit("SportyBet sent no games with their ids: run with --find --books sportybet and send the sample")
    i = row["sporty_ids"]["home"]
    sel = [{"eventId": i["event"], "marketId": i["market"], "specifier": i["specifier"] or None, "outcomeId": i["outcome"]}]
    s = sporty_session()
    r = s.post(SPORTY_SHARE, json={"selections": sel}, timeout=30)
    print(f"{row['match']}, home win: HTTP {r.status_code}")
    print(r.text[:800])
    print("code:", sporty_code(s, sel))


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
        odds.update({k: v for k, v in (r.get("more") or {}).items() if MORE.match(k)})
        if odds:
            row = {"home": teams[0], "away": teams[1], "t_ms": int(float(r["time"]) * 1000),
                   "league": str(r.get("league") or ""), "odds": odds}
            ids = {k: v for k, v in (r.get("sporty_ids") or {}).items() if k in odds and v.get("event")}
            if ids:                                               # SportyBet's own ids, to make a booking code from
                row["ids"] = ids
            names = {k: v for k, v in (r.get("players") or {}).items() if k in odds}
            if names:                                             # players to score: who they are
                row["players"] = names
            out.append(row)
    return out


def keep_goals(bk, into: dict, find: bool = False) -> None:
    """NaijaBet_Api's Bet9ja reader keeps only the win and double-chance prices: read over/under 2.5 from the same
    replies as they're parsed ({match id: {over_2_5, under_2_5}})."""
    parse = bk.normalizer

    def normalizer(data):
        for r in bet9ja_rows(data):
            got = {k: r[k] for k in GOAL_KEYS if r.get(k)}
            if r.get("more"):
                got["more"] = r["more"]
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
    more = sum(1 for r in rows if any(MORE.match(k) for k in r["odds"]))
    def has(test):
        return sum(1 for r in rows if any(MORE.match(k) and test(k) for k in r["odds"]))
    return (f"  {book}: over/under 2.5 on {n} of {len(rows)} games; other markets on {more}, corners on "
            f"{has(lambda k: k.startswith('C'))}, handicaps on {has(lambda k: k[:2] in ('AH', 'EH'))}, shots on "
            f"{has(lambda k: k[:2] in ('SH', 'ST'))}, keeper saves on {has(lambda k: k.startswith('SV'))}, "
            f"win or over on {has(lambda k: '|' in k)}, players to score on {has(lambda k: k.startswith('PS'))}, "
            f"substitute scores on {has(lambda k: k.startswith('SUB'))}, 1Up/2Up on {has(lambda k: k.startswith('UP'))}")


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
            print(f"  {book}: {len(rows)} games read, but the server didn't answer ({type(exc).__name__}: {exc})." + (
                  " Is the link address still right? It changes when the link restarts: on the server,"
                  " bash ~/analytix/scripts/run.sh token prints the current one; run that command here again"
                  if args.server.startswith("https://") else
                  " Is the tunnel (ssh -N analytix) open? Restart it if the laptop slept or changed network"))


SAVED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "naijaOdds.json")


def saved() -> dict:
    try:
        with open(SAVED, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main() -> None:
    keep = saved()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every", type=float, default=20)
    ap.add_argument("--server", default=keep.get("server") or "http://localhost:8000",
                    help="the public link (https://….trycloudflare.com) or the tunnel (http://localhost:8001)")
    ap.add_argument("--books", default="bet9ja,betking,nairabet,sportybet")
    ap.add_argument("--token", default=keep.get("token") or os.getenv("ANALYTIX_BETS_TOKEN", ""))
    ap.add_argument("--save", action="store_true", help="remember --server and --token in naijaOdds.json beside this script")
    ap.add_argument("--no-intl", action="store_true", help="leave out Bet9ja's international competitions")
    ap.add_argument("--no-extra", action="store_true", help="only NaijaBet_Api's ten leagues: no cups, extra leagues or internationals")
    ap.add_argument("--bet9ja-groups", default="", help="Bet9ja competition ids (or their addresses), comma-separated")
    ap.add_argument("--find", action="store_true", help="list the international competitions found; send nothing")
    ap.add_argument("--try-code", action="store_true", help="make one SportyBet booking code and print SportyBet's answer")
    ap.add_argument("--try-vet", default="", metavar="CODE", help="load one SportyBet booking code and print what came back")
    args = ap.parse_args()
    if args.try_code:
        return try_code()
    if args.try_vet:
        return try_vet(args.try_vet)
    args.server = args.server.rstrip("/")
    if args.server.endswith("/bets"):                             # the page's address pasted as is
        args.server = args.server[:-len("/bets")]
    if args.save:
        with open(SAVED, "w", encoding="utf-8") as fh:
            json.dump({"server": args.server, "token": args.token}, fh)
        print(f"saved: from now on just run  python naijaOdds.py --loop  (sending to {args.server})")
    print(f"sending to {args.server}" + (" with a token" if args.token else ""))
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
        session = sporty_session() if "sportybet" in args.books else None
        wake = time.time() + args.every * 60
        if session:
            print(time.strftime("%H:%M:%S"), "  making SportyBet codes and loading tickets the page asks for, until the next read at",
                  time.strftime("%H:%M", time.localtime(wake)))
        while time.time() < wake:                                 # between reads: the page's SportyBet code requests
            if session:
                serve_codes(args, session)
                serve_vets(args, session)
            time.sleep(min(15, max(0, wake - time.time())))


if __name__ == "__main__":
    main()

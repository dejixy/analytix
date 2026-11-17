"""
Football data for the bets suite (engine/betting.py), from free sources:

    football-data.co.uk   every game of three seasons per league as CSV: results, shots on target, and odds from
                          Pinnacle and the market average (opening and closing); fixtures.csv has the coming games with
                          their odds. Free, no key. Updated about twice a week
    The Odds API          live odds for the coming games from Pinnacle, the Betfair exchange and other European books
                          (ANALYTIX_ODDS_API_KEY, free at the-odds-api.com: 500 credits a month; one league read is one
                          credit, scores two). Used as the sharp price when there is a key, results too
    Nigerian books        Bet9ja, BetKing and Nairabet only answer Nigerian addresses, so their odds are read on a laptop
                          in Nigeria (scripts/naijaOdds.py) and sent to the server through the SSH tunnel
"""
import asyncio
import csv
import io
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from engine import betting as B

FD_SEASON = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FD_FIXTURES = "https://www.football-data.co.uk/fixtures.csv"
ODDS_API = "https://api.the-odds-api.com/v4/sports/{sport}/{what}"
UA = {"User-Agent": "Mozilla/5.0 (Analytix football)"}
LONDON = ZoneInfo("Europe/London")

# football-data code: (name, The Odds API sport key)
LEAGUES = {
    "E0": ("Premier League", "soccer_epl"), "E1": ("Championship", "soccer_efl_champ"),
    "E2": ("League One", "soccer_england_league1"), "E3": ("League Two", "soccer_england_league2"),   # play through breaks
    "SP1": ("La Liga", "soccer_spain_la_liga"), "I1": ("Serie A", "soccer_italy_serie_a"),
    "D1": ("Bundesliga", "soccer_germany_bundesliga"), "F1": ("Ligue 1", "soccer_france_ligue_one"),
    "N1": ("Eredivisie", "soccer_netherlands_eredivisie"), "P1": ("Primeira Liga", "soccer_portugal_primeira_liga"),
    "SC0": ("Scottish Premiership", "soccer_spl"), "B1": ("Belgian Pro League", "soccer_belgium_first_div"),
    "T1": ("Super Lig", "soccer_turkey_super_league"), "G1": ("Greek Super League", "soccer_greece_super_league"),
}
CUPS = {"UCL": ("Champions League", "soccer_uefa_champs_league"), "UEL": ("Europa League", "soccer_uefa_europa_league")}
SHARP_BOOKS = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "matchbook")


def num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def season_codes(now: datetime, n: int = 3) -> list[str]:
    """football-data's season folders, newest first: "2627" for 2026/27 (a season starts in July)."""
    y = now.year if now.month >= 7 else now.year - 1
    return [f"{(y - k) % 100:02d}{(y - k + 1) % 100:02d}" for k in range(n)]


def _when(date: str, time: str | None) -> int | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            d = datetime.strptime(date.strip(), fmt)
            break
        except (ValueError, AttributeError):
            d = None
    if d is None:
        return None
    hh, mm = 15, 0
    if time and ":" in time:
        try:
            hh, mm = (int(x) for x in time.strip().split(":")[:2])
        except ValueError:
            pass
    return int(d.replace(hour=hh, minute=mm, tzinfo=LONDON).timestamp() * 1000)


ODDS_COLS = ("PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA", "AvgH", "AvgD", "AvgA", "AvgCH", "AvgCD", "AvgCA",
             "B365H", "B365D", "B365A", "MaxH", "MaxD", "MaxA")


def parse_fd(text: str, league: str | None = None) -> list[dict]:
    """A football-data CSV as games: {league, home, away, t_ms, hg, ag (None if not played), hst, ast, odds columns}."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        home, away = (r.get("HomeTeam") or r.get("Home") or "").strip(), (r.get("AwayTeam") or r.get("Away") or "").strip()
        t = _when(r.get("Date") or "", r.get("Time"))
        if not home or not away or t is None:
            continue
        hg, ag = num(r.get("FTHG") if r.get("FTHG") not in (None, "") else r.get("HG")), num(r.get("FTAG") if r.get("FTAG") not in (None, "") else r.get("AG"))
        g = {"league": league or (r.get("Div") or "").strip(), "home": home, "away": away, "t_ms": t,
             "hg": int(hg) if hg is not None else None, "ag": int(ag) if ag is not None else None,
             "hst": num(r.get("HST")), "ast": num(r.get("AST"))}
        for c in ODDS_COLS:
            v = num(r.get(c))
            if v:
                g[c] = v
        out.append(g)
    return out


async def fetch_history(codes=None, seasons: int = 3, pause_s: float = 0.6) -> tuple[list[dict], list[str]]:
    """Three seasons of every league: (games, errors)."""
    games, errors = [], []
    async with httpx.AsyncClient(timeout=40, headers=UA, follow_redirects=True) as c:
        for code in codes or LEAGUES:
            for s in season_codes(datetime.now(timezone.utc), seasons):
                try:
                    r = await c.get(FD_SEASON.format(season=s, code=code))
                    if r.status_code == 200 and "HomeTeam" in r.text[:400]:
                        games += parse_fd(r.content.decode("utf-8-sig", errors="replace"), code)
                    elif r.status_code != 404:
                        errors.append(f"{code} {s}: HTTP {r.status_code}")
                except httpx.HTTPError as exc:
                    errors.append(f"{code} {s}: {type(exc).__name__}")
                await asyncio.sleep(pause_s)
    return games, errors


async def fetch_fixtures() -> list[dict]:
    async with httpx.AsyncClient(timeout=40, headers=UA, follow_redirects=True) as c:
        r = await c.get(FD_FIXTURES)
        r.raise_for_status()
        return parse_fd(r.content.decode("utf-8-sig", errors="replace"))


def sharp_from_fd(fixtures: list[dict], read_ms: int) -> list[dict]:
    """football-data's coming games as sharp prices: Pinnacle's, else the market average."""
    out = []
    for g in fixtures:
        if g.get("hg") is not None or g["league"] not in LEAGUES:
            continue
        for h, d, a, src in (("PSH", "PSD", "PSA", "Pinnacle (football-data)"), ("AvgH", "AvgD", "AvgA", "market average (football-data)")):
            fair = B.devig({"1": g.get(h), "X": g.get(d), "2": g.get(a)})
            if fair:
                out.append({"id": f"fd:{g['league']}:{B.norm(g['home'])}:{B.norm(g['away'])}:{g['t_ms'] // B.DAY}",
                            "league": g["league"], "league_name": LEAGUES[g["league"]][0], "home": g["home"], "away": g["away"],
                            "t_ms": g["t_ms"], "fair": fair, "src": src, "read_ms": read_ms})
                break
    return out


# ── The Odds API ─────────────────────────────────────────────────────────────
def parse_odds_api(events: list, league: str, league_name: str, read_ms: int) -> list[dict]:
    """The coming games of one league with a sharp fair price: Pinnacle, else the Betfair exchange or Matchbook, else
    the median of every European book's fair chances."""
    out = []
    for e in events if isinstance(events, list) else []:
        home, away = e.get("home_team"), e.get("away_team")
        try:
            t = int(datetime.fromisoformat(str(e.get("commence_time")).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        per = {}
        for bk in e.get("bookmakers") or []:
            for m in bk.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                o = {x.get("name"): num(x.get("price")) for x in m.get("outcomes") or []}
                fair = B.devig({"1": o.get(home), "X": o.get("Draw"), "2": o.get(away)})
                if fair:
                    per[bk.get("key")] = fair
        fair, src = None, ""
        for k in SHARP_BOOKS:
            if k in per:
                fair, src = per[k], {"pinnacle": "Pinnacle", "matchbook": "Matchbook"}.get(k, "Betfair exchange")
                break
        if not fair and len(per) >= 3:
            ps = sorted(per.values(), key=lambda f: f["1"])
            mid = ps[len(ps) // 2]
            fair, src = mid, f"median of {len(per)} books"
        if fair and home and away:
            out.append({"id": f"oa:{e.get('id')}", "league": league, "league_name": league_name, "home": home, "away": away,
                        "t_ms": t, "fair": fair, "src": f"{src} (The Odds API)", "read_ms": read_ms})
    return out


def parse_scores(events: list) -> list[dict]:
    out = []
    for e in events if isinstance(events, list) else []:
        if not e.get("completed"):
            continue
        sc = {x.get("name"): num(x.get("score")) for x in e.get("scores") or []}
        hg, ag = sc.get(e.get("home_team")), sc.get(e.get("away_team"))
        try:
            t = int(datetime.fromisoformat(str(e.get("commence_time")).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        if hg is not None and ag is not None:
            out.append({"home": e["home_team"], "away": e["away_team"], "t_ms": t, "hg": int(hg), "ag": int(ag)})
    return out


async def fetch_odds_api(sports: dict[str, tuple[str, str]], what: str = "odds", key: str | None = None,
                         pause_s: float = 1.0) -> tuple[dict[str, list], int | None, list[str]]:
    """({league: raw events}, credits left, errors) for each league ({league: (name, sport key)})."""
    key = key or os.getenv("ANALYTIX_ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("no key: set ANALYTIX_ODDS_API_KEY (free at the-odds-api.com)")
    got, left, errors = {}, None, []
    params = {"apiKey": key}
    if what == "odds":
        params.update(regions="eu", markets="h2h", oddsFormat="decimal")
    else:
        params.update(daysFrom=3)
    async with httpx.AsyncClient(timeout=30, headers=UA) as c:
        for league, (_name, sport) in sports.items():
            try:
                r = await c.get(ODDS_API.format(sport=sport, what=what), params=params)
                if r.headers.get("x-requests-remaining"):
                    left = int(float(r.headers["x-requests-remaining"]))
                if r.status_code == 200:
                    got[league] = r.json()
                elif r.status_code in (401, 429):
                    errors.append(f"{league}: HTTP {r.status_code}")
                    break
                elif r.status_code != 404:
                    errors.append(f"{league}: HTTP {r.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{league}: {type(exc).__name__}")
            await asyncio.sleep(pause_s)
    return got, left, errors


# ── what the laptop sends ────────────────────────────────────────────────────
def parse_push(payload: dict) -> tuple[str, list[dict]]:
    """A push from scripts/naijaOdds.py: (book, [{home, away, t_ms, league, odds: {market: price}}])."""
    book = str(payload.get("book") or "").strip()[:30]
    rows = []
    for r in payload.get("rows") or []:
        try:
            odds = {B.BOOK_KEYS[k]: float(v) for k, v in (r.get("odds") or {}).items() if k in B.BOOK_KEYS and v and float(v) > 1}
            if r.get("home") and r.get("away") and odds:
                rows.append({"home": str(r["home"])[:60], "away": str(r["away"])[:60], "t_ms": int(r["t_ms"]),
                             "league": str(r.get("league") or "")[:60], "odds": odds})
        except (TypeError, ValueError, KeyError):
            continue
    return book, rows

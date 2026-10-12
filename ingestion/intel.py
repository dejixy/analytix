"""
Independent information for prediction markets, from public sources that need no keys.

    Deribit   options on BTC, ETH and SOL: every strike and expiry with its implied volatility
              GET /api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option
    ESPN      scoreboards: teams, score, clock, status, the pre-game sportsbook line, and for American football a
              live win probability
              GET site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD
    GDELT     a global index of news articles: how many mention a name every 15 minutes, and the latest headlines
              GET api.gdeltproject.org/api/v2/doc/doc?query=…&mode=TimelineVolRaw|ArtList&format=json
              (GDELT asks for at most one request every 5 seconds; api/intel.py keeps to that)

Every parser is defensive: a missing field gives None rather than an error, and scripts/probeIntel.py shows
what each source actually returns.
"""
import json
from datetime import datetime, timezone
from typing import Any

import httpx

DERIBIT = "https://www.deribit.com/api/v2/public"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Analytix/1.0", "Accept": "application/json"}


async def _get(url: str, params: dict | None = None, timeout: float = 15) -> Any:
    async with httpx.AsyncClient(timeout=timeout, headers=HEADERS, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            try:
                return json.loads(r.text, strict=False)
            except ValueError as exc:
                raise ValueError(f"not JSON: {r.text[:160]!r}") from exc


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


# ── Deribit ─────────────────────────────────────────────────────────────────
DERIBIT_CURRENCY = {"BTC": "BTC", "ETH": "ETH", "SOL": "USDC"}     # SOL options are USDC-settled


async def fetch_options(asset: str) -> list[dict]:
    js = await _get(f"{DERIBIT}/get_book_summary_by_currency", {"currency": DERIBIT_CURRENCY[asset], "kind": "option"})
    rows = (js or {}).get("result") or []
    if DERIBIT_CURRENCY[asset] == "USDC":
        rows = [r for r in rows if str(r.get("instrument_name", "")).startswith(f"{asset}_USDC-")]
    return rows


# ── ESPN ────────────────────────────────────────────────────────────────────
def _ms(iso: str | None) -> int | None:
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return int(datetime.strptime(iso, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _ml(side: dict | None) -> float | None:
    """A moneyline in either of ESPN's shapes: {"moneyLine": -150} or {"close": {"odds": "-150"}}."""
    if not isinstance(side, dict):
        return None
    if side.get("moneyLine") is not None:
        return _f(side["moneyLine"])
    for k in ("close", "current", "open"):
        v = (side.get(k) or {}).get("odds") if isinstance(side.get(k), dict) else None
        if v is not None:
            v = str(v).upper().replace("EVEN", "100").replace("+", "")
            if _f(v) is not None:
                return _f(v)
    return None


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _first(x) -> dict:
    return _d(x[0]) if isinstance(x, list) and x else {}


def _score(c: dict) -> int | None:
    v = c.get("score")
    if isinstance(v, dict):                       # some feeds nest it: {"value": 2.0, "displayValue": "2"}
        v = v.get("value", v.get("displayValue"))
    f = _f(v)
    return int(f) if f is not None else None


def parse_event(ev: dict, sport: str, league: str) -> dict | None:
    comp = _first(ev.get("competitions"))
    teams = {_d(c).get("homeAway"): _d(c) for c in comp.get("competitors") or []}
    if "home" not in teams or "away" not in teams:
        return None
    status = _d(comp.get("status")) or _d(ev.get("status"))
    st = _d(status.get("type"))
    odds_raw = _first(comp.get("odds"))
    ml = _d(odds_raw.get("moneyline"))
    odds = {"home_ml": _ml(odds_raw.get("homeTeamOdds")) or _ml(ml.get("home")),
            "away_ml": _ml(odds_raw.get("awayTeamOdds")) or _ml(ml.get("away")),
            "draw_ml": _ml(odds_raw.get("drawOdds")) or _ml(ml.get("draw")),
            "provider": _d(odds_raw.get("provider")).get("name"), "details": odds_raw.get("details")}
    prob = _d(_d(_d(comp.get("situation")).get("lastPlay")).get("probability"))
    win_prob = None
    if prob.get("homeWinPercentage") is not None:
        win_prob = {"home": _f(prob.get("homeWinPercentage")), "away": _f(prob.get("awayWinPercentage")),
                    "tie": _f(prob.get("tiePercentage"))}
    link = next((_d(l).get("href") for l in ev.get("links") or [] if "summary" in (_d(l).get("rel") or [])), None)
    return {
        "id": str(ev.get("id")), "name": ev.get("name"), "sport": sport, "league": league,
        "start_ms": _ms(ev.get("date")), "state": st.get("state"), "completed": bool(st.get("completed")),
        "detail": st.get("shortDetail") or st.get("detail"), "clock": status.get("displayClock"),
        "period": status.get("period"),
        "home": {"team": _d(teams["home"].get("team"))}, "away": {"team": _d(teams["away"].get("team"))},
        "home_score": _score(teams["home"]), "away_score": _score(teams["away"]),
        "odds": odds, "win_prob": win_prob, "link": link,
    }


def parse_scoreboard(js: dict, sport: str, league: str) -> list[dict]:
    """One league's scoreboard → games. An event in an unexpected shape is skipped, not fatal."""
    games = []
    for ev in _d(js).get("events") or []:
        try:
            g = parse_event(_d(ev), sport, league)
        except Exception:                               # one odd event must not lose the whole board
            g = None
        if g:
            games.append(g)
    return games


async def fetch_scoreboard(sport: str, league: str, date: str | None = None) -> list[dict]:
    js = await _get(f"{ESPN}/{sport}/{league}/scoreboard", {"dates": date} if date else None)
    return parse_scoreboard(js, sport, league)


# ── GDELT ───────────────────────────────────────────────────────────────────
def _gdelt_ms(s: str | None) -> int | None:
    try:
        return int(datetime.strptime(str(s), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


async def fetch_news_volume(query: str, timespan: str = "4d") -> list[tuple[int, float]]:
    js = await _get(GDELT, {"query": query, "mode": "TimelineVolRaw", "format": "json", "timespan": timespan})
    series = (js or {}).get("timeline") or []
    data = series[0].get("data") if series else []
    out = []
    for d in data or []:
        t = _gdelt_ms(d.get("date"))
        if t is not None:
            out.append((t, _f(d.get("value")) or 0.0))
    return sorted(out)


async def fetch_headlines(query: str, n: int = 6, timespan: str = "2d") -> list[dict]:
    js = await _get(GDELT, {"query": query, "mode": "ArtList", "format": "json", "maxrecords": n, "sort": "DateDesc",
                            "timespan": timespan})
    out = []
    for a in (js or {}).get("articles") or []:
        if a.get("url") and a.get("title"):
            out.append({"title": a["title"], "url": a["url"], "domain": a.get("domain"), "seen_ms": _gdelt_ms(a.get("seendate"))})
    return out

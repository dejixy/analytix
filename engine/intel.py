"""
Outside information for prediction markets, turned into numbers a trader can act on. Pure functions.

    matching     a Hyperliquid match ("RB Leipzig vs PSV Eindhoven") ↔ the real game on ESPN, by team names (and
                 their short codes, e.g. PIT) and start time
    references   the chance from an independent source for each outcome: the options market, the live game
                 (score, ESPN's model, the goals model), the sportsbook line before kick-off
    edge         what a trade is worth against that chance, at the prices you can actually get:
                     EV per $1          p / price - 1
                     Kelly fraction     (p - price) / (1 - price)     the bankroll share that grows money fastest
                                                                       if p is right; a quarter of it is shown,
                                                                       since p is an estimate
                     return if right    1 / price - 1, and per year given the days to the close
    news         a spike in coverage of the teams or names in a market, against their own normal, plus keyword
                 flags (injury, suspended, resigns…) from the headlines
    rules        how the market settles, in plain words
"""
import re
import unicodedata
from datetime import datetime, timezone

DROP_WORDS = {"fc", "cf", "afc", "sc", "the", "club", "de", "calcio", "ac", "as", "ss", "ssc", "cd", "rc", "sv", "vfb",
              "vfl", "tsg", "bk", "fk", "if", "sk", "cp", "ud", "rcd"}   # club-type words, not part of what tells teams apart
MATCH_AT = 0.75

COMPETITIONS = {                       # Hyperliquid's competition names → ESPN scoreboard (sport, league)
    "nfl": ("football", "nfl"), "national football league": ("football", "nfl"),
    "ncaa football": ("football", "college-football"), "college football": ("football", "college-football"),
    "nba": ("basketball", "nba"), "wnba": ("basketball", "wnba"), "ncaa basketball": ("basketball", "mens-college-basketball"),
    "mlb": ("baseball", "mlb"), "major league baseball": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"), "national hockey league": ("hockey", "nhl"),
    "uefa champions league": ("soccer", "uefa.champions"), "champions league": ("soccer", "uefa.champions"),
    "uefa europa league": ("soccer", "uefa.europa"), "europa league": ("soccer", "uefa.europa"),
    "uefa conference league": ("soccer", "uefa.europa.conf"),
    "english premier league": ("soccer", "eng.1"), "premier league": ("soccer", "eng.1"),
    "la liga": ("soccer", "esp.1"), "laliga": ("soccer", "esp.1"), "serie a": ("soccer", "ita.1"),
    "bundesliga": ("soccer", "ger.1"), "ligue 1": ("soccer", "fra.1"), "eredivisie": ("soccer", "ned.1"),
    "primeira liga": ("soccer", "por.1"), "mls": ("soccer", "usa.1"), "major league soccer": ("soccer", "usa.1"),
    "fa cup": ("soccer", "eng.fa"), "efl championship": ("soccer", "eng.2"), "championship": ("soccer", "eng.2"),
    "fifa world cup": ("soccer", "fifa.world"), "world cup": ("soccer", "fifa.world"),
    "uefa nations league": ("soccer", "uefa.nations"), "copa libertadores": ("soccer", "conmebol.libertadores"),
}
FLAGS = {
    "injury": r"\b(injur\w*|ruled out|out for|sidelined|hamstring|acl|concussion|doubtful|questionable)\b",
    "suspended": r"\b(suspend\w*|ban(?:ned)?|red card|sent off)\b",
    "leaves": r"\b(resign\w*|steps? down|sacked|fired|quits?|withdraw\w*|drops? out)\b",
    "legal": r"\b(arrest\w*|charged|indict\w*|lawsuit|sued|investigat\w*|probe)\b",
    "delay": r"\b(postpon\w*|delay\w*|cancel\w*|called off|rescheduled|weather)\b",
    "deal": r"\b(acquir\w*|merger|takeover|ipo|files? for|listing|approv\w*)\b",
}


# ── names ───────────────────────────────────────────────────────────────────
def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def tokens(s: str | None) -> set[str]:
    words = norm(s).split()
    return {w for w in words if w not in DROP_WORDS and len(w) > 1} or set(words)


def name_score(a: str | None, b: str | None) -> float:
    """How surely two team names mean the same team, 0..1: shared words over the longer name, so "Manchester City"
    and "Manchester United" only score 0.5."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def team_match(name: str | None, short: str | None, team: dict) -> float:
    """A Hyperliquid participant (name, short code) against an ESPN team {displayName, shortDisplayName, name,
    location, abbreviation}."""
    if short and team.get("abbreviation") and norm(short) == norm(team["abbreviation"]):
        return 1.0
    return max(name_score(name, team.get(k)) for k in ("displayName", "shortDisplayName", "name", "location"))


def league_for(competition: str | None) -> tuple[str, str] | None:
    c = norm(competition)
    if not c:
        return None
    if c in COMPETITIONS:
        return COMPETITIONS[c]
    for k, v in COMPETITIONS.items():
        if len(k) > 4 and k in c:
            return v
    return None


def match_game(a: str, b: str, short_a: str | None, short_b: str | None, start_ms: int | None,
               games: list[dict]) -> tuple[dict, bool] | None:
    """The ESPN game between participants A and B (either way round) starting within 36 hours of start_ms.
    Returns (game, a_is_home)."""
    best, best_score = None, 0.0
    for g in games:
        home, away = g.get("home") or {}, g.get("away") or {}
        if start_ms and g.get("start_ms") and abs(g["start_ms"] - start_ms) > 36 * 3_600_000:
            continue
        for a_home in (True, False):
            ta, tb = (home, away) if a_home else (away, home)
            s = min(team_match(a, short_a, ta.get("team") or {}), team_match(b, short_b, tb.get("team") or {}))
            if s > best_score:
                best, best_score = (g, a_home), s
    return best if best_score >= MATCH_AT else None


# ── what the game says ──────────────────────────────────────────────────────
def game_probs(g: dict, a_home: bool) -> tuple[dict | None, str, str]:
    """(chances {"a", "b", "draw"} for participant A / B / a draw, source id, plain label) from one ESPN game:
    the final result, ESPN's live model, the goals model, or the pre-game sportsbook line."""
    from engine.inplay import american_to_prob, devig, soccer_live, soccer_minute
    state = g.get("state")
    hs, as_ = g.get("home_score"), g.get("away_score")

    def flip(ph, pd, pa):
        return {"a": ph if a_home else pa, "b": pa if a_home else ph, "draw": pd}

    if state == "post" and g.get("completed") and hs is not None and as_ is not None:
        ph, pd, pa = (1.0, 0.0, 0.0) if hs > as_ else (0.0, 1.0, 0.0) if hs == as_ else (0.0, 0.0, 1.0)
        return flip(ph, pd, pa), "final", f"Final score {hs}–{as_}"
    odds = g.get("odds") or {}
    pre = devig([american_to_prob(odds.get("home_ml")), american_to_prob(odds.get("away_ml"))]
                + ([american_to_prob(odds.get("draw_ml"))] if odds.get("draw_ml") is not None else []))
    if state == "in":
        wp = g.get("win_prob")
        if wp and wp.get("home") is not None:
            ph, pa = float(wp["home"]), float(wp.get("away") if wp.get("away") is not None else 1 - wp["home"])
            pd = float(wp.get("tie") or 0.0)
            return flip(ph, pd, pa), "espn", f"ESPN live model at {g.get('detail') or 'the latest play'}"
        if g.get("sport") == "soccer" and pre and len(pre) == 3 and hs is not None and as_ is not None:
            minute = soccer_minute(g.get("clock"), g.get("period"), g.get("detail"))
            if minute is not None:
                ph, pd, pa = soccer_live((pre[0], pre[2], pre[1]), minute, hs, as_)
                return flip(ph, pd, pa), "goals", f"Goals model: {hs}–{as_} at {int(minute)}'"
        return None, "", ""
    if state == "pre" and pre:
        if len(pre) == 3:
            return flip(pre[0], pre[2], pre[1]), "book", f"Sportsbook line ({odds.get('provider') or 'pre-game'})"
        return flip(pre[0], 0.0, pre[1]), "book", f"Sportsbook line ({odds.get('provider') or 'pre-game'})"
    return None, "", ""


def game_chip(g: dict, a_home: bool) -> dict:
    """What the card shows: LIVE 67' 1–0 / FINAL 27–13 / starts …, with A's score first."""
    hs, as_ = g.get("home_score"), g.get("away_score")
    sa, sb = (hs, as_) if a_home else (as_, hs)
    home, away = (g.get("home") or {}).get("team") or {}, (g.get("away") or {}).get("team") or {}
    ta, tb = (home, away) if a_home else (away, home)
    return {"state": g.get("state"), "detail": g.get("detail") or "", "score_a": sa, "score_b": sb,
            "a": ta.get("abbreviation") or ta.get("shortDisplayName"), "b": tb.get("abbreviation") or tb.get("shortDisplayName"),
            "start_ms": g.get("start_ms"), "completed": bool(g.get("completed"))}


# ── edge ────────────────────────────────────────────────────────────────────
def best_trade(p: float | None, bid: float | None, bid_sz: float, ask: float | None, ask_sz: float,
               days: float | None) -> dict | None:
    """The better of buying Yes at the ask or No at 1 - bid, against chance p. None when neither has an edge."""
    if p is None:
        return None
    options = []
    if ask is not None and 0 < ask < 1:
        options.append(("Yes", ask, p, ask_sz))
    if bid is not None and 0 < bid < 1:
        options.append(("No", 1 - bid, 1 - p, bid_sz))
    best = None
    for side, price, q, size in options:
        ev = q / price - 1
        if ev <= 0:
            continue
        kelly = (q - price) / (1 - price)
        t = {"side": side, "price": price, "p": q, "ev": ev, "kelly": kelly, "quarter_kelly": kelly / 4,
             "contracts": size, "usd": size * price, **returns(price, days)}
        if best is None or ev > best["ev"]:
            best = t
    return best


def returns(price: float | None, days: float | None) -> dict:
    """What $1 makes if this side is right: in total, and per year at that pace (simple, not compounded)."""
    if not price or price <= 0 or price >= 1:
        return {"if_right": None, "per_year": None}
    r = 1 / price - 1
    return {"if_right": r, "per_year": r * 365 / days if days and days > 1 / 24 else None}


# ── news ────────────────────────────────────────────────────────────────────
def news_spike(points: list[tuple[int, float]], now_ms: int, window_h: float = 2.0, base_days: float = 3.0) -> dict | None:
    """GDELT's article counts (every 15 minutes) → coverage over the last `window_h` hours against the typical
    `window_h`-hour stretch of the days before. {"recent", "usual", "ratio"}."""
    if not points:
        return None
    w = window_h * 3_600_000
    recent = sum(v for t, v in points if t > now_ms - w)
    older = [(t, v) for t, v in points if now_ms - w - base_days * 86_400_000 <= t <= now_ms - w]
    if not older:
        return {"recent": recent, "usual": None, "ratio": None}
    buckets: dict[int, float] = {}
    for t, v in older:
        buckets[int((now_ms - w - t) // w)] = buckets.get(int((now_ms - w - t) // w), 0.0) + v
    n = max(1, int(base_days * 86_400_000 // w))
    vals = sorted([buckets.get(i, 0.0) for i in range(n)])
    usual = vals[len(vals) // 2]
    return {"recent": recent, "usual": usual, "ratio": recent / max(usual, 1.0)}


def headline_flags(titles: list[str]) -> list[str]:
    found = []
    for name, pat in FLAGS.items():
        if any(re.search(pat, t or "", re.I) for t in titles):
            found.append(name)
    return found


def news_query(names: list[str]) -> str | None:
    """GDELT search for a market's names: quoted, OR-ed, English sources."""
    names = [n for n in dict.fromkeys(n.strip() for n in names if n and len(n.strip()) > 2)][:4]
    if not names:
        return None
    q = " OR ".join(f'"{n}"' for n in names)
    return f"({q}) sourcelang:english" if len(names) > 1 else f"{q} sourcelang:english"


# ── rules ───────────────────────────────────────────────────────────────────
def rules_line(f: dict, kind: str) -> str | None:
    """How the market settles, in plain words, from its listing."""
    parts = []
    if kind in ("price", "touch"):
        if f.get("priceDescription"):
            parts.append(f"settles on the {f['priceDescription']}")
        if f.get("seconds"):
            parts.append(f"window {f['seconds']}s")
    if f.get("countedPlay"):
        parts.append(f"counts {f['countedPlay']}")
    if f.get("officialSource"):
        parts.append(f"source: {f['officialSource']}")
    if f.get("resolutionDeadline"):
        try:
            d = datetime.strptime(f["resolutionDeadline"], "%Y%m%d-%H%M").replace(tzinfo=timezone.utc)
            parts.append(f"settles by {d.strftime('%-d %b %H:%M UTC')}")
        except ValueError:
            pass
    return ("Rules: " + " · ".join(parts) + ".") if parts else None

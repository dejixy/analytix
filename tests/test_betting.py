"""The bets suite: names, fair prices, history, stats, the value list, the rollover pick, the paper book, readers."""
import asyncio
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.bets import BetsService
from engine import betting as B
from ingestion import football as F

H, D = B.HOUR, B.DAY
NOW = int(datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_names_match_across_sources():
    for a, b in [("Manchester United", "Man United"), ("Man Utd", "Man United"), ("Nottingham Forest", "Nott'm Forest"),
                 ("Atlético Madrid", "Ath Madrid"), ("Bayern Munich", "Bayern Munchen"), ("Paris Saint-Germain", "Paris SG"),
                 ("Inter Milan", "Inter"), ("Tottenham Hotspur", "Tottenham"), ("Wolverhampton Wanderers", "Wolves")]:
        assert B.same_team(a, b) >= 0.92, (a, b)
    assert B.same_team("Arsenal", "Chelsea") < 0.5 and B.same_team("Man City", "Man United") < 0.72
    fx = [{"home": "Man United", "away": "Chelsea", "t_ms": NOW + 5 * H}, {"home": "Man City", "away": "Chelsea", "t_ms": NOW + 5 * H}]
    assert B.match_fixture("Manchester United", "Chelsea FC", NOW + 5 * H + 600_000, fx) is fx[0]
    assert B.match_fixture("Manchester United", "Chelsea", NOW + 3 * D, fx) is None                    # a different day


def test_fair_prices_and_markets():
    p = B.devig({"1": 1.25, "X": 6.0, "2": 12.0})
    assert abs(sum(p.values()) - 1) < 1e-12 and p["1"] > 0.75
    m = B.markets_from_1x2(p)
    assert abs(m["1X"] - (p["1"] + p["X"])) < 1e-12 and abs(m["12"] + m["X"] - 1) < 1e-12
    assert B.devig({"1": 1.5, "X": None, "2": 3}) is None
    assert B.wins("1X", 1, 1) and B.wins("12", 0, 2) and not B.wins("X2", 2, 0)


def _history(n=900, seed=7, home_bonus=0.05):
    """Seasons of games where home favourites at fair ~0.78 actually win ~0.78 + home_bonus."""
    rnd = random.Random(seed)
    teams = ["Big City", "Rovers", "United", "Athletic", "Wanderers", "Town", "Albion", "Villa"]
    out = []
    for k in range(n):
        home, away = rnd.sample(teams, 2)
        fav = home == "Big City"
        oh, od, oa = (1.25, 6.0, 11.0) if fav else (2.6, 3.3, 2.8)
        p = B.devig({"1": oh, "X": od, "2": oa})
        r = rnd.random()
        ph = p["1"] + (home_bonus if fav else 0)
        hg, ag = (2, 0) if r < ph else (1, 1) if r < ph + p["X"] * 0.8 else (0, 1)
        out.append({"league": "E0", "home": home, "away": away, "t_ms": NOW - (n - k) * D // 3, "hg": hg, "ag": ag,
                    "hst": 7 if home == "Big City" else 4, "ast": 2 if home == "Big City" else 4, "PSH": oh, "PSD": od, "PSA": oa})
    return out


def test_history_adjusts_the_chance_but_only_a_little():
    hist = _history()
    cal = B.calibration(hist)
    p = B.devig({"1": 1.25, "X": 6.0, "2": 11.0})["1"]
    adj, pat = B.adjust(p, "1", "E0", cal)
    assert pat["scope"] == "E0" and pat["rate"] > pat["implied"]                     # they won more than the price said
    assert p < adj < p + (pat["rate"] - pat["implied"])                              # moved toward it, shrunk
    assert B.adjust(0.30, "1", "E0", cal) == (0.30, None)                            # outside the favourite buckets
    assert B.adjust(p, "1", "E0", cal, "avg") == (p, None)                         # no history on average prices here
    assert B.price_kind("Pinnacle (The Odds API)") == "sharp" and B.price_kind("market average (football-data)") == "avg"
    two = B.calibration([{**g, "AvgH": 1.30, "AvgD": 5.0, "AvgA": 8.0} for g in hist])
    assert ("sharp", "*", "1", B.bucket_of(p)) in two and ("avg", "*", "1", B.bucket_of(B.devig({"1": 1.3, "X": 5, "2": 8})["1"])) in two
    rec = B.team_as_favourite(hist, "Big City")
    assert rec["n"] > 50 and rec["won"] / rec["n"] > 0.7 and rec["won"] + rec["drew"] <= rec["n"]
    games = [(g, side) for g, side in B.team_games(hist, "Big City") if B.hist_fair(g)[side] >= 0.65]
    assert rec["drew"] == sum(g["hg"] == g["ag"] for g, _ in games)
    f = B.form(hist, "Big City", NOW)
    teams = B.Teams(hist)
    assert B.form(hist, "Big City", NOW, teams=teams) == f and B.team_as_favourite(hist, "Big City", teams=teams) == rec
    assert B.form(hist, "Big City FC", NOW, teams=teams) == f and B.form(hist, "Harbour Wolves", NOW, teams=teams) is None
    assert B.same_team("Man City", "Man United", 0.72) < 0.72 and B.same_team("Man Utd", "Manchester United", 0.92) == 1.0
    assert f["n"] == 6 and f["sot_for"] > f["sot_against"] - 1
    ok, why = B.stats_agree({"ppg": 2.5, "sot_for": 7, "sot_against": 2, "results": "WWDWWW"},
                            {"ppg": 1.0, "sot_for": 3, "sot_against": 5, "results": "LDLWLL"})
    assert ok and "points a game" in why
    bad, why = B.stats_agree({"ppg": 1.5, "sot_for": 4, "sot_against": 5, "results": "LWLW"},
                             {"ppg": 1.8, "sot_for": 5, "sot_against": 4, "results": "WWDW"})
    assert bad is False and "lost 2" in why and "fewer points" in why
    assert B.stats_agree(None, f) == (None, "not enough recent games to judge")


def _sharp(home, away, t, oh, od, oa, src="Pinnacle", read=NOW - H, sid=None):
    return {"id": sid or f"s:{home}", "league": "E0", "league_name": "Premier League", "home": home, "away": away, "t_ms": t,
            "fair": B.devig({"1": oh, "X": od, "2": oa}), "src": src, "read_ms": read}


def test_value_list_and_rollover_pick():
    hist = _history(home_bonus=0.0)
    cal = B.calibration(hist)
    sharp = [_sharp("Big City", "Rovers", NOW + 8 * H, 1.20, 7.0, 15.0), _sharp("Town", "Albion", NOW + 2 * D, 1.45, 4.5, 7.0)]
    books = {"bet9ja": {"t_ms": NOW - 20 * 60_000, "rows": [
                {"home": "Big City FC", "away": "Rovers", "t_ms": NOW + 8 * H, "odds": {"1": 1.31, "1X": 1.08}},
                {"home": "Town", "away": "Albion", "t_ms": NOW + 2 * D, "odds": {"1": 1.40}}]},
             "betking": {"t_ms": NOW - 10 * 60_000, "rows": [
                {"home": "Big City", "away": "Rovers", "t_ms": NOW + 8 * H, "odds": {"1": 1.28}}]},
             "nairabet": {"t_ms": NOW - 5 * H, "rows": [                              # stale: read five hours ago
                {"home": "Town", "away": "Albion", "t_ms": NOW + 2 * D, "odds": {"1": 1.60}}]}}
    board = B.build(books, sharp, hist, cal, NOW)
    big = next(x for x in board if x["home"] == "Big City" and x["market"] == "1")
    assert big["book"] == "bet9ja" and big["odds"] == 1.31 and [o["book"] for o in big["others"]] == ["bet9ja", "betking"]
    assert big["fav"] == "Big City" and big["agree"] is True and big["edge"] > 0.02
    vb = B.value_bets(board)
    assert [(x["home"], x["market"]) for x in vb] == [("Big City", "1")]                # town's 1.60 is stale, 1.08 too short
    assert B.value_bets(board, before_ms=NOW + H) == []                                  # kicks off later than that
    pick = B.rollover_pick(board, NOW)
    assert pick["home"] == "Big City" and pick["market"] == "1"
    assert abs(B.chain_odds(0.73, 30) - 0.73 ** 30) < 1e-15


def _bet(key, odds, t, market="1", home="Big City", away="Rovers", p=0.78):
    return {"key": key, "match_id": key, "home": home, "away": away, "league": "E0", "league_name": "PL", "t_ms": t,
            "market": market, "book": "bet9ja", "odds": odds, "fair_p": p, "p": p, "edge": odds * p - 1, "sharp_src": "Pinnacle"}


def test_the_paper_book_follows_flat_bets_and_the_chain(tmp_path):
    bk = B.Book(tmp_path / "b.json")
    assert bk.add_flat(_bet("a", 1.36, NOW + 8 * H), NOW) and bk.add_flat(_bet("a", 1.36, NOW + 8 * H), NOW) is None
    p1 = bk.add_pick(_bet("r1", 1.25, NOW + 8 * H), "2026-11-18", NOW)
    assert p1["stake"] == 5000 and p1["leg"] == 1
    assert bk.add_pick(_bet("r2", 1.25, NOW + D), "2026-11-19", NOW + D) is None       # yesterday's hasn't settled yet
    res = [{"home": "Big City", "away": "Rovers", "t_ms": NOW + 8 * H, "hg": 2, "ag": 0}]
    clos = [{"home": "Big City", "away": "Rovers", "t_ms": NOW + 8 * H, "fair": B.devig({"1": 1.28, "X": 5.8, "2": 10.5})}]
    done = bk.settle(res, clos)
    assert len(done) == 2 and bk.chain["day"] == 1 and bk.chain["balance"] == 6250.0 and bk.flat[0]["profit"] == 90.0
    assert bk.flat[0]["clv"] > 0                                                       # 1.36 beat the fair close
    p2 = bk.add_pick(_bet("r2", 1.20, NOW + D + 8 * H, home="Town", away="Albion"), "2026-11-19", NOW + D)
    assert p2["stake"] == 6250.0 and p2["leg"] == 2
    bk.settle([{"home": "Town", "away": "Albion", "t_ms": NOW + D + 8 * H, "hg": 0, "ag": 1}])
    assert bk.chain == {"n": 2, "balance": 5000.0, "day": 0, "refills": 1, "best": 1}  # lost: a refill, a new chain
    rec = bk.record()
    assert rec["flat"]["n"] == 1 and rec["flat"]["roi"] == 90 / 250 and rec["rollover"]["spent"] == 10_000
    bk.save()
    assert B.Book(tmp_path / "b.json").chain["refills"] == 1
    bk.chain["refills"] = 7
    assert bk.add_pick(_bet("r9", 1.2, NOW + 9 * D), "2026-11-27", NOW + 9 * D) is None # the refills are used up


FD_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HST,AST,PSH,PSD,PSA,PSCH,PSCD,PSCA,AvgH,AvgD,AvgA
E0,15/08/2026,20:00,Liverpool,Bournemouth,4,2,H,10,3,1.30,6.0,9.5,1.28,6.2,10.0,1.29,5.8,9.0
E0,16/08/2026,15:00,Aston Villa,Newcastle,,,,,,2.4,3.4,3.0,,,,2.35,3.3,2.95
"""


def test_readers():
    g = F.parse_fd(FD_CSV, "E0")
    assert g[0]["hg"] == 4 and g[0]["hst"] == 10 and g[0]["PSCH"] == 1.28 and g[1]["hg"] is None
    assert datetime.fromtimestamp(g[0]["t_ms"] / 1000, timezone.utc).hour == 19                    # 20:00 London in August
    s = F.sharp_from_fd(g, NOW)
    assert len(s) == 1 and s[0]["home"] == "Aston Villa" and s[0]["src"].startswith("Pinnacle")
    assert F.season_codes(datetime(2026, 11, 1)) == ["2627", "2526", "2425"] and F.season_codes(datetime(2027, 3, 1))[0] == "2627"
    ev = [{"id": "x1", "commence_time": "2026-11-21T15:00:00Z", "home_team": "Arsenal", "away_team": "Fulham", "bookmakers": [
        {"key": "williamhill", "markets": [{"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 1.3}, {"name": "Fulham", "price": 9},
                                                                     {"name": "Draw", "price": 5.5}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 1.33}, {"name": "Fulham", "price": 10.2},
                                                                  {"name": "Draw", "price": 5.9}]}]}]}]
    oa = F.parse_odds_api(ev, "E0", "Premier League", NOW)
    assert oa[0]["src"].startswith("Pinnacle") and oa[0]["fair"]["1"] > 0.7 and oa[0]["id"] == "oa:x1"
    sc = F.parse_scores([{"completed": True, "home_team": "Arsenal", "away_team": "Fulham", "commence_time": "2026-11-21T15:00:00Z",
                          "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Fulham", "score": "0"}]}, {"completed": False}])
    assert sc == [{"home": "Arsenal", "away": "Fulham", "t_ms": oa[0]["t_ms"], "hg": 2, "ag": 0}]
    book, rows = F.parse_push({"book": "bet9ja", "rows": [{"home": "Arsenal", "away": "Fulham", "t_ms": NOW, "odds": {"home": "1.36", "home_or_draw": 1.07, "x": 3}},
                                                          {"home": "", "away": "B", "t_ms": NOW, "odds": {"home": 2}}]})
    assert book == "bet9ja" and rows == [{"home": "Arsenal", "away": "Fulham", "t_ms": NOW, "league": "", "odds": {"1": 1.36, "1X": 1.07}}]


def test_the_service(tmp_path, monkeypatch):
    rt = SimpleNamespace(mode="live")
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json")
    hist = _history(home_bonus=0.0)
    t = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def history():
        return hist, ["E1 2627: HTTP 500"]

    async def fixtures():
        return [{"league": "E0", "home": "Big City", "away": "Rovers", "t_ms": t + 8 * H, "hg": None, "ag": None,
                 "PSH": 1.20, "PSD": 7.0, "PSA": 15.0}]

    async def odds_api(sports, what="odds", key=None):
        raise RuntimeError("no key: set ANALYTIX_ODDS_API_KEY (free at the-odds-api.com)")
    svc.fetch_history, svc.fetch_fixtures, svc.fetch_odds_api = history, fixtures, odds_api

    async def sports():
        raise RuntimeError("no key: set ANALYTIX_ODDS_API_KEY (free at the-odds-api.com)")
    svc.fetch_sports = sports

    async def go():
        for k in ("history", "fixtures", "sharp"):
            await svc.refresh(k)
    asyncio.run(go())
    assert svc.status["history"]["ok"] and svc.status["sharp"]["setup"] and len(svc.sharp()) == 1
    monkeypatch.setenv("ANALYTIX_BETS_TOKEN", "s3cret")
    with pytest.raises(PermissionError):
        asyncio.run(svc.push({"book": "bet9ja", "rows": []}, "wrong"))
    got = asyncio.run(svc.push({"book": "bet9ja", "rows": [{"home": "Big City", "away": "Rovers", "t_ms": t + 8 * H, "odds": {"home": 1.31}}]}, "s3cret"))
    assert got == {"book": "bet9ja", "games": 1, "matched": 1} and (tmp_path / "odds.json").exists()
    asyncio.run(svc.refresh("book"))
    pub = svc.public()
    assert asyncio.run(svc.snapshot())["value"] == pub["value"]

    async def stale():                                   # new odds: the page gets the last board at once, rebuilt behind it
        before = svc._board
        svc._changed()
        got = await svc.snapshot(public=True)
        assert svc._board is before and got["public"] is True and svc._rebuild_task is not None
        await svc._rebuild_task
        assert svc._board is not before and svc._fresh()
    asyncio.run(stale())
    assert pub["value"][0]["home"] == "Big City" and len(svc.book.flat) == 1
    assert pub["sources"]["books"]["bet9ja"]["games"] == 1 and pub["patterns"] and pub["matched"] == {"bet9ja": 1}
    assert BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json").books["bet9ja"]["rows"]   # survives a restart


def test_internationals():
    import importlib.util
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    a = {"R": "OK", "D": {"PAL": {"1": {"S_DESC": "Soccer", "SG": {"55": {"SG_DESC": "International", "G": {
            "170001": {"G_DESC": "UEFA Nations League"}, "170002": {"G_DESC": "Friendlies"},
            "170003": {"G_DESC": "Friendlies Women"}}}, "60": {"SG_DESC": "England", "G": {"170880": {"G_DESC": "Premier League"}}}}},
         "5": {"S_DESC": "Volleyball", "SG": {"77": {"SG_DESC": "International", "G": {"9001": {"G_DESC": "Nations League"}}}}}}}}
    got = N.intl_groups(a)
    assert 170001 in got and 170002 in got and 170003 not in got and 170880 not in got and 9001 not in got
    b = {"D": {"S": [{"ID": 1, "DS": "Soccer", "C": [{"ID": 55, "DS": "International Clubs", "G": [{"ID": 1, "DS": "Club Friendlies"}]},
                                                    {"ID": 56, "DS": "World", "G": [{"ID": 180001, "DS": "World Cup Qualification CAF"}]}]}]}}
    assert list(N.intl_groups(b)) == [180001]
    sports = [{"key": "soccer_uefa_nations_league", "group": "Soccer", "title": "UEFA Nations League", "active": True, "has_outrights": False},
              {"key": "soccer_epl", "group": "Soccer", "title": "EPL", "active": True, "has_outrights": False},
              {"key": "soccer_fifa_world_cup_winner", "group": "Soccer", "title": "FIFA World Cup Winner", "active": True, "has_outrights": True},
              {"key": "soccer_international_friendlies_women", "group": "Soccer", "title": "Friendlies (Women)", "active": True, "has_outrights": False},
              {"key": "basketball_fiba_world_cup_qualif", "group": "Basketball", "title": "FIBA qualifiers", "active": True, "has_outrights": False}]
    assert F.intl_sports(sports) == {"soccer_uefa_nations_league": ("UEFA Nations League", "soccer_uefa_nations_league")}
    for x, y in (("Czechia", "Czech Republic"), ("Türkiye", "Turkey"), ("Bosnia & Herzegovina", "Bosnia-Herzegovina"),
                 ("Rep. of Ireland", "Republic of Ireland"), ("Côte d'Ivoire", "Ivory Coast"), ("Korea Republic", "South Korea")):
        assert B.same_team(x, y) >= 0.92, (x, y)
    # an international is judged on the price alone, and can be the rollover pick when it's at or above fair
    now = NOW
    sharp = [{"id": "oa:n1", "league": "soccer_uefa_nations_league", "league_name": "UEFA Nations League", "home": "Spain",
              "away": "Georgia", "t_ms": now + 5 * H, "fair": B.devig({"1": 1.22, "X": 6.5, "2": 13}), "src": "Pinnacle (The Odds API)",
              "read_ms": now, "intl": True}]
    books = {"bet9ja": {"t_ms": now, "rows": [{"home": "Spain", "away": "Georgia", "t_ms": now + 5 * H, "odds": {"1": 1.30}}]}}
    board = B.build(books, sharp, _history(), {}, now)
    x = board[0]
    assert x["intl"] and x["pattern"] is None and x["p"] == x["fair_p"] and x["agree"] is None and x["fav_form"] is None
    assert B.rollover_pick(board, now) is x and x["edge"] >= 0
    books["bet9ja"]["rows"][0]["odds"]["1"] = 1.20                     # under fair: no pick
    assert B.rollover_pick(B.build(books, sharp, _history(), {}, now), now) is None
    raw = {"D": {"E": [{"DS": "Spain - Georgia", "GN": "UEFA Nations League", "STARTDATE": "2026-11-17 20:45:00", "ID": 9,
                        "O": {"S_1X2_1": "1.30", "S_1X2_X": "6.1", "S_1X2_2": "11", "S_DC_1X": "1.03", "S_DC_12": None}},
                       {"DS": "no odds - here", "STARTDATE": "2026-11-17 20:45:00", "ID": 10, "O": {}}]}}
    rows = N.bet9ja_rows(raw)
    assert rows == [{"match": "Spain - Georgia", "league": "UEFA Nations League", "time": 1794944700, "match_id": 9, "more": {},
                     "home": 1.3, "draw": 6.1, "away": 11.0, "home_or_draw": 1.03}]      # 20:45 in Lagos, names untouched


def test_margin_methods_and_the_study():
    from engine import betstudy as S
    o = {"1": 1.30, "X": 5.5, "2": 10.0}
    p = {m: B.devig(o, m) for m in B.METHODS}
    for m, f in p.items():
        assert abs(sum(f.values()) - 1) < 1e-9, m
    assert p["proportional"]["1"] < p["shin"]["1"] < p["power"]["1"]        # power and Shin take more off the long shot
    assert B.devig({"1": 1.9, "2": 1.9}, "shin") == {"1": 0.5, "2": 0.5}
    hist = _history(n=900)
    for g in hist:
        g.update(B365H=g["PSH"] * 0.98, B365A=g["PSA"] * 0.98, MaxH=g["PSH"] * 1.10, MaxA=g["PSA"] * 1.10, PSD=g["PSD"])
    sc = B.compare_methods(hist)
    assert all(sc[m]["n"] == 900 for m in B.METHODS) and B.best_method(sc, least=100) in B.METHODS
    assert B.best_method(sc) == "proportional"                               # too few games to choose: the default
    try:
        B.set_method("power")
        assert B.devig(o) == p["power"]
    finally:
        B.set_method("proportional")
    st = S.study(hist)
    best = st["books"]["best"]
    assert st["games"] > 0 and best["rules"][0]["rule"].startswith("The flat rule") and best["rules"][0]["n"] > 0
    lead = best["rules"][0]
    assert 0 <= lead["won"] <= lead["n"] and lead["se"] > 0 and best["rollover"]["days"] >= best["rollover"]["n"] - 0
    every = best["rules"][-1]
    assert every["n"] >= lead["n"]                                           # no edge needed: every price in the range
    assert all(c["season"] for c in S.candidates(hist))


def _row(**k):
    base = {"key": "k", "home": "A", "away": "B", "market": "1", "book": "bet9ja", "league": "E0", "league_name": "Premier League",
            "odds": 1.40, "edge": 0.025, "p": 0.73, "fair_p": 0.72, "odds_age_h": 0.2, "sharp_age_h": 3, "t_ms": NOW + 5 * H,
            "agree": True}
    return {**base, **k}


def test_bars_cap_and_lower_leagues():
    rows = [_row(key="top"), _row(key="low", league="E2"), _row(key="low_ok", league="SC0", edge=0.035),
            _row(key="intl", league="soccer_uefa_nations_league", intl=True), _row(key="stale", edge=0.14)]
    assert [x["key"] for x in B.value_bets(rows)] == ["low_ok", "top"]       # 3% in lower leagues; over 10% is not bet
    assert [x["key"] for x in B.value_bets(rows, edge=0.0)] == ["low_ok", "top", "low", "intl"]
    roll = [_row(key="low", league="E3", odds=1.25, edge=-0.01, p=0.9), _row(key="top", odds=1.25, edge=-0.01, p=0.8)]
    assert B.rollover_pick(roll, NOW)["key"] == "top"                          # a lower league needs at least fair
    roll[0]["edge"] = 0.0
    assert B.rollover_pick(roll, NOW)["key"] == "low"


def test_rollover_misses_say_why():
    board = [_row(key="a", odds=1.25, edge=-0.04), _row(key="b", odds=1.18, edge=0.01, agree=False),
             _row(key="c", odds=1.22, edge=-0.01, agree=None, intl=True), _row(key="d", odds=1.25, t_ms=NOW + 40 * H),
             _row(key="e", odds=1.90), _row(key="f", odds=1.25, edge=-0.005, league="E2")]
    m = {x["key"]: x["why"] for x in B.rollover_misses(board, NOW, n=6)}
    assert set(m) == {"a", "b", "c", "f"}                                     # too late and too long a price left out
    assert m["a"] == ["4.0% under fair, allowed 2%"]
    assert m["b"] == ["odds 1.18, needs 1.20–1.50"]                         # form isn't a test any more
    assert m["c"] == ["1.0% under fair; an international needs at least fair"]
    assert m["f"] == ["0.5% under fair; a lower league needs at least fair"]


def test_cups_from_the_list():
    sports = [{"key": "soccer_uefa_champs_league", "group": "Soccer", "active": True, "has_outrights": False},
              {"key": "soccer_uefa_europa_conference_league", "group": "Soccer", "active": True, "has_outrights": False},
              {"key": "soccer_uefa_champs_league_qualification", "group": "Soccer", "active": True, "has_outrights": False},
              {"key": "soccer_uefa_champs_league_winner", "group": "Soccer", "active": True, "has_outrights": True}]
    cups = F.club_cups(sports)
    assert cups["UCL"][1] == "soccer_uefa_champs_league" and cups["UECL"][1] == "soccer_uefa_europa_conference_league"
    assert cups["UEL"] == F.CUPS["UEL"]                                       # not listed: the built-in key


def test_fresh_read_before_kick_off(tmp_path, monkeypatch):
    rt = SimpleNamespace(mode="live")
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json")
    monkeypatch.setenv("ANALYTIX_ODDS_API_KEY", "k")
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    board = [_row(key="soon", league="N1", t_ms=now + 2 * H), _row(key="later", league="E0", t_ms=now + 9 * H),
             _row(key="long", league="SP1", t_ms=now + 2 * H, odds=2.5)]
    svc._board = (time.time(), svc._gen, board)
    svc.sharp_oa = [{"league": "N1", "id": "old"}, {"league": "E0", "id": "keep"}]
    calls = []

    async def odds_api(sports, what="odds", key=None):
        calls.append(sorted(sports))
        return {"N1": [{"id": "x", "home_team": "Ajax", "away_team": "PSV", "commence_time": "2026-11-21T15:00:00Z",
                        "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                            {"name": "Ajax", "price": 1.9}, {"name": "PSV", "price": 3.8}, {"name": "Draw", "price": 3.9}]}]}]}]}, 420, []
    svc.fetch_odds_api = odds_api
    asyncio.run(svc._pre_kickoff(now))
    assert calls == [["N1"]] and [r["id"] for r in svc.sharp_oa] == ["keep", "oa:x"]   # only the league about to play
    asyncio.run(svc._pre_kickoff(now + 600_000))
    assert calls == [["N1"]]                                                  # not again within 2 hours
    svc.credits, svc._fresh_at = 90, {}
    asyncio.run(svc._pre_kickoff(now))
    assert calls == [["N1"]]                                                  # under 100 credits left: saved for the day's read


def test_collector_finds_cups_and_reads_sportybet():
    import importlib.util
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    data = {"D": {"PAL": {"1": {"S_DESC": "Soccer", "SG": {
        "55": {"SG_DESC": "International Clubs", "G": {"1001": {"G_DESC": "UEFA Champions League"}, "1002": {"G_DESC": "UEFA Europa League"},
                                                       "1003": {"G_DESC": "UEFA Champions League Women"}}},
        "60": {"SG_DESC": "Netherlands", "G": {"1004": {"G_DESC": "Eredivisie"}}},
        "61": {"SG_DESC": "Scotland", "G": {"1005": {"G_DESC": "Premiership"}}},
        "62": {"SG_DESC": "China", "G": {"1006": {"G_DESC": "Super League"}}},
        "63": {"SG_DESC": "England", "G": {"170880": {"G_DESC": "Premier League"}}}}},
        "9": {"S_DESC": "Rugby Union", "SG": {"70": {"SG_DESC": "England", "G": {"9001": {"G_DESC": "Premiership"}}}}}}}}
    assert sorted(N.club_groups(data)) == [1001, 1002, 1004, 1005]
    assert 1001 not in N.intl_groups(data)                                    # a club cup isn't an international
    seen = {"D": {"PAL": {
        "1": {"S_DESC": "Antepost Soccer", "SG": {"5": {"SG_DESC": "International Clubs", "G": {
            "9781282": {"G_DESC": "UEFA Champions League2026/27 - To Reach the Final"}}},
            "6": {"SG_DESC": "Netherlands", "G": {"9767247": {"G_DESC": "Eredivisie 2026/27 - Top 3"}}}}},
        "2": {"S_DESC": "Soccer", "SG": {
            "7": {"SG_DESC": "Romania", "G": {"983830": {"G_DESC": "SuperLiga"}}},
            "8": {"SG_DESC": "Northern Ireland", "G": {"1078221": {"G_DESC": "Premiership"}}},
            "9": {"SG_DESC": "Denmark", "G": {"1": {"G_DESC": "Superligaen"}}},
            "10": {"SG_DESC": "Turkey", "G": {"2": {"G_DESC": "Super Lig"}}},
            "11": {"SG_DESC": "Greece", "G": {"3": {"G_DESC": "Super League 1"}, "4": {"G_DESC": "Super League 2"}}},
            "12": {"SG_DESC": "Portugal", "G": {"5": {"G_DESC": "Liga Portugal Betclic"}, "6": {"G_DESC": "Liga Portugal 2"}}},
            "13": {"SG_DESC": "Belgium", "G": {"7": {"G_DESC": "Pro League"}}},
            "14": {"SG_DESC": "International Clubs", "G": {"8": {"G_DESC": "UEFA Champions League"}, "9": {"G_DESC": "AFC Champions League Elite"}}}}},
        "3": {"S_DESC": "Ski Jumping", "SG": {"15": {"SG_DESC": "Individual LH", "G": {"10": {"G_DESC": "Lillehammer HS140 Qualification"}}}}},
        "4": {"S_DESC": "Squash", "SG": {"10991": {"SG_DESC": "International", "G": {"11": {"G_DESC": "China Open"}}}}}}}}
    # what the first live run found: futures (Antepost), namesakes and other sports, now all left out
    seen["D"]["PAL"]["5"] = {"S_DESC": "Players Soccer", "SG": {"3585": {"SG_DESC": "UEFA Champions League", "G": {
        "10535268": {"G_DESC": "PSG - Roma"}}}, "14899": {"SG_DESC": "Turkey Super Lig"}}}
    seen["D"]["PAL"]["2"]["SG"]["13"]["G"]["12"] = {"G_DESC": "Challenger Pro League"}
    assert sorted(N.club_groups(seen)) == [2, 3, 5, 7, 8]                    # nor player bets, nor Belgium's second tier
    assert N.intl_groups(seen) == {}
    reply = {"bizCode": 10000, "data": [{"id": "sr:tournament:7", "name": "UEFA Champions League", "events": [
        {"eventId": "sr:match:1", "homeTeamName": "Arsenal", "awayTeamName": "Benfica", "estimateStartTime": 1795636800000,
         "markets": [{"id": "1", "outcomes": [{"id": "1", "odds": "1.42"}, {"id": "2", "odds": "4.90"}, {"id": "3", "odds": "7.20"}]},
                     {"id": "10", "outcomes": [{"id": "9", "odds": "1.08", "desc": "Home or Draw"}, {"id": "11", "odds": "2.95", "isActive": 0}]},
                     {"id": "18", "outcomes": [{"id": "12", "odds": "1.80"}]}]},
        {"eventId": "sr:match:2", "homeTeamName": "No", "awayTeamName": "Odds", "estimateStartTime": 1795636800000, "markets": []}]}]}
    rows = N.sporty_rows(reply, "Champions League")
    assert [{k: v for k, v in r.items() if k not in ("sporty_ids", "more", "players")} for r in rows] == [
        {"match": "Arsenal - Benfica", "time": 1795636800.0, "league": "UEFA Champions League", "match_id": "sr:match:1",
         "home": 1.42, "draw": 4.9, "away": 7.2, "home_or_draw": 1.08}]
    assert rows[0]["sporty_ids"]["home_or_draw"] == {"event": "sr:match:1", "market": "10", "specifier": "", "outcome": "9"}
    by_desc = {"events": [{"homeTeamName": "A", "awayTeamName": "B", "estimateStartTime": 1, "markets": [
        {"id": "1", "outcomes": [{"desc": "Home", "odds": "1.5"}, {"desc": "Draw", "odds": "4"}, {"desc": "Away", "odds": "6"}]}]}]}
    assert N.sporty_rows(by_desc)[0]["away"] == 6.0


def test_a_restart_reuses_the_sharp_read(tmp_path, monkeypatch):
    rt = SimpleNamespace(mode="live")
    monkeypatch.setenv("ANALYTIX_ODDS_API_KEY", "k")
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json", tmp_path / "sharp.json")
    assert svc._first_wait("sharp") == 120                                    # nothing saved: read two minutes after a start

    async def sports():
        return [{"key": "soccer_uefa_europa_conference_league", "group": "Soccer", "active": True, "has_outrights": False}]

    async def odds_api(sports, what="odds", key=None):
        return {"E0": [{"id": "x", "home_team": "Arsenal", "away_team": "Fulham", "commence_time": "2026-11-21T15:00:00Z",
                        "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                            {"name": "Arsenal", "price": 1.33}, {"name": "Fulham", "price": 10.2}, {"name": "Draw", "price": 5.9}]}]}]}]}, 371, []
    svc.fetch_sports, svc.fetch_odds_api = sports, odds_api
    asyncio.run(svc.refresh("sharp"))
    assert svc.status["sharp"]["ok"] and (tmp_path / "sharp.json").exists()

    again = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json", tmp_path / "sharp.json")
    assert [r["id"] for r in again.sharp_oa] == ["oa:x"] and again.credits == 371 and again.status["sharp"]["ok"]
    assert again.cups["UECL"][1] == "soccer_uefa_europa_conference_league"
    assert again._first_wait("sharp") > 86000                                 # the next paid read waits for the day to pass
    assert again._first_wait("sharp", time.time() + 86400) == 120             # a day on, it reads again
    (tmp_path / "sharp.json").write_text("not json")
    assert BetsService(rt, None, None, tmp_path / "sharp.json").sharp_oa == []   # a broken file is ignored



def test_the_rule_is_pinnacle_alone():
    sharp = [{"id": "s1", "league": "E0", "league_name": "Premier League", "home": "Big City", "away": "Rovers", "t_ms": NOW + 5 * H,
              "fair": B.devig({"1": 1.40, "X": 4.6, "2": 8.0}), "src": "Pinnacle (The Odds API)", "read_ms": NOW}]
    hist = _history(home_bonus=0.15)                                          # history says such favourites win far more
    cal = B.calibration(hist)
    books = {"bet9ja": {"t_ms": NOW, "rows": [{"home": "Big City", "away": "Rovers", "t_ms": NOW + 5 * H, "odds": {"1": 1.44}}]}}
    x = B.build(books, sharp, hist, cal, NOW)[0]
    assert x["p"] == x["fair_p"] and abs(x["edge"] - (1.44 * x["fair_p"] - 1)) < 1e-9     # the rule's chance is Pinnacle's
    if x["pattern"]:
        assert x["p_hist"] > x["p"] and x["edge_hist"] > x["edge"]           # history is kept beside it, not used
    assert (x in B.value_bets([x], old=False)) == (B.FLAT_EDGE <= x["edge"] <= B.EDGE_MAX)
    roll = [{**x, "odds": 1.30, "edge": 1.30 * x["p"] - 1, "agree": False}]
    if roll[0]["edge"] >= B.ROLL_EDGE:
        assert B.rollover_pick(roll, NOW) is not None                        # form no longer blocks a rollover pick


def _leg(g, m, o, p, league="E0", book="bet9ja"):
    return {"match_id": g, "market": m, "odds": o, "p": p, "league": league, "t_ms": NOW + 5 * H, "book": book}


def test_the_combo_search_is_the_best_slip():
    import itertools
    import math
    from engine import combos as C
    rnd = random.Random(11)
    for _ in range(60):                                                       # against every slip there is
        bets = [_leg(g, m, round(rnd.uniform(1.1, 3.0), 2), 0) for g in range(rnd.randint(1, 6))
                for m in rnd.sample(["1", "2", "1X", "X2"], rnd.randint(1, 3))]
        for b in bets:
            b["p"] = min(0.97, 1 / b["odds"] * rnd.uniform(0.9, 1.06))
            b["league"] = rnd.choice(["E0", "E2"])
        target = round(rnd.uniform(1.1, 30), 2)
        games = {}
        for b in bets:
            games.setdefault(b["match_id"], []).append(b)
        fits = [s for c in itertools.product(*[[None, *v] for v in games.values()]) if (s := [x for x in c if x])
                and target <= math.prod(x["odds"] for x in s) <= target * (1 + C.TOL)]
        got = C.Solver(bets).find(target)
        assert (got is None) == (not fits)
        if fits:
            assert abs(C.value(got) - max(map(C.value, fits))) < 0.003
            assert target <= math.prod(x["odds"] for x in got) <= target * (1 + C.TOL)
            assert len({x["match_id"] for x in got}) == len(got)              # one bet a game
    # for the same total, fewer games and surer leagues win: each game counts a point (two lower down) against it
    two = [_leg("a", "1", 2.0, 0.5), _leg("b", "1", 2.0, 0.5)]
    four = [_leg(k, "1", 1.4142, 1 / 1.4142) for k in "cdef"]
    assert {x["match_id"] for x in C.Solver(two + four).find(3.99, tol=0.02)} == {"a", "b"}
    low = [_leg("a", "1", 2.0, 0.5, league="E2"), _leg("b", "1", 2.0, 0.5, league="E3"), _leg("c", "1", 2.0, 0.5), _leg("d", "1", 2.0, 0.5)]
    assert {x["match_id"] for x in C.Solver(low).find(3.99, tol=0.02)} == {"c", "d"}
    assert C.Solver(two).find(9) is None and C.Solver(two + four).find(3.99, tol=0.02, max_n=1) is None


def test_the_combo_takes_the_safest_games_first():
    from engine import combos as C
    safe = [_leg(f"s{k}", "1", 1.16, 0.86) for k in range(20)]                 # 86% games
    sure = [_leg(f"u{k}", "1X", 1.11, 0.905) for k in range(3)]                # 90%+, but only three of them
    flips = [_leg(f"f{k}", "2", 2.10, 0.49) for k in range(6)]                 # coin flips, better priced: never used
    mid = [_leg(f"m{k}", "1", 1.45, 0.70) for k in range(8)]                   # 70% games
    books = {"bet9ja": C.Builder(safe + sure + flips + mid)}
    two = C.best_of(books, 2.0)
    assert min(x["p"] for x in two) >= 0.85 and len(two) == 6               # 85%+ games, not one coin flip
    assert sum(x["p"] > 0.9 for x in two) == 3                               # the 90% games too: better priced
    assert all(x["p"] >= 0.90 for x in C.best_of(books, 1.3))                 # reachable with the 90% games alone
    fifty = C.best_of(books, 50)                                              # 15 games at 1.16 only reach 9.3
    assert fifty and len(fifty) <= C.MAX_GAMES and min(x["p"] for x in fifty) == 0.70
    assert all(x["p"] >= 0.6 for x in C.best_of(books, 150) or [])            # coin flips never, even to reach a total
    assert C.best_of({"bet9ja": C.Builder(safe)}, 50) is None and abs(C.Builder(safe).reach() - 1.16 ** 15) < 1e-9
    out = C.build(books, 2.0)
    assert out["slip"]["least"] == 0.86 and out["slip"]["n"] == 6 and out["settings"]["games"] == C.MAX_GAMES
    # a chosen minimum: the best return with every game at least that likely, so fewer, longer-priced games
    own = C.best_of(books, 2.0, 0.70)
    assert len(own) == 2 and min(x["p"] for x in own) >= 0.70                 # two 1.45s at 70%
    assert C.best_of(books, 50, 0.85) is None                                 # 85%+ games can't reach 50 in 15
    chosen = C.build(books, 2.0, least=0.72)
    assert chosen["least"] == 0.70 and chosen["slip"]["n"] == 2 and out["least"] is None


def test_combo_bets_follow_the_rules():
    from engine import combos as C
    row = _row(key="k", match_id="m1", odds=1.40, others=[{"book": "bet9ja", "odds": 1.40}, {"book": "betking", "odds": 1.36}])
    board = [row, {**row, "key": "k2", "market": "1X", "others": [{"book": "betking", "odds": 1.12}]},
             {**row, "key": "x", "market": "X", "others": [{"book": "betking", "odds": 2.9}]},              # no draws
             {**row, "key": "long", "match_id": "m2", "p": 0.45, "others": [{"book": "betking", "odds": 2.2}]},  # under 60%
             {**row, "key": "stale", "match_id": "m3", "others": [{"book": "betking", "odds": 2.2}]},        # 2.2 × 0.73: 61% over fair
             {**row, "key": "late", "match_id": "m4", "t_ms": NOW + 3 * D, "others": [{"book": "betking", "odds": 1.5}]},
             {**row, "key": "old", "match_id": "m5", "sharp_age_h": 30, "others": [{"book": "betking", "odds": 1.5}]},
             {**row, "key": "gone", "match_id": "m6", "t_ms": NOW + 60_000, "others": [{"book": "betking", "odds": 1.5}]}]
    got = C.legs(board, "betking", NOW - H, NOW, NOW + D)
    assert [(x["key"], x["odds"]) for x in got] == [("k", 1.36), ("k2", 1.12)] and got[0]["book"] == "betking"
    assert abs(got[0]["edge"] - (1.36 * 0.73 - 1)) < 1e-12
    assert C.legs(board, "betking", NOW - 3 * H, NOW, NOW + D) == []                  # the book's odds are too old
    assert [x["key"] for x in C.legs(board, "betking", NOW, NOW, NOW + 7 * D)] == ["k", "k2", "late"]
    sv = C.solvers(board, {"bet9ja": {"t_ms": NOW}, "betking": {"t_ms": NOW}}, NOW, 1)
    out = C.build(sv, 1.38)
    assert out["slip"]["book"] == "bet9ja" and out["slip"]["n"] == 1 and out["slip"]["odds"] == 1.40   # the better price
    assert out["bets"] == 3 and out["books"] == ["bet9ja", "betking"] and len(out["ladder"]) == len(C.LADDER)
    assert C.build(sv, 5000)["target"] == C.T_MAX == 1000 and C.build(sv, 0)["target"] == C.T_MIN
    assert C.build(sv, 500)["settings"]["games"] == 20 and C.build(sv, 50)["settings"]["games"] == 15


def test_the_combo_service_and_its_past(tmp_path):
    from engine import combos as C
    rt = SimpleNamespace(mode="live")
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json")
    t = int(time.time() * 1000)
    sharp = [_sharp(h, a, t + (5 + i) * H, oh, 7.0, 12.0, read=t, sid=f"s{i}") for i, (h, a, oh) in
             enumerate([("Big City", "Rovers", 1.20), ("Town", "Albion", 1.25), ("United", "Villa", 1.30), ("Athletic", "Wanderers", 1.40)])]
    svc.books = {"bet9ja": {"t_ms": t, "rows": [{"home": s["home"], "away": s["away"], "t_ms": s["t_ms"],
                                                 "odds": {"1": round(1 / s["fair"]["1"], 2)}} for s in sharp]}}
    svc._board = (time.time(), svc._gen, B.build(svc.books, sharp, [], {}, t))
    got = asyncio.run(svc.combo(2.0, 1))
    assert got["days"] == 1 and got["slip"] and 2.0 <= got["slip"]["odds"] <= 2.2 and got["slip"]["book"] == "bet9ja"
    assert asyncio.run(svc.combo(2.0, 5))["days"] == 1                                 # only 1, 3 or 7 days
    hist = _history(n=600)
    for g in hist:
        g.update(PSH=1.30, PSD=5.0, PSA=9.0, B365H=1.27, B365A=8.5)            # a home favourite every game, three a day
    past = C.past(hist)
    assert set(past) == {"safest", "0.60", "0.65", "0.70", "0.75", "0.80", "0.85", "0.90"}
    assert past["0.90"]["1.5"]["n"] == 0                                      # 1.27s are about 75% likely
    past = past["safest"]
    two = past["1.5"]
    assert two["n"] > 50 and 0 <= two["won"] <= two["n"] and two["se"] > 0 and 1.5 <= two["odds"] <= 1.65
    assert past["200"]["n"] == 0                                              # three games a day can't reach it
    assert set(past) == {str(x) for x in C.LADDER}


FD_GOALS = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,PSH,PSD,PSA,P>2.5,P<2.5,PC>2.5,PC<2.5,Avg>2.5,Avg<2.5,B365>2.5,B365<2.5
E0,21/11/2026,15:00,Arsenal,Fulham,,,1.40,4.8,8.0,1.62,2.38,,,1.60,2.30,1.57,2.35
E0,21/11/2026,17:30,Burnley,Everton,,,2.90,3.2,2.60,,,,,2.10,1.72,2.05,1.75
"""


def test_over_under_goals():
    from engine import betstudy as S
    from engine import combos as C
    assert B.wins("O25", 2, 1) and not B.wins("O25", 1, 1) and B.wins("U25", 0, 2) and not B.wins("U25", 3, 0)
    assert not B.wins("2", 1, 1) and B.wins("2", 0, 1)                                  # the win markets unchanged
    g = F.parse_fd(FD_GOALS, "E0")
    assert g[0]["P>2.5"] == 1.62 and g[1]["Avg<2.5"] == 1.72
    s = F.sharp_from_fd(g, NOW)
    assert s[0]["ou_odds"] == {"O25": 1.62, "U25": 2.38} and s[0]["ou_src"].startswith("Pinnacle")
    assert s[1]["ou_src"].startswith("market average") and "ou_odds" in s[1]                 # no Pinnacle: the average
    # the board: over/under priced from the sharp game's own over/under, judged on the price alone, 3% bar
    sharp = [{**s[0], "t_ms": NOW + 6 * H, "read_ms": NOW - H}]
    books = {"bet9ja": {"t_ms": NOW, "rows": [{"home": "Arsenal", "away": "Fulham", "t_ms": NOW + 6 * H,
                                              "odds": {"1": 1.36, "O25": 1.70, "U25": 2.20}}]}}
    board = {x["market"]: x for x in B.build(books, sharp, [], {}, NOW)}
    over = board["O25"]
    assert abs(over["p"] - B.devig({"O25": 1.62, "U25": 2.38})["O25"]) < 1e-12 and over["pattern"] is None
    assert over["sharp_src"].startswith("Pinnacle") and "agree" not in over and B.lower(over) and not B.lower(board["1"])
    assert B.edge_bar(over) == B.FLAT_EDGE_LOW
    # The Odds API's win prices with football-data's over/under for the same game
    rt = SimpleNamespace(mode="live")
    svc = BetsService(rt)
    svc.sharp_oa = [{"id": "oa:1", "league": "E0", "home": "Arsenal", "away": "Fulham", "t_ms": s[0]["t_ms"] + 600_000,
                     "fair": B.devig({"1": 1.42, "X": 4.7, "2": 7.8}), "src": "Pinnacle (The Odds API)", "read_ms": NOW}]
    svc.sharp_fd = s
    got = svc.sharp()
    assert len(got) == 2 and got[0]["id"] == "oa:1" and got[0]["ou_odds"] == {"O25": 1.62, "U25": 2.38}
    assert "ou_odds" not in svc.sharp_oa[0]                                             # the saved read is untouched
    # the push, settling and the close
    _, rows = F.parse_push({"book": "bet9ja", "rows": [{"home": "A", "away": "B", "t_ms": NOW, "odds": {"over_2_5": "1.70", "under_2_5": 2.2}}]})
    assert rows[0]["odds"] == {"O25": 1.70, "U25": 2.2}
    bk = B.Book()
    bk.add_flat({**over, "key": "g1"}, NOW)
    bk.settle([{"home": "Arsenal", "away": "Fulham", "t_ms": over["t_ms"], "hg": 2, "ag": 1}],
              [{"home": "Arsenal", "away": "Fulham", "t_ms": over["t_ms"], "fair": None, "ou": B.devig({"O25": 1.55, "U25": 2.50})}])
    assert bk.flat[0]["status"] == "won" and bk.flat[0]["clv"] > 0                    # 1.70 beat a 1.55 close
    # three seasons: over/under rows in the replay, and over/under legs on past slips
    hist = _history(n=600)
    for k, h in enumerate(hist):
        h.update({"P>2.5": 1.80, "P<2.5": 2.05, "B365>2.5": 1.85, "B365<2.5": 1.95, "Max>2.5": 1.95, "Max<2.5": 2.10,
                  "B365H": h["PSH"] * 0.97, "B365A": h["PSA"] * 0.97, "MaxH": h["PSH"], "MaxA": h["PSA"]})
    rules = {r["rule"]: r for r in S.study(hist)["books"]["best"]["rules"]}
    ou = rules["…any odds, edge 3%+"]
    assert rules["Over/under 2.5 goals: odds 1.20–1.50, edge 3%+"]["n"] == 0 and ou["n"] > 0
    assert rules["Every over/under 2.5 price, no edge needed"]["n"] == 1200
    assert C.past(hist)["safest"]["1.5"]["n"] >= 0


def test_the_collector_reads_over_under():
    import importlib.util
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    raw = {"D": {"E": [{"DS": "Arsenal - Fulham", "GN": "Premier League", "STARTDATE": "2026-11-21 16:00:00", "ID": 7,
                        "O": {"S_1X2_1": "1.36", "S_OU@2.5_O": "1.70", "S_OU@2.5_U": "2.10", "S_OU@3.5_O": "2.9"}}]}}
    r = N.bet9ja_rows(raw)[0]
    assert r["over_2_5"] == 1.70 and r["under_2_5"] == 2.10 and r["home"] == 1.36
    goals = {}

    class Lib:                                                                # NaijaBet_Api's reader, by its shape
        def normalizer(self, data):
            return [{"match": "Arsenal - Fulham", "match_id": 7, "home": 1.36}]

        def get_all(self):
            return self.normalizer(raw)
    lib = Lib()
    N.keep_goals(lib, goals)
    assert lib.get_all()[0]["home"] == 1.36 and goals == {7: {"over_2_5": 1.70, "under_2_5": 2.10, "more": {"O35": 2.9}}}
    sporty = {"data": [{"name": "Premier League", "events": [{"homeTeamName": "Arsenal", "awayTeamName": "Fulham",
               "estimateStartTime": 1795000000000, "eventId": "sr:match:1", "markets": [
                   {"id": "1", "outcomes": [{"id": "1", "odds": "1.37"}, {"id": "2", "odds": "5.0"}, {"id": "3", "odds": "8.1"}]},
                   {"id": "18", "specifier": "total=3.5", "outcomes": [{"id": "12", "odds": "2.95"}, {"id": "13", "odds": "1.38"}]},
                   {"id": "18", "specifier": "total=2.5", "outcomes": [{"id": "12", "odds": "1.72"}, {"id": "13", "odds": "2.08"}]}]}]}]}
    row = N.sporty_rows(sporty)[0]
    assert row["over_2_5"] == 1.72 and row["under_2_5"] == 2.08 and row["home"] == 1.37
    assert N.goals_line("x", [{"odds": {"over_2_5": 1.7, "under_2_5": 2.1, "O15": 1.2, "CO95": 1.8}}, {"odds": {"home": 1.2}}]) == \
        "  x: over/under 2.5 on 1 of 2 games; other markets on 1, corners on 1, handicaps on 0, shots on 0, keeper saves on 0, win or over on 0, players to score on 0, substitute scores on 0, 1Up/2Up on 0"


def test_value_bets_by_either_rule():
    pat = {"n": 400, "won": 330, "rate": 0.825, "implied": 0.80, "scope": "E0", "kind": "sharp", "range": [1.16, 1.25], "shift": 0.02}
    rows = [_row(key="new", edge=0.022, edge_hist=0.03, pattern=pat),
            _row(key="old", edge=0.004, edge_hist=0.026, pattern=pat),                  # only history lifts it over 2%
            _row(key="old_low", league="E2", edge=0.004, edge_hist=0.026, pattern=pat),  # a lower league needs 3%
            _row(key="no_pat", edge=0.004, edge_hist=0.026),                              # no history pattern: no old rule
            _row(key="old_cap", edge=0.05, edge_hist=0.14, pattern=pat),                  # the new rule's
            _row(key="old_big", edge=0.01, edge_hist=0.12, pattern=pat)]                  # over 10% either way: not bet
    assert {x["key"]: B.rule_of(x) for x in rows} == {"new": "new", "old": "old", "old_low": None, "no_pat": None,
                                                      "old_cap": "new", "old_big": None}
    assert [x["key"] for x in B.value_bets(rows)] == ["old_cap", "new", "old"]          # the new rule's first
    assert [x["key"] for x in B.value_bets(rows, old=False)] == ["old_cap", "new"]
    bk = B.Book()
    assert bk.add_flat(rows[1], NOW)["rule"] == "old" and bk.flat[0]["edge_hist"] == 0.026


def test_sportybet_booking_codes(tmp_path, monkeypatch):
    import importlib.util
    from engine import combos as C
    # the push keeps SportyBet's ids, the board carries them per book, the combo builder's legs keep them
    _, rows = F.parse_push({"book": "sportybet", "rows": [{"home": "Big City", "away": "Rovers", "t_ms": NOW, "odds": {"home": 1.31, "draw": 5},
                            "ids": {"home": {"event": "sr:match:9", "market": "1", "specifier": "", "outcome": "1"},
                                    "away": {"event": "sr:match:9", "market": "1", "outcome": "3"}}}]})   # no away price: dropped
    assert rows[0]["ids"] == {"1": {"event": "sr:match:9", "market": "1", "specifier": "", "outcome": "1"}}
    t = int(time.time() * 1000)
    sharp = [_sharp(h, a, t + (5 + i) * H, oh, 7.0, 12.0, read=t, sid=f"s{i}") for i, (h, a, oh) in
             enumerate([("Big City", "Rovers", 1.20), ("Town", "Albion", 1.25), ("United", "Villa", 1.30)])]
    sel = lambda i: {"event": f"sr:match:{i}", "market": "1", "specifier": "", "outcome": "1"}
    books = {"sportybet": {"t_ms": t, "rows": [{"home": s["home"], "away": s["away"], "t_ms": s["t_ms"], "odds": {"1": round(1 / s["fair"]["1"], 2)},
                                                "ids": {"1": sel(i)}} for i, s in enumerate(sharp)]},
             "bet9ja": {"t_ms": t, "rows": [{"home": s["home"], "away": s["away"], "t_ms": s["t_ms"], "odds": {"1": round(1 / s["fair"]["1"], 2) + 0.02}}
                                            for s in sharp]}}
    board = B.build(books, sharp, [], {}, t)
    x = next(r for r in board if r["home"] == "Big City")
    assert x["book"] == "bet9ja" and {o["book"]: o.get("sel") for o in x["others"]} == {"bet9ja": None, "sportybet": sel(0)}
    sv = C.solvers(board, books, t, 1)
    assert C.build(sv, 1.55)["slip"]["book"] == "bet9ja" and not C.build(sv, 1.55)["slip"]["coded"]
    only = C.build(sv, 1.55, book="sportybet")
    assert only["slip"]["book"] == "sportybet" and only["slip"]["coded"] and only["books"] == ["bet9ja", "sportybet"]
    # the page asks; the laptop collects the job, books it on SportyBet and sends the code back
    monkeypatch.setenv("ANALYTIX_BETS_TOKEN", "s3cret")
    svc = BetsService(SimpleNamespace(mode="live"))
    svc.books, svc._board = books, (time.time(), svc._gen, board)
    first = asyncio.run(svc.combo_code(1.55))
    assert first["status"] == "waiting" and first["games"] >= 1
    with pytest.raises(PermissionError):
        svc.code_jobs("wrong")
    jobs = svc.code_jobs("s3cret")
    assert len(jobs) == 1 and jobs[0]["selections"][0]["eventId"].startswith("sr:match:") and jobs[0]["selections"][0]["specifier"] is None
    assert asyncio.run(svc.combo_code(1.55))["status"] == "waiting"                     # asked again: the same job
    svc.code_done({"id": jobs[0]["id"], "code": "N3N8VT", "url": "https://www.sportybet.com/ng/?shareCode=N3N8VT",
                   "deadline_ms": str(t + 5 * H), "missing": []}, "s3cret")
    got = asyncio.run(svc.combo_code(1.55))
    assert got["status"] == "ready" and got["code"] == "N3N8VT" and got["deadline_ms"] == t + 5 * H and svc.code_jobs("s3cret") == []
    svc.codes[jobs[0]["id"]]["deadline_ms"] = t - 1                                     # past SportyBet's deadline: asked anew
    assert asyncio.run(svc.combo_code(1.55))["status"] == "waiting"
    svc.codes[jobs[0]["id"]]["asked"] -= 200                                            # the laptop never answered
    assert "collector" in asyncio.run(svc.combo_code(1.55))["why"]
    svc.code_done({"id": "nope", "code": "X"}, "s3cret")                                # an unknown job is ignored
    # the collector's side: SportyBet's answer read, and an error passed back
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)

    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body, self.content = status, body, b"x"

        def json(self):
            return self._body

    class Session:
        def __init__(self, reply):
            self.reply, self.sent = reply, None

        def post(self, url, json=None, timeout=None):
            self.sent = (url, json)
            return self.reply
    ok = Session(Reply(200, {"bizCode": 10000, "data": {"shareCode": "AB12CD", "shareURL": "https://x", "deadline": 1795000000000,
                                                         "unavailableOutcomes": [{"homeTeamName": "Town", "awayTeamName": "Albion"}]}}))
    assert N.sporty_code(ok, [{"eventId": "sr:match:1"}]) == {"code": "AB12CD", "url": "https://x", "deadline_ms": 1795000000000,
                                                            "missing": ["Town Albion"]}
    assert ok.sent == (N.SPORTY_SHARE, {"selections": [{"eventId": "sr:match:1"}]})
    assert N.sporty_code(Session(Reply(403, {"message": "blocked"})), []) == {"error": "HTTP 403: blocked"}
    calls = []
    monkeypatch.setattr(N, "server_call", lambda server, token, path, body=None: calls.append((path, body)) or
                        ([{"id": "j1", "selections": [{"eventId": "sr:match:1"}]}] if body is None else {"ok": True}))
    N.serve_codes(SimpleNamespace(server="http://localhost:8001", token="t"), ok)
    assert calls[1] == ("/api/bets/code-jobs", {"id": "j1", "code": "AB12CD", "url": "https://x", "deadline_ms": 1795000000000,
                                                "missing": ["Town Albion"]})


def test_the_collector_says_why_it_cant_look_for_codes(monkeypatch, capsys):
    import importlib.util
    import urllib.error
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)

    def refused(*a, **k):
        raise urllib.error.HTTPError("http://localhost:8001/api/bets/code-jobs", 404, "Not Found", {}, None)
    monkeypatch.setattr(N, "server_call", refused)
    args = SimpleNamespace(server="http://localhost:8001", token="")
    N.serve_codes(args, None)
    N.serve_codes(args, None)                                                 # the same trouble again: said once
    out = capsys.readouterr().out
    assert out.count("couldn't look for SportyBet code requests: HTTP 404") == 1 and "older version" in out


def test_the_goals_and_corners_models():
    from engine import corners as K
    from engine import goals as G
    m = G.grid(1.7, 1.0)
    p1, px, p2, po = (float(m[G.MASKS[k]].sum()) for k in ("1", "X", "2", "O25"))
    lh, la = G.fit(p1, px, p2, po)
    assert abs(lh - 1.7) < 0.01 and abs(la - 1.0) < 0.01
    mk = G.markets(G.grid(lh, la))
    assert abs(mk["O15"] + mk["U15"] - 1) < 1e-9 and mk["O05"] > mk["O15"] > mk["O25"] > mk["O35"]
    assert mk["HO05"] > mk["AO05"] and mk["1|O15"] >= max(mk["O15"], p1 - 1e-6) and abs(mk["DNB1"][0] - p1) < 1e-6 and abs(mk["DNB1"][1] - px) < 1e-6
    for code, score, won in [("O15", (1, 1), True), ("U15", (1, 0), True), ("HO15", (2, 0), True), ("AO05", (2, 0), False),
                             ("GG", (1, 1), True), ("NG", (2, 0), True), ("1|O25", (1, 0), True), ("1|O25", (0, 3), True),
                             ("2|O15", (1, 0), False), ("DNB1", (1, 1), None), ("DNB2", (0, 2), True)]:
        assert B.wins(code, *score) == won, code
    assert [B.kind(k) for k in ("1", "DNB2", "O35", "HO15", "GG", "1|O15", "CO95", "ZZ")] == \
        ["win", "dnb", "goals", "team", "btts", "mix", "corners", None]
    assert [B.kind(k) for k in ("1X", "12", "X2")] == ["dc", "dc", "dc"]                  # double chance has its own button
    rnd = random.Random(4)
    hist = []
    for k in range(900):
        h, a = rnd.sample(["Manchester City", "Burnley", "Arsenal", "Fulham", "Everton", "Wolves"], 2)
        hist.append({"league": "E0", "home": h, "away": a, "t_ms": NOW - (900 - k) * H, "hc": 9 if h == "Manchester City" else 4,
                     "ac": 2 + rnd.randint(0, 3)})
    model, spreads = K.build(hist)
    assert model.find("Man City") == "Manchester City" and model.find("Real Madrid") is None
    mh, ma = model.expect("Manchester City", "Burnley", "E0")
    assert mh > ma and abs(sum(K.total(mh, ma, 8.0)) - 1) < 1e-9 and K.wins("CO95", 6, 4) and not K.wins("CU95", 6, 4)


def test_the_combo_builder_mixes_markets():
    from engine import combos as C
    from engine import corners as K
    t = NOW
    sharp = [_sharp("Manchester City", "Burnley", t + 5 * H, 1.30, 5.8, 10.0, read=t, sid="s1"),
             _sharp("Arsenal", "Fulham", t + 6 * H, 1.55, 4.2, 6.0, read=t, sid="s2")]
    hist = [{"league": "E0", "home": h, "away": a, "t_ms": t - k * H, "hc": 7, "ac": 4}
            for k, (h, a) in enumerate([("Manchester City", "Burnley"), ("Arsenal", "Fulham"), ("Burnley", "Arsenal"), ("Fulham", "Manchester City")] * 6)]
    corners = K.build(hist)
    odds = {"1": 1.28, "DNB1": 1.10, "O15": 1.22, "HO05": 1.08, "GG": 2.10, "1|O15": 1.10, "CO85": 1.60}
    books = {"sportybet": {"t_ms": t, "rows": [{"home": s["home"], "away": s["away"], "t_ms": s["t_ms"], "odds": dict(odds)} for s in sharp]}}
    board = B.build(books, sharp, [], {}, t, corners=corners)
    by = {(x["home"], x["market"]): x for x in board}
    dnb = by[("Manchester City", "DNB1")]
    assert dnb["p_void"] > 0 and abs(dnb["edge"] - (1.10 * dnb["p"] + dnb["p_void"] - 1)) < 1e-12 and dnb["model"] is None
    assert by[("Manchester City", "O15")]["model"] == "goals" and by[("Manchester City", "CO85")]["model"] == "corners"
    assert {x["market"] for x in B.value_bets(board, edge=-1)} <= set(B.CORE)              # the new markets: combos only
    all_kinds = C.solvers(board, books, t, 1)
    goals_only = C.solvers(board, books, t, 1, ("goals",))
    assert {x["market"] for x in goals_only["sportybet"].bets} == {"O15"}
    mixed = C.best_of(all_kinds, 1.15)
    assert mixed and all(C.chance(x) >= 0.6 for x in mixed) and len({x["match_id"] for x in mixed}) == len(mixed)
    s = C.slip([x for x in all_kinds["sportybet"].bets if x["market"] == "DNB1"])
    assert abs(s["p"] - math.prod(x["p"] + x["p_void"] for x in all_kinds["sportybet"].bets if x["market"] == "DNB1")) < 1e-12
    assert C.build(all_kinds, 1.15, {"safest": {"1.5": {"n": 9}}}, kinds=("win", "goals", "corners"))["ladder"][0]["past"] is None
    _, rows = F.parse_push({"book": "sportybet", "rows": [{"home": "A", "away": "B", "t_ms": t, "odds": {"O15": 1.2, "CO95": 1.8, "nonsense": 2}}]})
    assert rows[0]["odds"] == {"O15": 1.2, "CO95": 1.8}


def test_the_collector_reads_the_combo_markets():
    import importlib.util
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    o = lambda i, d, v: {"id": i, "desc": d, "odds": v}
    reply = {"data": [{"name": "Premier League", "events": [{"homeTeamName": "Man City", "awayTeamName": "Fulham",
             "estimateStartTime": 1795264200000, "eventId": "sr:match:72221406", "markets": [
                 {"id": "1", "outcomes": [o("1", "Home", "1.30"), o("2", "Draw", "6.0"), o("3", "Away", "9.5")]},
                 {"id": "11", "outcomes": [o("4", "Home", "1.08"), o("5", "Away", "7.5")]},
                 {"id": "18", "specifier": "total=1.5", "outcomes": [o("12", "Over 1.5", "1.15"), o("13", "Under 1.5", "5.2")]},
                 {"id": "18", "specifier": "total=2", "outcomes": [o("12", "Over 2", "1.30"), o("13", "Under 2", "3.4")]},
                 {"id": "18", "specifier": "total=2.5", "outcomes": [o("12", "Over 2.5", "1.50"), o("13", "Under 2.5", "2.55")]},
                 {"id": "19", "specifier": "total=0.5", "outcomes": [o("12", "Over 0.5", "1.06"), o("13", "Under 0.5", "8.0")]},
                 {"id": "20", "specifier": "total=1.5", "outcomes": [o("12", "Over 1.5", "3.9"), o("13", "Under 1.5", "1.22")]},
                 {"id": "29", "outcomes": [o("74", "Yes", "1.95"), o("76", "No", "1.80")]},
                 {"id": "166", "specifier": "total=9.5", "outcomes": [o("12", "Over 9.5", "1.70"), o("13", "Under 9.5", "2.05")]},
                 {"id": "45", "outcomes": [o("x", "0:0", "15")]}]}]}]}
    row = N.sporty_rows(reply)[0]
    assert row["home"] == 1.30 and row["over_2_5"] == 1.50 and "U15" not in row
    assert row["more"] == {"DNB1": 1.08, "DNB2": 7.5, "O15": 1.15, "U15": 5.2, "HO05": 1.06, "HU05": 8.0, "AO15": 3.9,
                           "AU15": 1.22, "GG": 1.95, "NG": 1.80, "CO95": 1.70, "CU95": 2.05}       # the 2.0 line skipped
    assert row["sporty_ids"]["CO95"] == {"event": "sr:match:72221406", "market": "166", "specifier": "total=9.5", "outcome": "12"}
    b9 = N.b9_more({"S_DNB_1": "1.09", "S_OU@1.5_O": "1.14", "S_OU@2.5_O": "1.5", "S_HAOU@0.5_OH": "1.05",
                    "S_HAOU@1.5_UA": "1.25", "S_GGNG_Y": "1.9", "S_CHANCEMIXOU@1.5_1orOv": "1.04",
                    "S_CHANCEMIXOU@3.5_1orOv": "1.2", "S_CHANCEMIXGGOU@2.5_2orOv": "1.4", "S_OUCORNERS@9.5_U": "2.0",
                    "S_OUCORNERS@20.5_U": "1.01", "S_1X2_1": "1.3"})
    assert b9 == {"DNB1": 1.09, "O15": 1.14, "HO05": 1.05, "AU15": 1.25, "GG": 1.9, "1|O15": 1.04, "2|O25": 1.4, "CU95": 2.0}
    assert all(B.kind(k) for k in [*b9, *row["more"]])                       # the server knows every code it's sent


def test_a_thousand_odds_slip():
    from engine import combos as C
    bets = [_leg(f"a{k}", "1", 1.50, 0.67) for k in range(25)] + [_leg(f"b{k}", "1", 1.41, 0.72) for k in range(25)]
    books = {"sportybet": C.Builder(bets)}
    big = C.best_of(books, 1000)
    total = math.prod(x["odds"] for x in big)
    assert 1000 <= total <= 1100 and 15 < len(big) <= 20                       # over 200: up to 20 games
    assert books["sportybet"].reach(15) < 1000 < books["sportybet"].reach(20)
    out = C.build(books, 1000)
    assert out["slip"]["n"] == len(big) and [r["target"] for r in out["ladder"]][-2:] == [500, 1000]


def test_the_combo_builder_says_which_books_have_the_markets():
    from engine import combos as C
    books = {"bet9ja": C.Builder([{**_leg("a", "1|O25", 1.30, 0.75), "book": "bet9ja"}]), "sportybet": C.Builder([])}
    out = C.build(books, 1.2, book="sportybet", kinds=("mix",))
    assert out["bets"] == 0 and out["book"] == "sportybet" and out["books"] == ["bet9ja"] and out["slip"] is None


def test_handicaps_shots_and_saves():
    """Handicaps from the goals model's score grid; shots, shots on target and saves from the count models."""
    from engine import corners as K
    from engine import combos as C
    from engine import goals as G
    m = G.grid(1.7, 0.9)
    mk = G.markets(m)
    p1, px, p2 = (float(m[G.MASKS[k]].sum()) for k in ("1", "X", "2"))
    assert abs(mk["AH1M05"] - p1) < 1e-9 and abs(mk["AH2P05"] - (px + p2)) < 1e-9      # -0.5 is a win; +0.5 a win or draw
    win, void = mk["AH1M10"]                                                             # -1: a one-goal win is refunded
    assert abs(win - mk["AH1M15"]) < 1e-9 and abs(win + void + mk["AH2P05"] - 1) < 1e-9
    assert abs(mk["EH011"] + mk["EH01X"] + mk["EH012"] - 1) < 1e-9 and abs(mk["EH011"] - mk["AH1M15"]) < 1e-9
    for code, score, won in [("AH1M15", (2, 0), True), ("AH1M15", (1, 0), False), ("AH1M10", (1, 0), None), ("AH2P15", (2, 1), True),
                             ("AH2P15", (3, 1), False), ("AH2P10", (1, 0), None), ("EH011", (2, 0), True), ("EH01X", (1, 0), True),
                             ("EH012", (1, 1), True), ("EH101", (0, 0), True)]:
        assert G.wins(code, *score) == won and B.wins(code, *score) == won, code
    assert [B.kind(k) for k in ("AH1M15", "EH01X", "CO95", "CHO45", "SHO245", "STAU35", "SVHO25")] == \
        ["handicap", "handicap", "corners", "corners", "shots", "shots", "saves"]
    g = {"hs": 15, "as": 8, "hst": 6, "ast": 3, "hg": 2, "ag": 1, "hc": 7, "ac": 2}
    assert K.pair("shots", g) == (15, 8) and K.pair("sot", g) == (6, 3) and K.pair("saves", g) == (2, 4)   # home keeper saved 3-1
    assert K.wins("SVAO35", *K.pair("saves", g)) and not K.wins("SHU225", *K.pair("shots", g)) and K.wins("CHO55", 7, 2)
    rnd = random.Random(9)
    hist = []
    for k in range(1200):
        h, a = rnd.sample(["Manchester City", "Burnley", "Arsenal", "Fulham", "Everton", "Wolves"], 2)
        big = h == "Manchester City"
        hist.append({"league": "E0", "home": h, "away": a, "t_ms": NOW - (1200 - k) * H, "hg": rnd.randint(0, 3), "ag": rnd.randint(0, 2),
                     "hs": rnd.randint(18, 26) if big else rnd.randint(8, 14), "as": rnd.randint(5, 12),
                     "hst": rnd.randint(7, 10) if big else rnd.randint(3, 5), "ast": rnd.randint(4, 6)})
    models = {stat: K.build(hist, stat) for stat in ("shots", "sot", "saves")}
    shots = K.priced("shots", *models["shots"][0].expect("Manchester City", "Burnley", "E0"), 20.0)
    assert shots["SHHO125"] > 0.8 and abs(shots["SHO245"] + shots["SHU245"] - 1) < 1e-9
    chk = K.check(hist, "sot")
    assert chk["games"] == 1200 and all(v["n"] > 0 for v in chk["markets"].values())
    sharp = [_sharp("Manchester City", "Burnley", NOW + 5 * H, 1.30, 5.8, 10.0, read=NOW, sid="s1")]
    odds = {"1": 1.28, "AH1M10": 1.40, "AH1M15": 1.85, "SHHO125": 1.30, "SVAO15": 1.50}
    books = {"sportybet": {"t_ms": NOW, "rows": [{"home": "Manchester City", "away": "Burnley", "t_ms": sharp[0]["t_ms"], "odds": odds}]}}
    board = {x["market"]: x for x in B.build(books, sharp, [], {}, NOW, corners=models)}
    assert board["AH1M10"]["p_void"] > 0 and board["AH1M10"]["model"] == "goals" and board["AH1M15"]["p_void"] == 0
    assert board["SHHO125"]["model"] == "shots" and board["SVAO15"]["model"] == "saves" and "shots model" in board["SHHO125"]["sharp_src"]
    assert C.haircut(board["SHHO125"]) == C.HAIRCUT_CORNERS and C.haircut(board["AH1M15"]) == C.HAIRCUT_LOW
    row = books["sportybet"]["rows"][0]
    row["odds"] = {"1": 1.28, **{f"SHHO{k}5": 1.5 for k in range(12, 21)}}
    ps = {x["market"]: x["p"] for x in B.build(books, sharp, [], {}, NOW, corners=models) if x["market"] != "1"}
    line = next(int(m[4:-1]) for m, p in sorted(ps.items()) if 0.65 < p < 0.88)          # a line the builder can use
    row["odds"] = {"1": 1.28, f"SHHO{line}5": round(1.03 / ps[f"SHHO{line}5"], 2)}         # inside the 10% edge cap
    board = B.build(books, sharp, [], {}, NOW, corners=models)
    assert {x["market"] for x in C.solvers(board, books, NOW, 1, ("shots",))["sportybet"].bets} == {f"SHHO{line}5"}


def test_the_collector_reads_handicaps_shots_and_saves():
    import importlib.util
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    o = lambda i, d, v: {"id": i, "desc": d, "odds": v}
    pp = "pre:playerprops:72221406"
    reply = {"data": [{"name": "Premier League", "events": [{"homeTeamName": "Man City", "awayTeamName": "Fulham",
             "estimateStartTime": 1795264200000, "eventId": "sr:match:72221406", "markets": [
                 {"id": "1", "outcomes": [o("1", "Home", "1.40"), o("2", "Draw", "5.79"), o("3", "Away", "7.42")]},
                 {"id": "16", "specifier": "hcp=-1.5", "outcomes": [o("1714", "Home (-1.5)", "1.96"), o("1715", "Away (+1.5)", "1.85")]},
                 {"id": "16", "specifier": "hcp=-1", "outcomes": [o("1714", "Home (-1.0)", "1.55"), o("1715", "Away (+1.0)", "2.45")]},
                 {"id": "14", "specifier": "hcp=0:1", "outcomes": [o("1711", "Home (0:1)", "1.97"), o("1712", "Draw (0:1)", "4.30"),
                                                                   o("1713", "Away (0:1)", "3.10")]},
                 {"id": "854", "specifier": "total=2.5", "outcomes": [o("74", "Yes", "1.13"), o("76", "No", "5.30")]},
                 {"id": "900552", "specifier": "total=16.5", "outcomes": [o("12", "Over 16.5", "1.68"), o("13", "Under 16.5", "2.02")]},
                 {"id": "900393", "specifier": "total=8.5", "outcomes": [o("12", "Over 8.5", "1.70"), o("13", "Under 8.5", "2.00")]},
                 {"id": "800297", "outcomes": [o(f"{pp}:128383:2:1", "Rulli (Man City) 2+", "1.36"),
                                               o(f"{pp}:756514:2:1", "Donnarumma (Man City) 2+", "1.40"),
                                               o(f"{pp}:103335:3:2", "Leno (Fulham) 3+", "1.21"),
                                               o(f"{pp}:123207:3:2", "Lecomte (Fulham) 3+", "1.21")]},
                 {"id": "830297", "specifier": "pair=10406-1565", "outcomes": [o(f"{pp}:103335", "Leno", "1.20"),
                                                                               o(f"{pp}:756514", "Donnarumma", "4.85")]}]}]}]}
    row = N.sporty_rows(reply)[0]
    assert row["more"] == {"AH1M15": 1.96, "AH2P15": 1.85, "AH1M10": 1.55, "AH2P10": 2.45, "EH011": 1.97, "EH01X": 4.30,
                           "EH012": 3.10, "1|O25": 1.13, "SHHO165": 1.68, "SHHU165": 2.02, "STO85": 1.70, "STU85": 2.00,
                           "SVHO15": 1.40, "SVAO25": 1.21}                                 # the starting keepers' lines only
    assert row["sporty_ids"]["SVHO15"]["outcome"] == f"{pp}:756514:2:1" and row["sporty_ids"]["AH1M15"]["specifier"] == "hcp=-1.5"
    assert all(B.kind(k) for k in row["more"])                                             # the server takes every one


def test_players_to_score_get_their_own_slip():
    import importlib.util
    from engine import combos as C
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    o = lambda i, d, v: {"id": i, "desc": d, "odds": v}
    scorers = [o("1716", "No Goal", "0.00")] + [o(f"sr:player:{100 + k}", f"Player {k} (Man City)", f"{2.5 + k * 0.7:.2f}") for k in range(12)]
    reply = {"data": [{"name": "Premier League", "events": [{"homeTeamName": "Man City", "awayTeamName": "Fulham",
             "estimateStartTime": 1795264200000, "eventId": "sr:match:1", "markets": [
                 {"id": "1", "outcomes": [o("1", "Home", "1.40"), o("2", "Draw", "5.79"), o("3", "Away", "7.42")]},
                 {"id": "40", "specifier": "type=prematch", "outcomes": scorers}]}]}]}
    row = N.sporty_rows(reply)[0]
    ps = {k: v for k, v in row["more"].items() if k.startswith("PS")}
    assert len(ps) == N.SCORERS_KEPT and min(ps.values()) == 2.5 and ps["PS100"] == 2.5 and row["players"]["PS100"] == "Player 0 (Man City)"
    assert row["sporty_ids"]["PS100"] == {"event": "sr:match:1", "market": "40", "specifier": "type=prematch", "outcome": "sr:player:100"}
    _, rows = F.parse_push({"book": "sportybet", "rows": [{"home": "A", "away": "B", "t_ms": NOW, "odds": {"PS100": 2.5, "home": 1.4},
                                                            "players": {"PS100": "Player 0 (A)", "PS999": "x"}}]})
    assert rows[0]["odds"] == {"PS100": 2.5, "1": 1.4} and rows[0]["players"] == {"PS100": "Player 0 (A)"} and B.kind("PS100") == "scorers"
    games = []
    for g in range(6):
        prices = {f"PS{g}{k}": round(2.2 + g * 0.3 + k * 0.5, 2) for k in range(5)}
        games.append({"home": f"H{g}", "away": f"A{g}", "t_ms": NOW + (3 + g) * H, "league": "Premier League", "odds": {"1": 1.5, **prices},
                      "players": {k: f"Striker {k}" for k in prices},
                      "ids": {k: {"event": f"e{g}", "market": "40", "specifier": "type=prematch", "outcome": k} for k in prices}})
    books = {"sportybet": {"t_ms": NOW, "rows": games}}
    legs = C.scorer_legs(books, NOW, NOW + B.DAY)
    assert len(legs) == 30 and all(x["model"] == "scorer" for x in legs)
    got = C.scorers(books, NOW, 1, 10)
    s = got["slip"]
    assert s and 10 <= s["odds"] <= 11 and s["p"] is None and s["edge"] is None and s["coded"] and got["games"] == 6
    assert len({x["match_id"] for x in s["legs"]}) == s["n"] and all(x["p"] is None for x in s["legs"])
    longest = max(x["odds"] for x in s["legs"])
    for a in legs:                                                     # no slip in range has a shorter longest price
        for b in legs:
            if a["match_id"] < b["match_id"] and 10 <= a["odds"] * b["odds"] <= 11:
                assert max(a["odds"], b["odds"]) >= longest - 1e-9
    assert C.scorers(books, NOW, 1, 1.5)["slip"] is None                # no single scorer is that short
    assert C.scorers({"sportybet": {"t_ms": NOW - 3 * H, "rows": games}}, NOW, 1, 10)["slip"] is None   # stale odds


def test_substitution_to_score():
    """No substitute scores = e^(-expected goals x share); the share read from the books' own yes/no prices."""
    import importlib.util
    from engine import combos as C
    from engine import goals as G
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    assert N.sporty_more("900606", "", "76", "No") == "SUBN" and N.sporty_more("900606", "", "74", "Yes") == "SUBY"
    assert N.MORE.match("SUBN") and B.kind("SUBN") == "subs" and B.kind("SUBY") == "subs"
    t = NOW
    sharp, rows = [], []
    for k in range(B.SUB_MIN + 5):
        s = _sharp(f"Home {k}", f"Away {k}", t + (3 + k) * H, 1.6 + k * 0.05, 4.0, 5.5, read=t, sid=f"s{k}")
        sharp.append(s)
        fair = B.devig(s["odds"]) if s.get("odds") else s["fair"]
        lam = sum(G.fit(fair["1"], fair["X"], fair["2"]))
        no = math.exp(-lam * 0.18)                                   # the books price a share of 0.18
        rows.append({"home": s["home"], "away": s["away"], "t_ms": s["t_ms"],
                     "odds": {"1": 1.5, "SUBN": round(1 / no * 0.95, 3), "SUBY": round(1 / (1 - no) * 0.95, 3)}})
    board = B.build({"sportybet": {"t_ms": t, "rows": rows}}, sharp, [], {}, t)
    subs = [x for x in board if x["market"] in ("SUBN", "SUBY")]
    assert len(subs) == 2 * len(rows) and all(abs(x["sub_share"] - 0.18) < 0.01 for x in subs)
    assert all(x["model"] == "subs" and "lam" not in x and "pair" not in x for x in subs)
    no = next(x for x in subs if x["market"] == "SUBN")
    assert abs(no["edge"] - (no["odds"] * no["p"] - 1)) < 1e-12 and -0.08 < no["edge"] < 0      # the books' margin, about
    assert C.haircut(no) == C.HAIRCUT_CORNERS
    few = B.build({"sportybet": {"t_ms": t, "rows": rows[:3]}}, sharp, [], {}, t)          # too few games: the default share
    assert {round(x["sub_share"], 4) for x in few if x["market"] == "SUBN"} == {B.SUB_SHARE}


def test_rollover_legs_fill_the_day():
    """As many rollover legs a day as fit: the next one once the last has settled, from games later today only, the
    earliest qualifying slot first; and the combo builder's today-only window."""
    from engine import combos as C
    lagos_10 = NOW                                                    # 09:00 UTC = 10:00 in Lagos
    assert B.day_end(lagos_10) == int(datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_row(key="early", t_ms=NOW + 4 * H, odds=1.30, edge=0.0, p=0.77),
            _row(key="early_better", t_ms=NOW + 5 * H, odds=1.25, edge=0.0, p=0.80),   # the same slot (within 90 minutes)
            _row(key="late", t_ms=NOW + 9 * H, odds=1.20, edge=0.0, p=0.84),
            _row(key="tomorrow", t_ms=NOW + 20 * H, odds=1.20, edge=0.0, p=0.90)]
    assert B.rollover_pick(rows, NOW)["key"] == "early_better"         # the earliest slot, its most likely game
    assert B.rollover_pick(rows[2:], NOW)["key"] == "late"             # tomorrow's game waits for tomorrow
    bk = B.Book()
    first = bk.add_pick(_bet("early_better", 1.25, NOW + 5 * H, p=0.8), "2026-11-18", NOW)
    assert first and bk.add_pick(_bet("late", 1.20, NOW + 9 * H), "2026-11-18", NOW) is None      # the first is open
    bk.settle([{"home": "Big City", "away": "Rovers", "t_ms": NOW + 5 * H, "hg": 1, "ag": 0}])
    second = bk.add_pick(_bet("late", 1.20, NOW + 9 * H, home="Town", away="Albion"), "2026-11-18", NOW + 7 * H)
    assert second and second["leg"] == 2 and second["stake"] == 6250.0 and second["day"] == "2026-11-18"
    bk.settle([{"home": "Town", "away": "Albion", "t_ms": NOW + 9 * H, "hg": 2, "ag": 0}])
    assert bk.add_pick(_bet("again", 1.2, NOW + 8 * H, home="Ham", away="Eggs"), "2026-11-18", NOW + 11 * H) is None  # earlier than the last
    assert bk.chain["day"] == 2 and [p["status"] for p in bk.picks] == ["won", "won"]
    assert C.window_end(NOW, 0) == B.day_end(NOW) and C.window_end(NOW, 3) == NOW + 3 * D and 0 in C.DAYS


def test_a_leg_booked_under_old_rules_is_replaced():
    bk = B.Book()
    old = bk.add_pick(_bet("brighton", 1.15, NOW + 6 * H, p=0.86), "2026-11-21", NOW - 10 * H)     # booked under 1.15-1.35
    ok = lambda p: B.ROLL_MIN <= p["odds"] <= B.ROLL_MAX
    assert bk.replace_open(NOW, ok) is old and old["status"] == "replaced" and bk.open_pick() is None
    new = bk.add_pick(_bet("new", 1.30, NOW + 3 * H, home="Town", away="Albion"), "2026-11-22", NOW)   # earlier is fine
    assert new and new["leg"] == 1 and new["stake"] == 5000 and bk.last_pick() is new
    assert bk.replace_open(NOW, ok) is None                                       # it fits: kept
    assert bk.record()["rollover"]["picks"] == 0                                  # a replaced leg never counts
    bk.picks[-1]["odds"] = 1.10
    assert bk.replace_open(NOW + 4 * H, ok) is None                               # already kicked off: it stands


def test_1up_and_2up():
    """1Up: the side has led by 1 at any point (a win always has). 2Up: led by 2 at any point, or won."""
    import importlib.util
    from engine import goals as G
    L = G.leads(1.7, 1.0)
    p1 = float(G.grid(1.7, 1.0, rho=0.0)[G.MASKS["1"]].sum())
    assert p1 < L["UP21"] < L["UP11"] < 1 and L["UP12"] < L["UP11"] and L["UP22"] < L["UP12"]
    lone = G.leads(1.0, 0.0)                                         # only the home side scores: it leads once it scores
    assert abs(lone["UP11"] - (1 - math.exp(-1.0))) < 1e-9 and lone["UP12"] == 0
    flip = G.leads(1.0, 1.7)
    assert abs(flip["UP11"] - L["UP12"]) < 1e-12 and abs(flip["UP21"] - L["UP22"]) < 1e-12
    spec = importlib.util.spec_from_file_location("naijaOdds", Path(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    assert [N.sporty_more(mid, "", oid, "") for mid, oid in (("60200", "1"), ("60200", "3"), ("60100", "1"), ("60100", "3"), ("60200", "2"))] \
        == ["UP11", "UP12", "UP21", "UP22", None]
    assert all(N.MORE.match(c) for c in ("UP11", "UP22")) and [B.kind(c) for c in ("UP11", "UP12", "UP21", "UP22")] == ["up1", "up1", "up2", "up2"]
    sharp = [_sharp("Manchester City", "Fulham", NOW + 5 * H, 1.40, 5.8, 7.4, read=NOW, sid="s1")]
    books = {"sportybet": {"t_ms": NOW, "rows": [{"home": "Manchester City", "away": "Fulham", "t_ms": NOW + 5 * H,
                                                   "odds": {"1": 1.40, "UP11": 1.18, "UP21": 1.36}}]}}
    board = {x["market"]: x for x in B.build(books, sharp, [], {}, NOW)}
    assert board["UP11"]["model"] == "goals" and board["UP11"]["p"] > board["UP21"]["p"] > board["1"]["p"]


def test_results_come_from_espn_first(tmp_path, monkeypatch):
    """A finished rollover leg settles from ESPN's free scoreboard within minutes, with no Odds API credits spent."""
    import asyncio
    import api.bets as AB
    from api.bets import BetsService
    from ingestion import intel as I
    rt = type("RT", (), {"mode": "replay"})()
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json")
    asked = []

    async def board(sport, league, date=None):
        asked.append((league, date))
        return [{"completed": True, "start_ms": NOW, "home_score": 2, "away_score": 1,
                 "home": {"team": {"displayName": "Barcelona"}}, "away": {"team": {"displayName": "Real Betis"}}},
                {"completed": False, "start_ms": NOW + H, "home_score": 0, "away_score": 0,
                 "home": {"team": {"displayName": "Sevilla"}}, "away": {"team": {"displayName": "Getafe"}}}]
    monkeypatch.setattr(I, "fetch_scoreboard", board)
    called = []

    async def odds_api(*a, **k):
        called.append(a)
        return {}, None, []
    svc.fetch_odds_api = odds_api
    monkeypatch.setenv("ANALYTIX_ODDS_API_KEY", "x")
    leg = svc.book.add_pick(_bet("barca", 1.30, NOW, home="Barcelona", away="Real Betis", p=0.8) | {"league": "SP1"}, "2026-11-18", NOW - H)
    asyncio.run(svc._settle(NOW + 2 * H))
    assert leg["status"] == "won" and leg["score"] == "2-1" and not called            # no credits spent
    assert {lg for lg, _ in asked} == {"esp.1"} and svc.book.chain["day"] == 1
    assert AB.espn_slug("soccer_uefa_champs_league") == "uefa.champions" and AB.espn_slug("E2") == "eng.3"
    assert AB.espn_slug("soccer_fifa_world_cup_qualifiers_europe") is None


def test_the_combo_builder_takes_a_game_cap():
    from engine import combos as C
    assert C.games_cap(50) == 15 and C.games_cap(500) == 20 and C.games_cap(50, 30) == 30 and C.games_cap(500, 10) == 10
    assert C.games_cap(50, 12) == 15                                         # not one of the choices: the usual cap
    bets = [{"key": f"g{i}", "match_id": f"g{i}", "home": f"H{i}", "away": f"A{i}", "league": "E0", "t_ms": NOW + (3 + i) * H,
             "market": "1", "book": "bet9ja", "odds": o, "p": 0.98 / o, "p_void": 0.0, "chance": 0.98 / o, "edge": -0.02}
            for i, o in enumerate([1.20, 1.25, 1.30, 1.35] * 7 + [1.20, 1.25])]
    books = {"bet9ja": C.Builder(bets)}
    assert C.best_of(books, 20, cap=10) is None                              # the 10 longest come to about 18
    got = C.best_of(books, 20, cap=20)
    assert got and len(got) <= 20 and 20 <= math.prod(x["odds"] for x in got) <= 22
    out = C.build(books, 20, {"safest": {"20": {"n": 5}}}, cap=10)
    assert out["slip"] is None and out["settings"]["games"] == 10 and out["settings"]["cap"] == 10
    assert out["ladder"][5]["target"] == 20 and out["ladder"][5]["past"] is None            # not replayed at 10 games
    assert C.build(books, 20, {"safest": {"20": {"n": 5}}}, cap=15)["ladder"][5]["past"] == {"n": 5}    # 15 is the usual cap


def test_george_and_muiz_rollovers(tmp_path):
    """Friends' rollovers: a slip of today's games in their odds range, safest games first, on one book, rolled over
    like the main one in a paper book of their own; settled game by game."""
    from api.bets import BetsService
    from engine import combos as C
    rt = SimpleNamespace(mode="replay")
    svc = BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json")
    assert set(svc.lanes) == {"george", "muiz"} and B.LANES["george"]["range"] == (1.90, 2.30) and B.LANES["muiz"]["range"] == (9.80, 10.40)
    teams = [("Arsenal", "Fulham"), ("Chelsea", "Wolves"), ("Liverpool", "Everton"), ("Man City", "Burnley"), ("Newcastle", "Brentford"),
             ("Barcelona", "Alaves"), ("Real Madrid", "Getafe"), ("Bayern", "Mainz"), ("Inter", "Lecce"), ("Napoli", "Como"),
             ("PSG", "Lorient"), ("Ajax", "Heracles")]
    board = []
    for i, (h, a) in enumerate(teams):
        odds = 1.20 + 0.02 * i
        p = 0.99 / odds
        board.append({**_row(key=f"g{i}", home=h, away=a, odds=odds, edge=odds * p - 1, p=p, fair_p=p, t_ms=NOW + (3 + i * 0.25) * H),
                      "match_id": f"m{i}", "p_void": 0.0, "others": [{"book": "sportybet", "odds": odds, "sel": {"event": f"e{i}", "market": "1",
                                                                                                                "specifier": "", "outcome": "1"}}]})
    svc.books = {"sportybet": {"t_ms": NOW, "rows": []}}
    g = C.roll_slip(board, svc.books, NOW, 1.90, 2.30)
    assert g and 1.90 <= g["odds"] <= 2.30 * 1.0001 and g["n"] >= 2 and g["book"] == "sportybet" and g["coded"]
    m = C.roll_slip(board, svc.books, NOW, 9.80, 10.40)
    assert m and 9.80 <= m["odds"] <= 10.40 * 1.0001 and m["n"] > g["n"]
    assert C.roll_slip(board, svc.books, NOW + 20 * H, 1.9, 2.3) is None                     # tomorrow: nothing left today
    day = "2026-11-18"
    svc._roll(svc.lanes["george"], B.LANES["george"]["range"], board, NOW, day, slip=True)
    leg = svc.lanes["george"].picks[0]
    assert leg["legs"] and leg["stake"] == B.ROLL_START and leg["status"] == "open" and all(x["status"] == "open" for x in leg["legs"])
    view = svc._roll_view(svc.lanes["george"], B.LANES["george"]["range"], board, NOW, day, slip=True)
    assert view["today"] is leg and view["slip"] and view["range"] == [1.90, 2.30]
    games = leg["legs"]
    res = [{"home": x["home"], "away": x["away"], "t_ms": x["t_ms"], "hg": 2, "ag": 0} for x in games[:-1]]
    svc.lanes["george"].settle(res)
    assert leg["status"] == "open"                                                          # one game still to play
    svc.lanes["george"].settle(res + [{"home": games[-1]["home"], "away": games[-1]["away"], "t_ms": games[-1]["t_ms"], "hg": 3, "ag": 1}])
    assert leg["status"] == "won" and abs(svc.lanes["george"].chain["balance"] - B.ROLL_START * leg["odds"]) < 1e-6
    svc._roll(svc.lanes["muiz"], B.LANES["muiz"]["range"], board, NOW, day, slip=True)
    mz = svc.lanes["muiz"].picks[0]
    svc.lanes["muiz"].settle([{"home": mz["legs"][0]["home"], "away": mz["legs"][0]["away"], "t_ms": mz["legs"][0]["t_ms"], "hg": 0, "ag": 1}])
    assert mz["status"] == "lost" and svc.lanes["muiz"].chain["refills"] == 1                # lost on its first lost game
    job = svc._code_job(g)
    assert job["status"] == "waiting" and job["games"] == g["n"]
    svc.lanes["george"].save()
    assert B.Book(tmp_path / "bets_george.json").picks[0]["legs"]

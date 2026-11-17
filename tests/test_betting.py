"""The bets suite: names, fair prices, history, stats, the value list, the rollover pick, the paper book, readers."""
import asyncio
import random
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
    assert rec["n"] > 50 and rec["won"] / rec["n"] > 0.7
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
    assert rows == [{"match": "Spain - Georgia", "league": "UEFA Nations League", "time": 1794944700, "match_id": 9,
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
        g.update(B365H=g["PSH"] * 0.98, B365A=g["PSA"] * 0.98, MaxH=g["PSH"] * 1.06, MaxA=g["PSA"] * 1.06, PSD=g["PSD"])
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

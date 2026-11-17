"""The bets suite: names, fair prices, history, stats, the value list, the rollover pick, the paper book, readers."""
import asyncio
import random
from datetime import datetime, timezone
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

    async def go():
        for k in ("history", "fixtures", "sharp"):
            await svc.refresh(k)
    asyncio.run(go())
    assert svc.status["history"]["ok"] and svc.status["sharp"]["setup"] and len(svc.sharp()) == 1
    monkeypatch.setenv("ANALYTIX_BETS_TOKEN", "s3cret")
    with pytest.raises(PermissionError):
        svc.push({"book": "bet9ja", "rows": []}, "wrong")
    got = svc.push({"book": "bet9ja", "rows": [{"home": "Big City", "away": "Rovers", "t_ms": t + 8 * H, "odds": {"home": 1.31}}]}, "s3cret")
    assert got == {"book": "bet9ja", "games": 1, "matched": 1} and (tmp_path / "odds.json").exists()
    asyncio.run(svc.refresh("book"))
    pub = svc.public()
    assert pub["value"][0]["home"] == "Big City" and len(svc.book.flat) == 1
    assert pub["sources"]["books"]["bet9ja"]["games"] == 1 and pub["patterns"] and pub["matched"] == {"bet9ja": 1}
    assert BetsService(rt, tmp_path / "bets.json", tmp_path / "odds.json").books["bet9ja"]["rows"]   # survives a restart

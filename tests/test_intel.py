import math

import pytest

from engine.inplay import american_to_prob, devig, fit_rates, soccer_live, soccer_minute, three_way
from engine.intel import (
    best_trade, game_probs, headline_flags, league_for, match_game, name_score, news_query, news_spike, returns,
    rules_line,
)
from engine.options import build_surface, digital_above, distribution, norm_cdf, parse_instrument, touch_prob
from engine.sharp import Calibration, SharpBook, resolved_value

NOW = 1_791_000_000_000
DAY = 86_400_000


# ── options ─────────────────────────────────────────────────────────────────
def _rows(asset="BTC", fwd=100_000.0, iv=lambda k: 50.0, days=(2, 9, 30)):
    from datetime import datetime, timezone
    rows = []
    for d in days:
        exp = datetime.fromtimestamp((NOW + d * DAY) / 1000, timezone.utc)
        tag = f"{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"
        for k in range(60_000, 140_001, 5_000):
            for cp in "CP":
                rows.append({"instrument_name": f"{asset}-{tag}-{k}-{cp}", "mark_iv": iv(k), "underlying_price": fwd})
    return rows


def test_instruments_parse_including_usdc_and_decimal_strikes():
    a = parse_instrument("BTC-27DEC26-100000-C")
    assert a[0] == "BTC" and a[2] == 100000.0 and a[3] == "C"
    assert parse_instrument("XRP_USDC-27DEC26-0d5-P")[2] == 0.5
    assert parse_instrument("SOL_USDC-3OCT26-150-C")[0] == "SOL"
    assert parse_instrument("BTC-PERPETUAL") is None


def test_a_flat_smile_gives_the_plain_lognormal_answer():
    surf = build_surface("BTC", _rows(), NOW)
    assert len(surf.smiles) == 3
    t = surf.smiles[1].expiry_ms
    p = digital_above(surf, 100_000, 110_000, t, NOW)
    tau = (t - NOW) / 1000 / (365 * 86400)
    sd = 0.5 * math.sqrt(tau)
    assert p == pytest.approx(norm_cdf((math.log(100_000 / 110_000) - 0.5 * sd * sd) / sd), abs=1e-3)
    assert digital_above(surf, 100_000, 90_000, t, NOW) > 0.5 > p


def test_a_put_skew_makes_lines_just_above_the_price_likelier_than_the_plain_answer():
    skew = build_surface("BTC", _rows(iv=lambda k: 50 + 30 * (100_000 - k) / 40_000), NOW)
    flat = build_surface("BTC", _rows(iv=lambda k: 50.0), NOW)
    t = flat.smiles[1].expiry_ms
    assert digital_above(skew, 100_000, 105_000, t, NOW) > digital_above(flat, 100_000, 105_000, t, NOW)
    assert 0 < touch_prob(flat, 100_000, 110_000, t, NOW) <= 1


def test_the_ladder_turns_into_bands_and_shows_when_it_is_out_of_order():
    bands = distribution([1950, 2050], [0.30, 0.10])
    assert [round(b["p"], 2) for b in bands] == [0.7, 0.2, 0.1]
    assert distribution([1950, 2050], [0.0, 0.97])[1]["p"] < 0
    assert distribution([1950], [None]) == []


# ── the game ────────────────────────────────────────────────────────────────
def test_sportsbook_lines_lose_their_margin():
    assert american_to_prob(-150) == pytest.approx(0.6) and american_to_prob(130) == pytest.approx(100 / 230)
    assert sum(devig([american_to_prob(-150), american_to_prob(130)])) == pytest.approx(1.0)
    assert devig([0.5, None]) is None


def test_the_goals_model_matches_the_line_and_reacts_to_goals_and_time():
    lam_h, lam_a = fit_rates(0.45, 0.27, 0.28)
    w, d, l = three_way(lam_h, lam_a)
    assert (w, d, l) == pytest.approx((0.45, 0.27, 0.28), abs=0.01)
    start = soccer_live((0.45, 0.27, 0.28), 0, 0, 0)
    goal = soccer_live((0.45, 0.27, 0.28), 30, 1, 0)
    late = soccer_live((0.45, 0.27, 0.28), 88, 1, 0)
    assert goal[0] > start[0] and late[0] > goal[0] > 0.6
    assert soccer_live((0.45, 0.27, 0.28), 80, 0, 0)[1] > 0.5           # level late on: a draw is likeliest
    assert soccer_live((0.45, 0.27, 0.28), 93, 2, 1) == (1.0, 0.0, 0.0)
    assert soccer_minute("67'", 2) == 67 and soccer_minute("45'+2'", 1) == 47 and soccer_minute("", 1, "Halftime") == 45


def test_teams_match_by_name_or_code_and_city_rivals_stay_apart():
    assert name_score("Manchester City", "Manchester United") == 0.5
    assert name_score("Pittsburgh Steelers", "Pittsburgh Steelers") == 1.0
    games = [{"start_ms": NOW, "home": {"team": {"displayName": "Indianapolis Colts", "abbreviation": "IND"}},
              "away": {"team": {"displayName": "Pittsburgh Steelers", "abbreviation": "PIT"}}},
             {"start_ms": NOW, "home": {"team": {"displayName": "Manchester United", "abbreviation": "MUN"}},
              "away": {"team": {"displayName": "Arsenal", "abbreviation": "ARS"}}}]
    g, a_home = match_game("Pittsburgh Steelers", "Indianapolis Colts", "PIT", "IND", NOW + 3_600_000, games)
    assert g is games[0] and a_home is False
    assert match_game("Manchester City", "Arsenal", None, None, NOW, games) is None
    assert match_game("Pittsburgh Steelers", "Indianapolis Colts", "PIT", "IND", NOW + 3 * DAY, games) is None
    assert league_for("NFL") == ("football", "nfl") and league_for("UEFA Champions League") == ("soccer", "uefa.champions")


def test_the_game_gives_chances_from_the_result_the_live_model_or_the_line():
    base = {"home": {}, "away": {}, "sport": "soccer"}
    final = dict(base, state="post", completed=True, home_score=1, away_score=2)
    p, src, _ = game_probs(final, a_home=False)
    assert src == "final" and p["a"] == 1.0 and p["b"] == 0.0
    pre = dict(base, state="pre", odds={"home_ml": -120, "away_ml": 320, "draw_ml": 260, "provider": "DraftKings"})
    p, src, label = game_probs(pre, a_home=True)
    assert src == "book" and p["a"] + p["b"] + p["draw"] == pytest.approx(1.0) and "DraftKings" in label
    live = dict(pre, state="in", home_score=0, away_score=1, clock="75'", period=2)
    p, src, _ = game_probs(live, a_home=True)
    assert src == "goals" and p["b"] > 0.6
    nfl = dict(base, sport="football", state="in", win_prob={"home": 0.8, "away": 0.2}, detail="Q3 4:12")
    p, src, _ = game_probs(nfl, a_home=False)
    assert src == "espn" and p["a"] == pytest.approx(0.2)


# ── edge ────────────────────────────────────────────────────────────────────
def test_the_best_trade_picks_the_side_with_positive_expected_value():
    t = best_trade(0.60, 0.40, 50, 0.50, 30, days=4)
    assert t["side"] == "Yes" and t["ev"] == pytest.approx(0.2) and t["kelly"] == pytest.approx(0.2)
    assert t["contracts"] == 30 and t["usd"] == pytest.approx(15)
    no = best_trade(0.20, 0.40, 50, 0.50, 30, days=4)
    assert no["side"] == "No" and no["price"] == pytest.approx(0.6) and no["ev"] == pytest.approx(0.8 / 0.6 - 1)
    assert best_trade(0.45, 0.40, 50, 0.50, 30, days=4) is None
    r = returns(0.97, 4)
    assert r["if_right"] == pytest.approx(3 / 97) and r["per_year"] == pytest.approx(3 / 97 * 365 / 4)


# ── news ────────────────────────────────────────────────────────────────────
def test_a_news_spike_is_measured_against_the_names_own_normal():
    q = 15 * 60_000
    pts = [(NOW - i * q, 1.0) for i in range(8, 300)] + [(NOW - i * q, 6.0) for i in range(8)]
    s = news_spike(pts, NOW)
    assert s["recent"] == 48 and s["usual"] == 8 and s["ratio"] == 6
    assert headline_flags(["Star striker ruled out with hamstring injury", "Club confirms"]) == ["injury"]
    assert headline_flags(["Manager sacked after defeat"]) == ["leaves"]
    assert news_query(["Pittsburgh Steelers", "Indianapolis Colts"]) == '("Pittsburgh Steelers" OR "Indianapolis Colts") sourcelang:english'


def test_rules_read_in_plain_words():
    line = rules_line({"countedPlay": "regulation time, 90 minutes plus stoppage time", "officialSource": "UEFA",
                       "resolutionDeadline": "20261014-1900"}, "sports")
    assert line == "Rules: counts regulation time, 90 minutes plus stoppage time · source: UEFA · settles by 14 Oct 19:00 UTC."


# ── sharp wallets and calibration ──────────────────────────────────────────
def test_wallets_that_keep_being_right_get_marked_sharp():
    book = SharpBook()
    for i in range(20):
        t0 = NOW + i * 1000
        book.on_trade(1, {"t": t0, "px": 0.40, "sz": 10, "buyer": "0xsharp", "seller": "0xfish"}, t0)
    book.settle(NOW + 3 * 3_600_000, lambda oid: 0.472)               # two hours later Yes was at 47.2
    st = book.stats["2h"]["0xsharp"]
    assert st.n == 20 and st.avg == pytest.approx(7.2)
    assert book.label("0xsharp") == "sharp" and book.label("0xfish") == "fade"
    old = SharpBook()
    old.on_trade(1, {"t": NOW - 120_000, "px": 0.4, "sz": 1, "buyer": "0xa", "seller": "0xb"}, NOW)   # a catch-up snapshot
    assert old.summary()["pending"] == 0


def test_sharp_needs_consistency_not_one_lucky_trade():
    book = SharpBook()
    import random
    rng = random.Random(1)
    for i in range(40):
        t0 = NOW + i * 60_000
        book.on_trade(7, {"t": t0, "px": 0.5, "sz": 5, "buyer": "0xa", "seller": "0xb"}, t0)
    gains = iter([0.5 + rng.gauss(0.03, 0.02) for _ in range(200)])
    book.settle(NOW + 10 * 3_600_000, lambda oid: next(gains))
    assert book.label("0xa") == "sharp" and book.label("0xb") == "fade"
    flow = book.flow(7, NOW - 1)
    assert flow["sharp_net"] == pytest.approx(200) and flow["sharp_wallets"][0]["wallet"] == "0xa"
    assert book.summary()["sharp"] == 1


def test_calibration_compares_odds_with_what_happened():
    cal = Calibration()
    for oid, p, y in [(1, 0.92, 1), (2, 0.95, 1), (3, 0.91, 0), (4, 0.15, 0)]:
        cal.note(oid, NOW + 3_600_000, p, NOW)                    # an hour before the close: all three windows
        cal.resolve(oid, y)
    t = cal.table("1h")
    top = [r for r in t["rows"] if r["band"] == "90–100%"][0]
    assert t["settled"] == 4 and top["n"] == 3 and top["happened"] == pytest.approx(2 / 3)
    assert resolved_value(0.99) == 1 and resolved_value(0.01) == 0 and resolved_value(0.5) is None

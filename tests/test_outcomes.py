import asyncio
import math
import time
from types import SimpleNamespace

import pytest

from api.outcomes import OutcomeService, outcome_of
from engine.outcomes import (
    build_markets, fair_above, fair_touch, flow_of, odds, parse_fields, parse_time, push_cost,
    sigma_per_sqrt_s, sum_check, who, why_line,
)

# Shapes copied from the live probe (scripts/probeOutcomes.py), trimmed
META = {
    "outcomes": [
        {"outcome": 10464, "name": "template:binaryPrice",
         "description": "perp:xyz:SILVER|priceDescription:1-minute candle|seconds:300|threshold:60.7|time:20261012-1335",
         "sideSpecs": [{"name": "template:Yes"}, {"name": "template:No"}], "quoteToken": "USDC", "venue": "txyz"},
        {"outcome": 11214, "name": "template:binaryPrice",
         "description": "perp:ETH|priceDescription:ETH-USDC perp mark|seconds:60|threshold:2538.8|time:20261011-1830",
         "sideSpecs": [{"name": "template:Yes"}, {"name": "template:No"}], "venue": "out", "deployerFeeScale": "1.0"},
        {"outcome": 10823, "name": "template fallback", "description": "", "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
         "venue": "out"},
        {"outcome": 10824, "name": "template:sportsContestParticipant2", "description": "participant:RB Leipzig",
         "sideSpecs": [{"name": "Yes"}, {"name": "No"}], "venue": "out"},
        {"outcome": 10825, "name": "template:sportsContestParticipant2", "description": "participant:PSV Eindhoven",
         "sideSpecs": [{"name": "Yes"}, {"name": "No"}], "venue": "out"},
        {"outcome": 10826, "name": "template:sportsContestDraw2", "description": "",
         "sideSpecs": [{"name": "Yes"}, {"name": "No"}], "venue": "out"},
        {"outcome": 9001, "name": "Recurring",
         "description": "class:priceBinary|underlying:BTC|expiry:20261012-0600|targetPrice:65000|period:1d",
         "sideSpecs": [{"name": "Yes"}, {"name": "No"}]},
        {"outcome": 9002, "name": "template:sportsTotal", "description": "participant:Arsenal|line:2.5",
         "sideSpecs": [{"name": "Over"}, {"name": "Under"}], "venue": "out"},
    ],
    "questions": [
        {"question": 411, "name": "template:sportsContestResult",
         "description": "competition:UEFA Champions League|contestType:Match|participantA:RB Leipzig|"
                        "participantB:PSV Eindhoven|resolutionDeadline:20261014-1900|scheduledStart:20261013-1900|"
                        "stage:Matchday 2",
         "fallbackOutcome": 10823, "namedOutcomes": [10824, 10825, 10826], "settledNamedOutcomes": []},
    ],
}


def test_descriptions_split_on_the_first_colon_only():
    f = parse_fields("perp:xyz:SILVER|threshold:60.7|time:20261012-1335")
    assert f == {"perp": "xyz:SILVER", "threshold": "60.7", "time": "20261012-1335"}
    assert parse_fields("") == {}
    assert parse_time("20261011-1830") == 1791743400000
    assert parse_time("soon") is None


def test_the_listing_becomes_readable_markets():
    markets, questions = build_markets(META)
    eth = markets[11214]
    assert (eth.kind, eth.underlying, eth.threshold, eth.venue_name) == ("price", "ETH", 2538.8, "Outcome")
    assert eth.title == "ETH above 2,538.8 at 11 Oct 18:30 UTC"
    assert (eth.yes_coin, eth.no_coin) == ("#112140", "#112141")
    silver = markets[10464]
    assert silver.underlying == "xyz:SILVER" and silver.title.startswith("SILVER above 60.7") and silver.venue_name == "trade.xyz"
    old = markets[9001]                                         # the older "Recurring" format
    assert (old.kind, old.underlying, old.threshold) == ("price", "BTC", 65000)
    q = questions[411]
    assert q.title == "RB Leipzig vs PSV Eindhoven" and "UEFA Champions League" in q.sub and "Matchday 2" in q.sub
    assert q.outcomes == (10824, 10825, 10826, 10823)
    assert markets[10826].title == "RB Leipzig vs PSV Eindhoven: Draw"
    assert markets[10823].label == "None of these" and markets[10824].label == "RB Leipzig"
    assert markets[10824].kind == "sports" and markets[10824].expiry_ms == parse_time("20261014-1900")
    total = markets[9002]
    assert total.label == "Arsenal (line 2.5)" and total.sides == ("Over", "Under")


def test_odds_use_the_mid_unless_the_book_is_too_wide():
    assert odds(0.60, 0.62, 0.5) == pytest.approx(0.61)
    assert odds(0.008, 0.997, 0.52) == 0.52                     # an empty-ish book: the last trade says more
    assert odds(0.008, 0.997, None) == pytest.approx(0.5025)
    assert odds(None, None, None) is None


def test_push_cost_counts_dollars_to_move_the_price():
    asks = [(0.50, 100), (0.52, 100), (0.56, 100)]
    assert push_cost(asks, 5) == pytest.approx(0.50 * 100 + 0.52 * 100)
    assert push_cost([(0.5, 10)], 5) is None                    # the book doesn't reach that far


def test_fair_odds_follow_distance_time_and_volatility():
    s = 0.0005                                                  # per sqrt second: about 15% a day
    assert fair_above(100, 100, s, 3600) == pytest.approx(0.5, abs=0.01)
    assert fair_above(102, 100, s, 3600) > 0.7
    assert fair_above(101, 100, s, 3600) > fair_above(101, 100, s, 86_400)     # more time, more chance to fall back
    assert fair_above(99, 100, s, 0) == 0.0 and fair_above(100, 100, s, 0) == 1.0
    assert fair_touch(100, 101, s, 3600) == pytest.approx(2 * fair_above(100, 101, s, 3600), abs=0.02)
    assert fair_touch(100, 101, s, 3600) > fair_above(100, 101, s, 3600)       # touching is easier than closing above
    closes = [100 * math.exp(0.001 * (1 if i % 2 else -1)) for i in range(200)]
    assert sigma_per_sqrt_s(closes, 60) == pytest.approx(0.002 / math.sqrt(60), rel=0.05)
    assert sigma_per_sqrt_s(closes[:10], 60) is None


def test_the_add_up_check_finds_locked_in_edges():
    # (bid, bid size, ask, ask size, odds)
    cheap = sum_check([(0.30, 50, 0.32, 40, 0.31), (0.30, 50, 0.31, 90, 0.305), (0.30, 50, 0.31, 70, 0.305)])
    assert cheap.way == "yes" and cheap.locked == pytest.approx(0.06) and cheap.sets == 40
    assert "6.0¢ is locked in" in cheap.note
    rich = sum_check([(0.40, 20, 0.45, 50, 0.42), (0.35, 30, 0.40, 50, 0.37), (0.30, 25, 0.33, 50, 0.31)])
    assert rich.way == "no" and rich.locked == pytest.approx(0.05) and rich.sets == 20
    fair = sum_check([(0.30, 1, 0.36, 1, 0.33), (0.30, 1, 0.36, 1, 0.33), (0.30, 1, 0.36, 1, 0.34)])
    assert fair.way is None and fair.note == "The odds add up to 100%."
    gap = sum_check([(0.3, 1, None, 0, 0.3), (0.3, 1, 0.3, 1, 0.3)])
    assert gap.way is None                                      # can't buy every Yes: no edge claimed


def _t(t, px, sz, side, buyer, seller):
    return {"t": t, "px": px, "sz": sz, "side": side, "buyer": buyer, "seller": seller, "tid": t}


def test_who_flags_two_wallets_trading_with_each_other():
    a, b = "0x2343dc81ad8e3b3ef350d49ae3376d0dcbf66ad2", "0x0e3c8b07716c51cebe30b9073bd42d780c607bc1"
    trades = [_t(1, 0.52, 9, "A", a, b), _t(2, 0.54, 8, "B", b, a), _t(3, 0.53, 10, "B", a, b)]
    w = who(trades)
    assert w["count"] == 2 and w["contracts"] == 27
    assert any("buying from and selling to each other" in n for n in w["notes"])
    busy = [_t(i, 0.5, 1, "B", f"0x{i:040x}", f"0x{i + 100:040x}") for i in range(10)]
    assert "no single wallet or pair stands out" in who(busy)["notes"][0]


def test_why_line_says_what_moved_the_odds():
    markets, _ = build_markets(META)
    eth, draw = markets[11214], markets[10826]
    assert why_line(draw, 0.41, 0.40, "the last hour") == "Odds steady around 41% over the last hour."
    flow = flow_of([_t(10, 0.5, 300, "B", "0xaaa", "0xbbb"), _t(11, 0.5, 20, "A", "0xccc", "0xddd")], 0)
    line = why_line(draw, 0.52, 0.40, "the last hour", flow)
    assert line.startswith("Odds rose 12 points (40% to 52%)") and "280 contracts of net Yes buying" in line
    assert "mostly from one wallet" in line
    followed = why_line(eth, 0.70, 0.40, "the last hour", None, 0.72, 0.41, 2550.0, 2530.0)
    assert "in line with ETH moving +0.79% (now above 2,538.8)" in followed
    trading = why_line(eth, 0.70, 0.40, "the last hour", flow, 0.41, 0.40, 2530.5, 2530.0)
    assert "the move came from trading here" in trading
    assert why_line(draw, 0.52, 0.40, "the last hour", None).endswith("quotes moved, not trades.")


def test_coins_map_back_to_outcomes():
    assert outcome_of("#112140") == 11214
    assert outcome_of("#112141") is None and outcome_of("ETH") is None and outcome_of("#x") is None


# ── the service ────────────────────────────────────────────────────────────
def _svc():
    rt = SimpleNamespace(mode="live", pipelines={}, scanner=SimpleNamespace(contexts={"ETH": {"mark": 2540.0}}))
    svc = OutcomeService(rt)

    async def meta():
        return META

    async def book(coin):
        return {"coin": coin, "time": 1, "levels": [[{"px": "0.30", "sz": "10", "n": 1}], [{"px": "0.31", "sz": "5", "n": 1}]]}

    async def trades(coin):
        return [{"coin": coin, "side": "B", "px": "0.31", "sz": "4", "time": int(time.time() * 1000) - 60_000, "tid": 7,
                 "users": ["0xAAA", "0xBBB"]}]

    async def candles(coin, interval, n=0):
        now = int(time.time() * 1000)
        if coin.startswith("#"):
            return [{"t": now - (30 - i) * 60_000, "c": str(0.3 + i * 0.001)} for i in range(30)]
        return [{"t": now - (800 - i) * 60_000, "c": str(2500 * math.exp(0.0005 * (1 if i % 2 else -1)))} for i in range(800)]

    svc.fetch_meta, svc.fetch_book, svc.fetch_trades, svc.fetch_candles = meta, book, trades, candles
    return svc


def test_the_service_lists_markets_with_live_odds_and_the_add_up_check():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    assert len(svc.markets) == 8 and len(svc._followed) == 8
    for oid, (bid, ask) in {10824: ("0.30", "0.32"), 10825: ("0.30", "0.31"), 10826: ("0.30", "0.31"),
                            10823: ("0.01", "0.02")}.items():
        svc.on_message({"channel": "l2Book", "data": {"coin": f"#{oid * 10}", "time": 5, "levels": [
            [{"px": bid, "sz": "50", "n": 1}], [{"px": ask, "sz": "40", "n": 1}]]}})
    svc.on_message({"channel": "activeSpotAssetCtx", "data": {"coin": "#108240", "ctx": {
        "markPx": "0.31", "prevDayPx": "0.25", "dayBaseVlm": "1139", "circulatingSupply": "596"}}})
    out = svc.public()
    rows = {r["id"]: r for r in out["markets"]}
    assert rows[10824]["odds"] == pytest.approx(0.31) and rows[10824]["chg_24h"] == pytest.approx(6.0)
    assert rows[10824]["volume_24h"] == 1139 and rows[10824]["open"] == 596
    q = out["questions"][0]
    assert q["sum"]["way"] == "yes" and q["sum"]["locked"] == pytest.approx(1 - (0.32 + 0.31 + 0.31 + 0.02))
    assert {v["name"] for v in out["venues"]} == {"Outcome", "trade.xyz", "Hyperliquid"}


def test_a_market_page_follows_its_trades_and_prices_the_fair_odds():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    d = asyncio.run(svc.detail(11214))
    assert 11214 in svc.opened and d["trades"][0]["buyer"] == "0xaaa"
    assert d["book"]["bids"] == [(0.30, 10.0)]                   # read once over REST: the socket isn't connected
    assert d["row"]["kind"] == "price" and d["fair"]["spot"] == 2540.0
    assert d["chart"]["odds"] and d["why"]
    svc.on_message({"channel": "trades", "data": [{"coin": "#112140", "side": "A", "px": "0.30", "sz": "2",
                                                   "time": int(time.time() * 1000), "tid": 8, "users": ["0xccc", "0xddd"]}]})
    assert [t["tid"] for t in svc.trades[11214]] == [7, 8]
    assert asyncio.run(svc.detail(424242)) is None


def test_the_socket_asks_for_the_right_feeds():
    svc = _svc()
    sent = []
    svc.sock = SimpleNamespace(set_subscriptions=lambda subs: sent.extend(subs), server_error=None)
    asyncio.run(svc.refresh_meta())
    kinds = {s["subscription"]["type"] for s in sent}
    assert kinds == {"l2Book", "activeAssetCtx"}                 # replies arrive on activeSpotAssetCtx
    assert {"type": "activeAssetCtx", "coin": "#112140"} in [s["subscription"] for s in sent]
    assert svc.feed_status()["feeds"] == 16


def test_matchups_read_as_a_vs_b_and_partial_questions_say_so():
    from engine.outcomes import market_from
    m = market_from({"outcome": 1, "name": "template:sportsContestWinner", "venue": "out",
                     "description": "competition:NFL|participantA:Pittsburgh Steelers|participantB:Indianapolis Colts|"
                                    "shortNameA:PIT|shortNameB:IND|scheduledStart:20261012-1700"})
    assert m.title == "Pittsburgh Steelers vs Indianapolis Colts" and m.sub == "NFL · starts 12 Oct 17:00 UTC"
    part = sum_check([(0.2, 1, 0.21, 1, 0.205), (0.1, 1, 0.12, 1, 0.11), (None, 0, None, 0, None)])
    assert part.note == "2 of 3 outcomes have a price; those add up to 32%."


def test_a_market_with_no_orders_and_no_trades_has_no_odds():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    svc.on_message({"channel": "activeSpotAssetCtx", "data": {"coin": "#108230", "ctx": {
        "markPx": "0.5", "prevDayPx": "0.5", "dayBaseVlm": "0", "circulatingSupply": "45000"}}})
    assert svc.quote(svc.markets[10823])[4] is None             # the 0.5 mark is a placeholder, not a price
    svc.on_message({"channel": "activeSpotAssetCtx", "data": {"coin": "#108230", "ctx": {
        "markPx": "0.03", "prevDayPx": "0.02", "dayBaseVlm": "120", "circulatingSupply": "45000"}}})
    assert svc.quote(svc.markets[10823])[4] == 0.03             # traded today: the mark counts


def test_side_names_fill_in_their_templates():
    from engine.outcomes import market_from
    m = market_from({"outcome": 1, "name": "template:sportsContestWinner", "venue": "out",
                     "sideSpecs": [{"name": "{shortNameA}"}, {"name": "{shortNameB}"}],
                     "description": "competition:NFL|participantA:Atlanta Falcons|participantB:Baltimore Ravens|"
                                    "shortNameA:ATL|shortNameB:BAL"})
    assert m.sides == ("Atlanta Falcons", "Baltimore Ravens")
    short = market_from({"outcome": 2, "name": "x", "sideSpecs": [{"name": "{shortNameA}"}, {"name": "{line} or less"}],
                         "description": "shortNameA:ATL|line:2.5"})
    assert short.sides == ("ATL", "2.5 or less")


def test_an_event_page_charts_its_likeliest_outcomes():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    for oid, (bid, ask) in {10824: ("0.30", "0.32"), 10825: ("0.29", "0.31"), 10826: ("0.33", "0.35")}.items():
        svc.on_message({"channel": "l2Book", "data": {"coin": f"#{oid * 10}", "time": 5, "levels": [
            [{"px": bid, "sz": "50", "n": 1}], [{"px": ask, "sz": "40", "n": 1}]]}})
    d = asyncio.run(svc.question_detail(411))
    assert d["question"]["title"] == "RB Leipzig vs PSV Eindhoven" and len(d["rows"]) == 4
    assert [s["label"] for s in d["series"]] == ["RB Leipzig", "PSV Eindhoven", "Draw"]   # listing order, priced only
    assert d["series"][0]["points"][-1][1] == pytest.approx(0.31)
    assert asyncio.run(svc.question_detail(999)) is None


def test_edges_are_measured_against_prices_you_can_trade():
    from engine.outcomes import trade_edge
    assert trade_edge(0.60, 0.40, 0.50) == pytest.approx(10.0)         # fair above the ask: buy Yes
    assert trade_edge(0.30, 0.40, 0.50) == pytest.approx(-10.0)        # fair below the bid: sell Yes
    assert trade_edge(0.45, 0.40, 0.50) == 0.0                         # inside the spread: nothing to do
    assert trade_edge(0.0, None, None) is None                         # no orders: a stale mark proves nothing
    assert trade_edge(0.0, None, 0.99) == 0.0


def test_the_ladder_check_flags_a_higher_line_selling_above_a_lower_line():
    from engine.outcomes import ladder_check
    t = 1_800_000_000_000
    rows = [{"id": 1, "kind": "price", "threshold": 1950, "expiry_ms": t, "bid": None, "bid_sz": 0, "ask": 0.02, "ask_sz": 40},
            {"id": 2, "kind": "price", "threshold": 2050, "expiry_ms": t, "bid": 0.10, "bid_sz": 25, "ask": 0.97, "ask_sz": 5},
            {"id": 3, "kind": "price", "threshold": 1600, "expiry_ms": t, "bid": 0.99, "bid_sz": 9, "ask": None, "ask_sz": 0}]
    found = ladder_check(rows)
    assert [(f["low"], f["high"]) for f in found] == [(1, 2)]
    assert found[0]["locked"] == pytest.approx(0.08) and found[0]["pairs"] == 25
    assert "Yes on 1,950 plus No on 2,050 locks in 8.0¢ a pair" in found[0]["note"]
    fine = [dict(rows[0], ask=0.12), rows[1]]
    assert ladder_check(fine) == []


def test_fair_odds_use_volatility_that_suits_the_market_length():
    svc = _svc()
    svc.vol[("ETH", "short")] = (time.time(), 0.00001)                 # a sleepy weekend afternoon
    svc.vol[("ETH", "long")] = (time.time(), 0.0002)                   # two normal weeks
    assert svc.sigma("ETH", 3600) == 0.00001 and svc.sigma("ETH", 4 * 86_400) == 0.0002
    assert svc.sigma("BTC", 3600) is None
    del svc.vol[("ETH", "short")]
    assert svc.sigma("ETH", 3600) == 0.0002                            # falls back to what it has


# ── independent reads wired into the list ─────────────────────────────────
def _book(svc, oid, bid, ask, size="50"):
    svc.on_message({"channel": "l2Book", "data": {"coin": f"#{oid * 10}", "time": 5, "levels": [
        [{"px": bid, "sz": size, "n": 1}] if bid else [], [{"px": ask, "sz": size, "n": 1}] if ask else []]}})


def test_price_markets_use_the_options_market_when_there_is_one():
    from engine.options import build_surface
    from tests.test_intel import _rows
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    m = svc.markets[11214]
    now = int(time.time() * 1000)
    m.expiry_ms = now + 3 * 86_400_000
    svc.intel.surfaces["ETH"] = build_surface("ETH", _rows("ETH", fwd=2540.0, iv=lambda k: 60.0, days=(2, 9)), now)
    _book(svc, 11214, "0.20", "0.25", "200")
    r = svc.row(m, now)
    assert r["ref"]["source"] == "options" and 0.4 < r["fair"] < 0.6
    assert r["trade"]["side"] == "Yes" and r["trade"]["ev"] > 0.5        # Yes at 25¢ against roughly even odds
    board = svc.edges([r], now)
    assert board and board[0]["id"] == 11214 and board[0]["source"] == "options"


def test_a_finished_game_prices_its_market_and_alerts_once():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    now = int(time.time() * 1000)
    for o in (10824, 10825, 10826, 10823):
        svc.markets[o].expiry_ms = now + 86_400_000
    game = {"state": "post", "completed": True, "home_score": 2, "away_score": 0, "sport": "soccer",
            "home": {"team": {"displayName": "RB Leipzig"}}, "away": {"team": {"displayName": "PSV Eindhoven"}}}
    svc.intel.matches["q411"] = (game, True)
    _book(svc, 10824, "0.80", "0.84", "100")                            # Leipzig won, Yes still at 84¢
    r = svc.row(svc.markets[10824], now)
    assert r["ref"]["source"] == "final" and r["trade"]["side"] == "Yes" and r["game"]["score_a"] == 2
    assert svc.row(svc.markets[10823], now)["ref"]["p"] == 0.0          # "none of these" can't happen now
    pushed = []
    svc.rt.alerts = SimpleNamespace(cfg={"rules": {"pm_edge": {"on": True, "min_ev_pct": 8.0}}}, push=pushed.append)
    svc._samples_taken = 10
    svc._edge_alerts(now)
    svc._edge_alerts(now)
    assert len(pushed) == 1 and "buy Yes at 84.0¢" in pushed[0].title and pushed[0].kind == "pm_edge"


def test_trades_feed_the_sharp_book_and_settled_markets_feed_calibration():
    svc = _svc()
    asyncio.run(svc.refresh_meta())
    now = int(time.time() * 1000)
    svc.on_message({"channel": "trades", "data": [{"coin": "#108240", "side": "B", "px": "0.30", "sz": "10",
                                                   "time": now, "tid": 1, "users": ["0xAA", "0xBB"]}]})
    assert sum(len(q) for q in svc.intel.sharp.pending.values()) == 4     # two wallets, two horizons
    svc.intel.calib.note(10824, now + 3_600_000, 0.95, now)
    svc.last_odds[10824] = 0.99
    meta = {"outcomes": [o for o in META["outcomes"] if o["outcome"] != 10824], "questions": []}

    async def fewer():
        return meta
    svc.fetch_meta = fewer
    asyncio.run(svc.refresh_meta())
    assert svc.intel.calib.results[-1][0] == 10824 and svc.intel.calib.results[-1][2] == 1

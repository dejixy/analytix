import asyncio
from types import SimpleNamespace

import pytest

from api.planner import PlannerService
from api.positions import PositionWatch, WatchError
from engine.alerts import DEFAULT_RULES
from engine.planner import rough_model, simulate
from engine.positions import Position, assess, log_distance, parse_account, protective_orders
from ingestion.account import valid_address

ADDR = "0x" + "ab" * 20
STATE = {
    "assetPositions": [
        {"position": {"coin": "ETH", "szi": "10.0", "entryPx": "2500.0", "positionValue": "24000.0",
                      "unrealizedPnl": "-1000.0", "returnOnEquity": "-0.4", "liquidationPx": "2300.0",
                      "marginUsed": "2400.0", "leverage": {"type": "cross", "value": 10}}, "type": "oneWay"},
        {"position": {"coin": "BTC", "szi": "-0.5", "entryPx": "80000", "positionValue": "41000",
                      "unrealizedPnl": "-1000", "returnOnEquity": "-0.12", "liquidationPx": None,
                      "marginUsed": "8200", "leverage": {"type": "isolated", "value": 5}}, "type": "oneWay"},
        {"position": {"coin": "SOL", "szi": "0.0", "entryPx": "100", "positionValue": "0"}, "type": "oneWay"},
    ],
    "marginSummary": {"accountValue": "20000", "totalMarginUsed": "10600"},
    "withdrawable": "9000", "time": 1790000000000,
}
ORDERS = [
    {"coin": "ETH", "side": "A", "isTrigger": True, "triggerPx": "2350", "orderType": "Stop Market"},
    {"coin": "ETH", "side": "A", "isTrigger": True, "triggerPx": "2320", "orderType": "Stop Market"},
    {"coin": "ETH", "side": "A", "isTrigger": True, "triggerPx": "2600", "orderType": "Take Profit Market"},
    {"coin": "ETH", "side": "A", "isTrigger": True, "triggerPx": "2700", "orderType": "Take Profit Limit"},
    {"coin": "ETH", "side": "A", "isTrigger": False, "limitPx": "2550", "orderType": "Limit"},
    {"coin": "ETH", "side": "B", "isTrigger": True, "triggerPx": "2200", "orderType": "Stop Market"},   # wrong side
    {"coin": "BTC", "side": "B", "isTrigger": True, "triggerPx": "84000", "orderType": "Stop Market"},
]


def test_a_wallet_is_read_into_positions_with_their_stops_and_targets():
    acct = parse_account(ADDR, STATE, ORDERS, {"ETH": 2400.0, "BTC": 82000.0})
    assert acct.value == 20000 and acct.margin_used == 10600 and len(acct.positions) == 2     # the empty SOL is skipped
    btc, eth = acct.positions                                   # biggest first
    assert eth.side == 1 and eth.size == 10 and eth.mark == 2400 and eth.value == 24000 and eth.cross
    assert eth.liq_price == 2300 and eth.leverage == 10
    assert eth.stop == 2350 and eth.target == 2600              # the ones that would fire first
    assert btc.side == -1 and btc.liq_price is None and not btc.cross and btc.stop == 84000 and btc.target is None


def test_protective_orders_ignore_the_wrong_side_and_plain_limits():
    # for a short, ETH's buy-side stop at 2,200 is below the price: not a stop-loss, and not a take-profit order
    assert protective_orders(ORDERS, "ETH", -1, 2400.0) == (None, None)
    assert protective_orders([], "ETH", 1, 2400.0) == (None, None)


def test_addresses_are_checked():
    assert valid_address(ADDR) and not valid_address("0x123") and not valid_address("ab" * 21)


def _pos(liq, side=1, mark=2400.0, stop=None, target=None):
    return Position("ETH", side, 10, 2500, mark, 10 * mark, 0.0, 0.0, liq, 2400, 10, True, stop, target)


def _paths(pos, hours, step):
    m = rough_model(2.0, step, 0)                               # ~5.9% a day
    d = log_distance(pos.mark, pos.liq_price, pos.side, True)
    s = log_distance(pos.mark, pos.stop, pos.side, True)
    t = log_distance(pos.mark, pos.target, pos.side, False)
    br = (s, t) if (s or t) else None
    return simulate(m, hours, None, 0, liq=(pos.side, d if d is not None else float("inf")), bracket=br), m.kind


def test_odds_grow_as_liquidation_gets_closer_and_with_time():
    near, far = _pos(2300.0), _pos(1200.0)
    r_near = assess(near, _paths(near, 1, 300)[0], _paths(near, 24, 3600)[0], 0.0000125)
    r_far = assess(far, _paths(far, 1, 300)[0], _paths(far, 24, 3600)[0], 0.0000125)
    assert 0.1 < r_near.p_liq_24h < 0.9 and r_near.p_liq_1h < r_near.p_liq_24h
    assert r_far.p_liq_24h < 0.001
    assert abs(r_near.liq_pct - (2300 / 2400 - 1) * 100) < 1e-9 and 0.3 < r_near.liq_moves < 1.5
    assert r_near.funding_day == pytest.approx(0.0000125 * 24 * 24000)            # a long pays positive funding
    short = _pos(None, side=-1)
    r = assess(short, _paths(short, 1, 300)[0], _paths(short, 24, 3600)[0], 0.0000125)
    assert r.p_liq_24h == 0 and r.liq_moves is None and r.funding_day < 0


def test_stop_and_target_odds_come_from_which_fires_first():
    pos = _pos(2000.0, stop=2380.0, target=2420.0)              # both very close: almost surely one fires
    r = assess(pos, _paths(pos, 1, 300)[0], _paths(pos, 24, 3600)[0], None)
    assert r.p_stop_24h + r.p_target_24h > 0.95 and r.p_stop_24h > r.p_target_24h * 0.6
    beyond = _pos(2300.0, stop=2250.0)                          # stop past liquidation
    r = assess(beyond, _paths(beyond, 1, 300)[0], _paths(beyond, 24, 3600)[0], None)
    assert r.stop_beyond_liq and r.p_stop_24h == 0


# ── the service ─────────────────────────────────────────────────────────────
class _Planner(PlannerService):
    async def model(self, coin, step_s):
        return rough_model(2.0, step_s, 0)


def _watch(tmp_path):
    pushed = []
    alerts = SimpleNamespace(cfg={"rules": {k: dict(v) for k, v in DEFAULT_RULES.items()}}, push=pushed.append)
    rt = SimpleNamespace(mode="live", pipelines={}, alerts=alerts)
    rt.planner = _Planner(rt)
    w = PositionWatch(rt, tmp_path / "watch.json")
    liq = {"px": "2300.0"}

    async def account(addr):
        st = {**STATE, "assetPositions": [dict(STATE["assetPositions"][0])]}
        st["assetPositions"][0] = {"position": {**STATE["assetPositions"][0]["position"], "liquidationPx": liq["px"]}}
        return st

    async def orders(addr):
        return []

    async def market():
        return {"ETH": {"mark": 2400.0, "funding": 0.0000125, "max_leverage": 25.0}}
    w.fetch_account, w.fetch_orders, w.fetch_market = account, orders, market
    return w, pushed, liq


def test_wallets_are_validated_saved_and_removed(tmp_path):
    w, _, _ = _watch(tmp_path)
    with pytest.raises(WatchError):
        w.add("not-an-address")
    w.add(ADDR, "Main")
    with pytest.raises(WatchError):
        w.add(ADDR.upper().replace("0X", "0x"))
    assert PositionWatch(w.rt, tmp_path / "watch.json").wallets == [{"address": ADDR, "label": "Main"}]   # saved
    w.remove(ADDR)
    assert w.public()["wallets"] == []


def test_a_poll_scores_positions_and_alerts_once_per_crossing(tmp_path):
    w, pushed, liq = _watch(tmp_path)
    w.add(ADDR, "Main")
    asyncio.run(w.poll())
    acct = w.public()["wallets"][0]["account"]
    (p,) = acct["positions"]
    assert p["coin"] == "ETH" and p["risk"]["p_liq_24h"] > 0.05 and p["risk_error"] is None
    assert len(pushed) == 1 and pushed[0].kind == "position"
    assert pushed[0].title.startswith("ETH long: ") and "chance of liquidation in the next 24h" in pushed[0].title
    assert any("Liquidation at 2,300.00" in line for line in pushed[0].lines)
    asyncio.run(w.poll())
    assert len(pushed) == 1                                     # still above the line: no repeat
    liq["px"] = "1200.0"
    asyncio.run(w.poll())                                       # far away now: re-arms
    liq["px"] = "2300.0"
    asyncio.run(w.poll())
    assert len(pushed) == 2


def test_no_alerts_when_the_rule_is_off(tmp_path):
    w, pushed, _ = _watch(tmp_path)
    w.rt.alerts.cfg["rules"]["position"]["on"] = False
    w.add(ADDR)
    asyncio.run(w.poll())
    assert pushed == [] and w.public()["wallets"][0]["account"]["positions"]

from types import SimpleNamespace

from api.liqmap import LiqMapCollector, parse_state

STATE = {
    "marginSummary": {"accountValue": "125000.5"},
    "assetPositions": [
        {"position": {"coin": "ETH", "szi": "-40.5", "entryPx": "2500.1", "liquidationPx": "2711.0",
                      "positionValue": "101000", "leverage": {"type": "cross", "value": 10}}},
        {"position": {"coin": "DOGE", "szi": "1000", "entryPx": "0.1"}},            # not watched
        {"position": {"coin": "BTC", "szi": "0.0"}},                               # closed
        {"position": {"coin": "BTC", "szi": "2", "entryPx": "80000", "liquidationPx": None, "positionValue": "160000",
                      "leverage": {"type": "isolated", "value": 3}}},
    ],
}


def test_positions_on_watched_coins_are_kept_with_their_liquidation_price():
    rows = parse_state(STATE, "0xabc", 1000, {"BTC", "ETH"})
    assert rows == [(1000, "0xabc", "ETH", -40.5, 2500.1, 2711.0, 101000.0, 10.0, 125000.5),
                    (1000, "0xabc", "BTC", 2.0, 80000.0, None, 160000.0, 3.0, 125000.5)]


def test_the_most_active_wallets_are_checked_first():
    c = LiqMapCollector(SimpleNamespace(mode="replay", coins=["BTC"]), None, top_n=2)
    for w, usd in (("0xA", 900_000), ("0xB", 60_000), ("0xC", 400_000), ("0xD", 10_000)):
        c.on_message({"channel": "trades", "data": [{"px": "1", "sz": str(usd), "users": [w, "0xMM"]}]})
    # 0xMM made the market on every trade, so it's the most active; 0xD is under the minimum
    assert c.top() == ["0xmm", "0xa"]
    assert not c.enabled                                  # replay mode never calls the API

"""The HLP recorder (api/hlp.py): finds HLP and the Liquidator, sums their fills by minute, saves positions."""
import asyncio
import sqlite3
from types import SimpleNamespace

from api.hlp import HLP_ADDRESS, HLPRecorder, children_of, find_vaults

HLP = "0x" + "1" * 40
LIQ = "0x" + "2" * 40
KID = "0x" + "3" * 40
X = "0x" + "9" * 40


def rec(tmp_path=None):
    rt = SimpleNamespace(mode="live", coins=["BTC", "ETH", "SOL", "HYPE"])
    return HLPRecorder(rt, tmp_path / "positions.sqlite" if tmp_path else None)


def trade(buyer, seller, side, usd=1000.0, ms=1_790_000_030_000, coin="BTC"):
    return {"coin": coin, "side": side, "px": "100", "sz": str(usd / 100), "time": ms, "users": [buyer, seller]}


def test_finds_hlp_and_the_liquidator_in_the_vault_list():
    listed = [{"summary": {"name": "Hyperliquidity Provider (HLP)", "vaultAddress": HLP.upper()}},
              {"summary": {"name": "Liquidator", "vaultAddress": LIQ}},
              {"summary": {"name": "Growi HF", "vaultAddress": X}}]
    hlp, who = find_vaults(listed)
    assert hlp == HLP and who == {LIQ: "Liquidator"}
    assert find_vaults([]) == (None, {})
    assert children_of({"relationship": {"type": "parent", "data": {"childAddresses": [KID.upper()]}}}) == [KID]
    assert children_of({}) == [] and children_of({"relationship": None}) == []
    assert HLP_ADDRESS.startswith("0x") and len(HLP_ADDRESS) == 42


def test_fills_are_summed_by_minute_with_who_took():
    r = rec()
    r.who = {KID: "HLP Strategy A", LIQ: "Liquidator"}
    r.on_message({"channel": "trades", "data": [
        trade(KID, X, "B", 1000),            # HLP bought, the buyer took: HLP took
        trade(X, KID, "B", 500),             # HLP sold, the buyer (X) took: HLP's resting order was hit
        trade(X, X, "A", 999),               # not ours
        trade(LIQ, X, "A", 2000),            # the Liquidator bought, the seller took
    ]})
    m = 1_790_000_030_000 // 60_000 * 60_000
    assert r.flow[(m, KID, "BTC")] == [1000.0, 500.0, 1000.0, 0.0, 2]
    assert r.flow[(m, LIQ, "BTC")] == [2000.0, 0.0, 0.0, 0.0, 1]
    r._flush(force=True)
    assert not r.flow and r.last["liquidator_fills"] == 1


def test_nothing_is_watched_before_the_vaults_are_found():
    r = rec()
    r.on_message({"channel": "trades", "data": [trade(KID, X, "B")]})
    assert not r.flow


def test_saves_positions_and_fills(tmp_path):
    class Client:
        async def post(self, url, json):
            st = {"marginSummary": {"accountValue": "5000000"},
                  "assetPositions": [{"position": {"coin": "BTC", "szi": "-12.5", "entryPx": "100000", "positionValue": "1250000",
                                                   "liquidationPx": None, "leverage": {"value": 5}}},
                                     {"position": {"coin": "DOGE", "szi": "100", "entryPx": "0.1"}}]}
            empty = {"marginSummary": {"accountValue": "10"}, "assetPositions": []}
            return SimpleNamespace(json=lambda: st if json["user"] == KID else empty, raise_for_status=lambda: None)

    async def go():
        r = rec(tmp_path)
        await r.start()
        r._task.cancel()
        r.who = {KID: "HLP Strategy A", LIQ: "Liquidator"}
        await r.round(Client())
        r.on_message({"channel": "trades", "data": [trade(LIQ, X, "A", 2000)]})
        await r.stop()

    asyncio.run(go())
    db = sqlite3.connect(tmp_path / "positions.sqlite")
    pos = db.execute("SELECT wallet, coin, szi, value_usd, account_usd FROM hlp_positions ORDER BY wallet").fetchall()
    assert sorted(pos) == sorted([(LIQ, "", 0.0, 0.0, 10.0), (KID, "BTC", -12.5, 1250000.0, 5000000.0)])
    assert db.execute("SELECT wallet, bought_usd, fills FROM hlp_flow").fetchall() == [(LIQ, 2000.0, 1)]


def test_off_outside_live_mode():
    r = HLPRecorder(SimpleNamespace(mode="replay", coins=["BTC"]), None)
    asyncio.run(r.start())
    assert not r.enabled and r._task is None

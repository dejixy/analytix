"""The HYPE holder map (engine/hypemap.py, scripts/hypeMap.py): parsing, groups, unlocks, the unstaking test."""
import asyncio
import math

from engine import hypemap as H

D = H.DAY_MS
T0 = 1_790_000_000_000 // D * D


def test_parsing_reads_both_shapes_and_skips_the_unknown():
    assert H.spot_hype({"balances": [{"coin": "USDC", "total": "5"}, {"coin": "HYPE", "total": "12.5", "hold": "0"}]}) == 12.5
    assert H.spot_hype(None) == 0
    s = H.stake_of({"delegated": "100", "undelegated": "5", "totalPendingWithdrawal": "40", "nPendingWithdrawals": 2})
    assert s == {"delegated": 100, "undelegated": 5, "pending": 40, "n_pending": 2}
    hist = [{"time": T0 + 3, "delta": {"withdrawal": {"amount": "40", "phase": "initiated"}}},
            {"time": T0 + 1, "delta": {"cDeposit": {"amount": "100"}}},
            {"time": T0 + 2, "delta": {"delegate": {"validator": "0xv", "amount": "100", "isUndelegate": False}}},
            {"time": T0 + 9, "delta": {"withdrawal": {"amount": "40", "phase": "finalized"}}},
            {"time": T0 + 4, "delta": {"somethingNew": {}}}]
    ledger = [{"time": T0 + 9, "delta": {"type": "cStakingTransfer", "token": "HYPE", "amount": "40", "isDeposit": False}},
              {"time": T0 + 20, "delta": {"type": "cStakingTransfer", "token": "HYPE", "amount": "7", "isDeposit": True}}]
    assert H.events_of(hist, ledger) == [(T0 + 1, "stake", 100), (T0 + 2, "delegate", 100), (T0 + 3, "unstake_start", 40),
                                         (T0 + 9, "unstake_done", 40), (T0 + 20, "stake", 7)]   # no double count


def test_groups_concentration_and_unlocks():
    ws = [H.Wallet(H.ASSISTANCE_FUND, spot=30e6),
          H.Wallet("0xres", spot=200e6, not_circulating=True),
          H.Wallet("0xcex", spot=5e6, label="Binance"),
          H.Wallet("0xstk", spot=1e5, delegated=9e5, pending=2e5,
                   events=[(T0 - 2 * D, "unstake_start", 1.5e5), (T0 - 1 * D, "unstake_start", 0.5e5),
                           (T0 - 20 * D, "unstake_start", 9e5)]),
          H.Wallet("0xtrd", spot=4e4, traded_usd=5e5),
          H.Wallet("0xhld", spot=2e3),
          H.Wallet("0xnil")]
    names = [H.group_of(w) for w in ws]
    assert names == ["Assistance Fund", "Not circulating", "Labelled: Binance", "Stakers", "Traders", "Holders", "Holders"]
    g = {r["group"]: r for r in H.groups(ws, supply=400e6)}
    assert g["Stakers"]["hype"] == 1.2e6 and g["Stakers"]["pending"] == 2e5 and abs(g["Assistance Fund"]["share"] - 0.075) < 1e-9
    assert "Holders" in g and g["Holders"]["wallets"] == 1                  # the empty wallet isn't counted
    c = H.concentration(ws, supply=100e6, tops=(1, 2))
    assert c[1] == 0.05 and abs(c[2] - 0.062) < 1e-9                          # AF and reserves left out
    assert H.unlocks(ws, T0) == [((T0 - 2 * D + 7 * D) // D * D, 1.5e5), ((T0 - D + 7 * D) // D * D, 0.5e5)]
    ws[3].pending = 2.5e5                                                    # more pending than the history explains
    assert H.unlocks(ws, T0)[-1] == (None, 0.5e5)
    assert H.tier_of(1.2e6) == "1M+" and H.tier_of(500) == "under 1k"


def test_the_spike_test_walks_forward_and_finds_a_planted_effect():
    days = [T0 + i * D for i in range(120)]
    flows, closes, px = {}, {}, 30.0
    for i, d in enumerate(days):
        big = i % 15 == 4 and i > 20                                         # every 15th day: a wave of withdrawals ...
        flows[d] = {"unstake_start": 5e5 if big else 1e5 + 1e4 * math.sin(i)}
        closes[d] = px
        px *= 1 + (0.004 if i % 3 else -0.002)
    for i, d in enumerate(days):
        if flows[d]["unstake_start"] >= 5e5:                                 # ... then HYPE sinks for the next week
            for j in range(1, 8):
                if i + j < len(days):
                    closes[days[i + j]] *= 1 - 0.01 * j
    t = H.spike_test(flows, closes)
    assert t["n_spikes"] >= 5 and days[0] not in t["spike_days"]           # nothing before 14 days of history
    assert t["7d"]["spike"] < t["7d"]["other"] and t["passes"]
    flat = H.spike_test({d: {"unstake_start": 1e5} for d in days}, closes)   # no spikes at all
    assert flat["n_spikes"] == 0 and not flat["passes"]


def test_the_crawl_paces_caches_and_reads_history(tmp_path):
    from scripts import hypeMap as M
    calls = []

    async def post(body):
        calls.append(body["type"])
        a = body.get("user")
        if body["type"] == "spotClearinghouseState":
            return {"balances": [{"coin": "HYPE", "total": "50000" if a == "0xa" else "10"}]}
        if body["type"] == "delegatorSummary":
            return {"delegated": "20000" if a == "0xa" else "0", "undelegated": "0", "totalPendingWithdrawal": "0",
                    "nPendingWithdrawals": 0}
        if body["type"] == "delegatorHistory":
            return [{"time": T0, "delta": {"cDeposit": {"amount": "20000"}}}]
        return None

    info = M.Paced(budget=1200, post=post)
    cache = M.Cache(tmp_path / "c.jsonl")
    got = asyncio.run(M.crawl(info, ["0xa", "0xb"], cache, history_min=10_000, log=None))
    assert got["0xa"].total == 70_000 and got["0xa"].events == [(T0, "stake", 20_000)] and got["0xb"].events == []
    assert calls.count("delegatorHistory") == 1
    again = asyncio.run(M.crawl(info, ["0xa", "0xb"], M.Cache(tmp_path / "c.jsonl"), history_min=10_000, log=None))
    assert len(calls) == 5 and again["0xa"].spot == 50_000                  # all from the cache the second time
    res = M.report(list(got.values()), 1e6, {T0 + i * D: 30.0 for i in range(10)}, T0 + 5 * D, 30.0)
    assert res["staked"] == 20_000 and res["groups"][0]["group"] in ("Stakers", "Holders")


def test_hypurrscan_holders_and_queue_in_their_shapes():
    assert H.parse_holders({"holders": {"0xA": "5", "0xb": 9, "bad": 100}}) == [("0xb", 9.0), ("0xa", 5.0)]
    assert H.parse_holders([["0xa", "1"], {"user": "0xC", "balance": "3"}]) == [("0xc", 3.0), ("0xa", 1.0)]
    now = T0 + 3 * D
    rows = [{"time": now - 2 * D, "user": "0xA", "wei": 5_000 * 10 ** 8}, {"time": now - D, "user": "0xb", "wei": 10 ** 8}]
    q = H.parse_queue(rows, now)
    assert q["n"] == 2 and q["hype"] == 5_001 and q["times_are"] == "asked" and q["users"] == ["0xa", "0xb"]
    assert q["by_day"] == [((now - 2 * D + 7 * D) // D * D, 5_000.0), ((now - D + 7 * D) // D * D, 1.0)]
    later = H.parse_queue([{"time": now + D, "user": "0xa", "wei": 10 ** 8}], now)
    assert later["times_are"] == "unlock" and later["by_day"] == [((now + D) // D * D, 1.0)]
    assert H.parse_queue([], now)["n"] == 0


def test_the_hyperevm_bridge_is_its_own_group_and_top_lists_the_biggest():
    from scripts import hypeTop
    bridge = H.Wallet(H.HYPER_EVM, spot=30e6)
    big = H.Wallet("0xaa", delegated=20e6)
    small = H.Wallet("0xbb", spot=1e6)
    assert H.group_of(bridge) == "On HyperEVM"
    c = H.concentration([bridge, big, small], supply=100e6, tops=(1, 10))
    assert c == {1: 0.2, 10: 0.21}                                       # the bridge isn't one holder
    snap = {"supply": 100e6, "wallets": {w.address: {"spot": w.spot, "staked": w.staked, "pending": w.pending,
                                                     "group": H.group_of(w)} for w in (bridge, big, small)}}
    lines = hypeTop.top(snap, 2, {"0xaa": "Some LST"})
    assert len(lines) == 3 and H.HYPER_EVM in lines[1] and "HyperEVM bridge" in lines[1]
    assert "0xaa" in lines[2] and "20.0%" in lines[2] and "Some LST" in lines[2]

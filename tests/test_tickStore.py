import gzip
import json

import numpy as np

from engine.ticks import hl_tick
from storage import tickStore


def _hour(folder, stamp, lines):
    with gzip.open(folder / f"eth_{stamp}.jsonl.gz", "wt", encoding="utf-8") as fh:
        for recv, msg in lines:
            fh.write(json.dumps({"recv_ms": recv, "msg": msg}) + "\n")


def _book(t, bid, ask):
    lv = lambda px: [{"px": str(px), "sz": "2.5", "n": 3}]
    return {"channel": "l2Book", "data": {"coin": "ETH", "time": t, "levels": [lv(bid), lv(ask)]}}


def _trade(t, tid, side, buyer, seller, engine=False):
    h = "0x" + ("0" * 64 if engine else "ab" * 32)
    return {"channel": "trades", "data": [{"coin": "ETH", "side": side, "px": "2650.1", "sz": "1.5", "time": t,
                                           "hash": h, "tid": tid, "users": [buyer, seller]}]}


def test_store_round_trip(tmp_path):
    rec, store = tmp_path / "rec", tmp_path / "store"
    rec.mkdir()
    day1, day2 = 1_790_035_200_000 - 3_600_000, 1_790_035_200_000          # 23:00 on one day, midnight the next
    _hour(rec, "20260921_2300", [
        (day1 + 1000, _book(day1 + 900, 2650.0, 2650.1)),
        (day1 + 2000, _trade(day1 + 1900, 1, "B", "0xAA", "0xbb")),
        (day1 + 2500, {"channel": "ext", "data": {"venue": "bybit", "coin": "ETH", "ts": day1 + 2400,
                                                  "bid": 2650.0, "ask": 2650.2}}),
        (day1 + 3000, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"funding": "0.00001",
                                                             "openInterest": "5", "markPx": "2650", "midPx": None}}}),
    ])
    _hour(rec, "20260922_0000", [
        (day2 + 1000, _trade(day2 + 900, 2, "A", "0xcc", "0xaa", engine=True)),
        (day2 + 2000, _book(day2 + 1900, 2651.0, 2651.1)),
    ])
    days = tickStore.hours_by_day([rec])
    assert sorted(days) == ["20260921", "20260922"]
    for day, paths in days.items():
        tickStore.write_day(store, day, paths)
        assert tickStore.built_sources(store, day) == tickStore.sources_of(paths)

    z = tickStore.load("ETH", ["book", "trades", "ext", "ctx"], store=store)
    assert z["book_t"].tolist() == [day1 + 900, day2 + 1900]
    assert z["bid_px"].shape == (2, tickStore.BOOK_LEVELS) and z["bid_px"][1, 0] == 2651.0
    assert np.isnan(z["bid_px"][0, 1]) and z["bid_sz"][0, 1] == 0
    assert z["trade_side"].tolist() == [1, -1] and z["trade_engine"].tolist() == [False, True]
    w = z["wallets"]
    assert w[z["trade_buyer"][0]] == "0xaa" and w[z["trade_seller"][1]] == "0xaa"   # one wallet across days
    assert z["ext_venue"].tolist() == [tickStore.VENUES.index("bybit")]
    assert z["ctx_oi"].tolist() == [5.0] and np.isnan(z["ctx_mid"][0])

    part = tickStore.load("ETH", ["book"], start_ms=day2, store=store, only=["bid_px"])
    assert part["book_t"].tolist() == [day2 + 1900] and "ask_px" not in part


def test_hl_tick():
    assert hl_tick(110_000.0) == 1.0          # whole numbers are always allowed
    assert hl_tick(2650.1) == 0.1
    assert hl_tick(104.37) == 0.01
    assert hl_tick(99.87) == 0.001            # SOL just under 100 has a 10x smaller tick
    assert hl_tick(np.array([45_000.0, 40.1])).tolist() == [1.0, 0.001]

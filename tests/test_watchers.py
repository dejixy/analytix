"""Watchers (engine/watchers.py, api/watchers.py): the roll and carry rules, the paper book, and a live pass."""
from datetime import datetime
from types import SimpleNamespace

from api.watchers import WatcherService
from engine import edges as E
from engine import watchers as W

H = W.HOUR


def ny(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=E.NY).timestamp() * 1000)


def _roll(now, front=60.0, nxt=59.4, basis_bps=0.0, funding=1e-5):
    st = E.roll_state("xyz:CL", now)
    oracle = st["front_weight"] * front + (1 - st["front_weight"]) * nxt
    ctx = {"mark": oracle * (1 + basis_bps / 1e4), "oracle": oracle, "funding": funding}
    return W.roll_read("xyz:CL", st, front, nxt, now - 15 * 60_000, ctx, now)


def test_roll_ticket_takes_the_part_of_the_roll_not_priced_in():
    mon = ny(2026, 11, 9, 12)                                   # 80% on Z26 (Friday's step is in since Sunday)
    r = _roll(mon)
    assert r["front_weight"] == 0.8 and r["side"] == -1
    left = 0.8 * (59.4 - 60.0) / (0.8 * 60 + 0.2 * 59.4) * 1e4
    hours = (ny(2026, 11, 12, 18) - mon) / H
    assert abs(r["left_bps"] - left) < 0.1 and abs(r["funding_bps"] - 1e-5 * hours * 1e4) < 0.1   # a short is paid
    assert abs(r["expect_bps"] - (-left + 1e-5 * hours * 1e4 - W.ROLL_COST_BPS)) < 0.2
    t = W.roll_ticket(r, open_now=True, now_ms=mon, day_pct=2.1)
    assert t["side"] == "short" and t["legs"][0]["dir"] == -1 and t["key"] == "roll:xyz:CL:2026-11-06"
    assert t["expires_ms"] == ny(2026, 11, 12, 18, 30) and "±2.1%" in t["why"][-1]
    assert W.roll_ticket(_roll(mon), open_now=False, now_ms=mon) is None                  # the real market is shut
    priced = _roll(mon, basis_bps=-75)                                                     # traders already see it
    assert W.roll_ticket(priced, True, mon) is None and "needs" in priced["why_not"]
    late = ny(2026, 11, 12, 17)
    assert W.roll_ticket(_roll(late), True, late) is None                                  # under 2 h to the last step
    early = ny(2026, 11, 2, 12)
    assert W.roll_ticket(_roll(early), True, early) is None                               # days before the window
    no_quotes = W.roll_read("xyz:CL", E.roll_state("xyz:CL", mon), None, None, None, {"mark": 60, "oracle": 60}, mon)
    assert W.roll_ticket(no_quotes, True, mon) is None and no_quotes["why_not"] == "no futures prices"


def _hist(now, apr_pct, hours=96):
    return [(now - k * H, apr_pct / 100 / 8760) for k in range(hours, 0, -1)]


def test_carry_opens_on_lasting_high_funding_and_closes_when_it_fades():
    now = ny(2026, 11, 9, 12)
    ctx = {"mark": 30.0, "funding": 0.30 / 8760}
    spot = {"px": 29.98, "day_volume": 5e6}
    r = W.carry_read("HYPE", _hist(now, 30), ctx, spot, now)
    assert abs(r["three_day_apr"] - 30) < 0.01 and abs(r["now_apr"] - 30) < 0.01
    assert abs(r["expect_bps"] - (0.30 / 8760 * W.CARRY_HOLD_H * 1e4 * W.CARRY_KEEP - W.CARRY_COST_BPS)) < 0.1
    t = W.carry_ticket(r, now)
    assert t and [g["kind"] for g in t["legs"]] == ["spot", "perp"] and [g["dir"] for g in t["legs"]] == [1, -1]
    low = W.carry_read("HYPE", _hist(now, 12), {"mark": 30.0, "funding": 0.12 / 8760}, spot, now)
    assert W.carry_ticket(low, now) is None and "needs" in low["why_not"]
    thin = W.carry_read("HYPE", _hist(now, 30), ctx, {"px": 30.0, "day_volume": 1e5}, now)
    assert W.carry_ticket(thin, now) is None and thin["why_not"] == "spot market too thin"
    assert W.carry_ticket(W.carry_read("HYPE", _hist(now, 30, hours=10), ctx, spot, now), now) is None   # short history
    assert W.carry_done(W.carry_read("HYPE", _hist(now, 5), ctx, spot, now))
    assert not W.carry_done(r)


def test_the_book_follows_prices_funding_and_fees_and_keeps_a_record(tmp_path):
    b = W.Book(tmp_path / "w.json")
    now = ny(2026, 11, 9, 12)
    t = b.add({"key": "roll:x:1", "watcher": "roll", "market": "xyz:CL", "title": "Short WTI into the roll: +40 bps",
               "side": "short", "opened_ms": now, "expires_ms": now + 10 * H, "expect_bps": 40.0, "cost_bps": 18.0,
               "why": [], "exit": "", "legs": [{"kind": "perp", "coin": "xyz:CL", "dir": -1, "entry": 100.0, "last": 100.0}]})
    b.mark({("perp", "xyz:CL"): 99.0}, {"xyz:CL": 1e-5}, now + 2 * H)          # price -1%, funding paid to shorts
    p = W.pnl(t)
    assert p["price_bps"] == 100.0 and abs(p["funding_bps"] - 0.2) < 1e-9 and p["net_bps"] == round(100.2 - 18, 1)
    b.mark({("perp", "xyz:CL"): 99.0}, {"xyz:CL": 1e-5}, now + 6 * H)          # a 4-hour outage: funding not guessed
    assert t["gaps"] == 1 and abs(W.pnl(t)["funding_bps"] - 0.2) < 1e-9
    assert b.has("roll:x:1") and b.resting("roll", "xyz:CL", now, 1)
    assert b.expire(now + 5 * H) == [] and len(b.expire(now + 10 * H)) == 1
    rec = b.record("roll")
    assert rec["n"] == 1 and rec["won"] == 1 and rec["avg_bps"] == t["result"]["net_bps"] and rec["expected_avg_bps"] == 40.0
    b.save()
    again = W.Book(tmp_path / "w.json")
    assert again.record("roll") == rec                                            # a restart keeps the record
    assert W.Book(tmp_path / "missing.json").tickets == []


def _runtime(now, front=60.0, nxt=59.4, hype_apr=30.0):
    st = E.roll_state("xyz:CL", now)
    oracle = st["front_weight"] * front + (1 - st["front_weight"]) * nxt
    pushed, logged = [], []
    edges = SimpleNamespace(
        coins=["xyz:CL", "xyz:GOLD"], markets={"xyz:CL": {"mark": oracle, "oracle": oracle, "funding": 1e-5},
                                             "xyz:GOLD": {"mark": 4000.0, "oracle": 4000.0, "funding": 0.0}},
        futures={st["symbols"][0]: {"px": front, "t_ms": now}, st["symbols"][1]: {"px": nxt, "t_ms": now}},
        candles={"xyz:CL": [(now - k * H, 60 * (1 + 0.01 * ((k // 24) % 3 - 1))) for k in range(24 * 40)]},
        changes=SimpleNamespace(add=lambda kind, text, t: logged.append((kind, text))))
    scanner = SimpleNamespace(contexts={"HYPE": {"mark": 30.0, "funding": hype_apr / 100 / 8760},
                                        "BTC": {"mark": 100_000.0, "funding": 0.11 / 8760}})
    alerts = SimpleNamespace(cfg={"rules": {"watch": {"on": True}}}, push=pushed.append)
    rt = SimpleNamespace(mode="live", edges=edges, scanner=scanner, alerts=alerts)
    return rt, pushed, logged


def test_a_live_pass_opens_once_alerts_follows_and_closes(tmp_path):
    now = ny(2026, 11, 9, 12)
    rt, pushed, logged = _runtime(now)
    svc = WatcherService(rt, tmp_path / "watchers.json")
    svc.spot = {"HYPE": {"px": 29.99, "day_volume": 9e6}, "UBTC": {"px": 99_990.0, "day_volume": 2e7}}
    svc.hist = {"HYPE": _hist(now, 30), "BTC": _hist(now, 11)}
    got = svc.step(now)
    kinds = sorted(t["watcher"] + ":" + t["market"] for t in got["opened"])
    assert kinds == ["carry:HYPE", "roll:xyz:CL"]                    # BTC's 11% a year doesn't fire
    assert len(pushed) == 2 and all(a.kind == "watch" and a.title.startswith("WATCH · ") for a in pushed)
    assert {k for k, _ in logged} == {"watch"}
    assert svc.step(now + 60_000)["opened"] == []                       # once per window / while open
    btc = next(r for r in svc.reads["carry"] if r["market"] == "BTC")
    assert "needs" in btc["why_not"] and not btc["ticket_open"]
    assert next(r for r in svc.reads["carry"] if r["market"] == "HYPE")["ticket_open"]
    pub = svc.public(now + 60_000)
    assert len(pub["open"]) == 2 and all("pnl" in t for t in pub["open"])
    assert [w["key"] for w in pub["watchers"]] == ["roll", "carry"] and pub["watchers"][0]["reads"][0]["market"] == "xyz:CL"
    svc.hist["HYPE"] = _hist(now + 2 * H, 3)                            # HYPE's funding fades
    got = svc.step(now + 2 * H)
    assert [t["market"] for t in got["closed"]] == ["HYPE"] and got["closed"][0]["closed_why"] == "funding faded"
    assert pushed[-1].title.startswith("WATCH · Close: Carry HYPE")
    assert svc.step(now + 3 * H)["opened"] == []                        # resting a day after a close
    later = ny(2026, 11, 12, 18, 31)
    got = svc.step(later)
    assert [t["market"] for t in got["closed"]] == ["xyz:CL"] and got["closed"][0]["closed_why"] == "time"
    assert WatcherService(rt, tmp_path / "watchers.json").book.record("roll")["n"] == 1    # saved
    rt.alerts.cfg["rules"]["watch"]["on"] = False
    n = len(pushed)
    rt2, _, _ = _runtime(ny(2026, 12, 8, 12))
    rt2.alerts = rt.alerts
    svc2 = WatcherService(rt2, None)
    svc2.step(ny(2026, 12, 8, 12))
    assert len(pushed) == n                                             # the rule switched off: no alert


def test_the_carry_rule_on_history_matches_the_live_rule():
    t0 = ny(2025, 3, 3, 0)
    a = lambda pct: pct / 100 / 8760
    rates = [a(11)] * (5 * 24) + [a(40)] * (6 * 24) + [a(2)] * (3 * 24) + [a(11)] * (10 * 24)   # one hot spell
    hist = [(t0 + k * H, r) for k, r in enumerate(rates)]
    flat = [(t0 + k * 4 * H, 30.0) for k in range(len(rates) // 4 + 2)]
    r = W.carry_history("HYPE", hist, flat, flat)
    assert len(r["trades"]) == 1 and not r["open"]
    tr = r["trades"][0]
    assert t0 + 5 * W.DAY < tr["t"] < t0 + 11 * W.DAY and tr["end"] < t0 + 14 * W.DAY     # opens in the spell, leaves after
    assert tr["basis_bps"] == 0 and abs(tr["net_bps"] - (tr["funding_bps"] - W.CARRY_COST_BPS)) < 1e-9
    assert 1 < tr["kept"] < 2.5 and tr["funding_bps"] > 0     # it enters as the 3-day average first crosses 20%
    s = W.summarise(r["trades"])
    assert s["n"] == 1 and s["avg_bps"] == tr["net_bps"]
    assert abs(r["always_apr"] - 100 * sum(rates) / len(rates) * 8760) < 1e-6 and r["negative_share"] == 0
    assert W.summarise([]) == {"n": 0}

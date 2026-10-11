from engine.origin import find_origin, origin_sentence, segment
from ingestion.external import Quoter, parse_binance, parse_bybit, symbol
from ingestion.replay import read_session
from ingestion.synthetic import generate_session
from pipeline import Pipeline
from tests.helpers import T0

S = 1000


def _step(at_s: float, before=100.0, after=101.0, every_ms=500):
    """A price that climbs from `before` to `after` over 4s from `at_s` seconds, sampled from 20s before T0."""
    out = []
    for i in range(-40, 80 * 1000 // every_ms):
        t = i * every_ms / 1000
        f = min(1.0, max(0.0, (t - at_s) / 4))
        out.append((T0 + i * every_ms, before + (after - before) * f))
    return tuple(out)


def test_the_exchange_that_got_halfway_first_is_where_it_started():
    here = _step(30)
    o = find_origin(here, {"binance": _step(27)}, T0, T0 + 60 * S, 1)
    assert o.verdict == "elsewhere" and o.venue == "Binance" and o.lead_s == 3.0 and o.label == "started on Binance"
    assert "Binance moved first, about 3.0s before Hyperliquid" in origin_sentence(o)
    assert find_origin(here, {"binance": _step(33)}, T0, T0 + 60 * S, 1).verdict == "here"
    assert find_origin(here, {"binance": _step(30.5)}, T0, T0 + 60 * S, 1).verdict == "together"
    flat = find_origin(here, {"binance": _step(30, 100.0, 100.05)}, T0, T0 + 60 * S, 1)
    assert flat.verdict == "here_only" and "only on Hyperliquid" in origin_sentence(flat)


def test_bybit_is_used_when_binance_has_nothing_and_no_data_means_no_claim():
    here = _step(30)
    assert find_origin(here, {"bybit": _step(26)}, T0, T0 + 60 * S, 1).venue == "Bybit"
    assert find_origin(here, {}, T0, T0 + 60 * S, 1) is None
    late = tuple(p for p in _step(27) if p[0] >= T0 + 20 * S)          # quotes only start mid-window
    assert find_origin(here, {"binance": late}, T0, T0 + 60 * S, 1) is None
    assert find_origin(here, {"binance": _step(27)}, T0, T0 + 60 * S, -1) is None   # it went up, not down


def test_longer_windows_are_timed_on_their_sharpest_minute():
    assert segment(T0, T0 + 60 * S, 60, T0 + 50 * S, 60) == (T0, T0 + 60 * S)
    assert segment(T0, T0 + 600 * S, 600, T0 + 400 * S, 60) == (T0 + 310 * S, T0 + 405 * S)


def test_exchange_messages_become_quotes_at_most_four_a_second():
    out = []
    q = Quoter("binance", out.append, {"ETHUSDT": "ETH"})
    for i, t in enumerate((0, 100, 300, 400)):
        parse_binance({"stream": "ethusdt@bookTicker", "data": {"e": "bookTicker", "s": "ETHUSDT", "b": "2650.1",
                       "a": "2650.2", "T": T0 + t, "E": T0 + t + 1}}, q)
    assert [m["data"]["ts"] for m in out] == [T0, T0 + 300]
    assert out[0] == {"channel": "ext", "data": {"venue": "binance", "coin": "ETH", "ts": T0, "bid": 2650.1, "ask": 2650.2}}
    out.clear()
    qb, book = Quoter("bybit", out.append, {"HYPEUSDT": "HYPE"}), {}
    parse_bybit({"topic": "orderbook.1.HYPEUSDT", "type": "snapshot", "ts": T0, "cts": T0 - 2,
                 "data": {"s": "HYPEUSDT", "b": [["38.10", "5"]], "a": [["38.12", "7"]]}}, qb, book)
    parse_bybit({"topic": "orderbook.1.HYPEUSDT", "type": "delta", "ts": T0 + 400, "cts": T0 + 399,
                 "data": {"s": "HYPEUSDT", "b": [["38.11", "2"]], "a": []}}, qb, book)
    assert [(m["data"]["ts"], m["data"]["bid"], m["data"]["ask"]) for m in out] == [(T0 - 2, 38.10, 38.12),
                                                                                 (T0 + 399, 38.11, 38.12)]
    assert symbol("kPEPE") == "1000PEPEUSDT" and symbol("ETH") == "ETHUSDT"


def _replay_with_binance(tmp_path, shift_ms):
    """The synthetic session, with Binance showing Hyperliquid's own price `shift_ms` earlier (or later)."""
    msgs = [m for _, m in read_session(generate_session(tmp_path / "s.jsonl"))]
    books = [(m["data"]["time"], (float(m["data"]["levels"][0][0]["px"]) + float(m["data"]["levels"][1][0]["px"])) / 2)
             for m in msgs if m.get("channel") == "l2Book"]
    pipe, i, seen = Pipeline("ETH"), 0, []
    for m in msgs:
        t = m["data"]["time"] if m.get("channel") == "l2Book" else pipe.state.now_ms
        while i < len(books) and books[i][0] - shift_ms <= t:
            ts, mid = books[i]
            pipe.on_message({"channel": "ext", "data": {"venue": "binance", "coin": "ETH", "ts": ts - shift_ms,
                                                        "bid": mid - 0.05, "ask": mid + 0.05}})
            i += 1
        pipe.on_message(m)
        ex = pipe.analyzer.latest.get("1m")
        if ex is not None and ex.origin is not None and ex.move.significance.value == "significant":
            seen.append(ex)
    return seen


def test_a_replay_where_binance_leads_says_so(tmp_path):
    seen = _replay_with_binance(tmp_path, 3_000)
    assert seen and sum(e.origin.verdict == "elsewhere" for e in seen) > 0.6 * len(seen)
    ex = next(e for e in seen if e.origin.verdict == "elsewhere")
    assert 2.0 <= ex.origin.lead_s <= 4.0 and any(line.startswith("Where it started: Binance moved first")
                                                  for line in ex.narrative)


def test_a_replay_where_binance_lags_says_hyperliquid_moved_first(tmp_path):
    seen = _replay_with_binance(tmp_path, -3_000)
    assert seen and sum(e.origin.verdict == "here" for e in seen) > 0.6 * len(seen)


def test_the_speed_meter_reports_and_resets():
    import asyncio
    from api.speed import Speed
    sp = Speed()
    for ms in (2.0, 4.0, 30.0):
        sp.engine.add(ms)

    async def tick():
        task = asyncio.create_task(sp.watch())
        await asyncio.sleep(0.6)
        task.cancel()
    asyncio.run(tick())
    r = sp.report(reset=False)
    assert r["engine_run"] == {"avg_ms": 12.0, "worst_ms": 30.0, "count": 3} and r["screen_delay"]["count"] >= 1
    assert sp.line().startswith("screen delay avg") and sp.report()["engine_run"]["count"] == 0

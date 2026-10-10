import asyncio
import json
from types import SimpleNamespace

import pytest

from api.alerts import AlertError, AlertService
from engine.alerts import WARMUP_MS, Alert, AlertEngine, PriceLevel
from ingestion.replay import read_session
from ingestion.synthetic import generate_session
from pipeline import Pipeline


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    return generate_session(tmp_path_factory.mktemp("al") / "s.jsonl")


def _run(path, engine, coin="ETH"):
    pipe, fired, last = Pipeline(coin), [], 0
    for _, msg in read_session(path):
        pipe.on_message(msg)
        if pipe.state.now_ms // 1000 != last:
            last = pipe.state.now_ms // 1000
            fired += [(pipe.state.now_ms, a) for a in engine.check(coin, pipe)]
    return pipe, fired


def test_the_scripted_market_raises_the_right_alerts(session):
    pipe, fired = _run(session, AlertEngine())
    kinds = {a.kind for _, a in fired}
    assert {"twap", "level", "cascade", "volatility"} <= kinds
    assert "liquidation" not in kinds                                    # the scripted liquidations are all small
    twap = next(a for _, a in fired if a.kind == "twap")
    assert "selling" in twap.title and "an hour" in twap.title and twap.direction == "down"
    cascade = next(a for _, a in fired if a.kind == "cascade")
    assert "of longs liquidated in" in cascade.title
    assert any("real liquidations" in line for line in cascade.lines)
    assert len({a.id for _, a in fired}) == len(fired)                   # nothing fires twice


def test_nothing_fires_during_warm_up_and_cooldowns_hold(session):
    eng = AlertEngine(cooldown_min=60)
    _, fired = _run(session, eng)
    first_seen = eng._mem["ETH"].first_ms
    assert fired and all(t - first_seen >= WARMUP_MS for t, _ in fired)
    vol = [t for t, a in fired if a.kind == "volatility"]
    assert len(vol) <= 1                                                 # a one-hour cooldown: one spike alert


def test_switching_a_rule_on_later_does_not_replay_the_past(session):
    eng = AlertEngine(rules={"twap": {"on": False}, "level": {"on": False}})
    _, fired = _run(session, eng)
    assert not any(a.kind in ("twap", "level") for _, a in fired)
    eng.rules["twap"]["on"] = eng.rules["level"]["on"] = True
    pipe = Pipeline("ETH")
    assert not [a for a in eng.check("ETH", SimpleNamespace(state=pipe.state, analyzer=pipe.analyzer))
                if a.kind in ("twap", "level")]


def test_price_alerts_fire_once_on_a_cross_either_way():
    eng = AlertEngine()
    eng.levels = [PriceLevel("a", "ETH", 100.0, 0), PriceLevel("b", "ETH", 90.0, 0)]
    mem_prev = None
    out = []
    for px in (95.0, 99.0, 101.0, 99.5, 102.0, 89.0):
        out += eng._price("ETH", mem_prev, px, 1)
        mem_prev = px
    assert [a.id for a in out] == ["px-a", "px-b"]                        # each level once, even after recrossing
    assert out[0].direction == "up" and out[1].direction == "down"
    assert all(lv.triggered_ms for lv in eng.levels)


def test_telegram_text_is_escaped():
    a = Alert("x", "ETH", "level", 0, "down", "ETH <b>broke</b> & ran", ("a < b",))
    html = a.telegram_html()
    assert "&lt;b&gt;broke&lt;/b&gt; &amp; ran" in html and "a &lt; b" in html and html.startswith("🔻 <b>")


# ── the service ─────────────────────────────────────────────────────────────
def _service(tmp_path, mode="live"):
    rt = SimpleNamespace(mode=mode, coins=["ETH", "BTC"], pipelines={})
    return AlertService(rt, tmp_path / "alerts.json")


def test_settings_validate_persist_and_never_leak_the_token(tmp_path):
    svc = _service(tmp_path)
    svc.update({"rules": {"twap": {"min_usd_per_hour": 2e6}, "level": {"on": False}}, "cooldown_min": 30,
                "coins": ["ETH"]})
    for bad in ({"rules": {"nope": {"on": True}}}, {"rules": {"volatility": {"min_ratio": 50}}},
                {"cooldown_min": 0}, {"coins": ["DOGE"]}, {"rules": {"twap": {"color": 1}}}):
        with pytest.raises(AlertError):
            svc.update(bad)
    svc.add_level("ETH", 2700)
    svc.cfg["telegram"].update(token="123:SECRET", bot="@a", chats=[{"id": 1, "title": "me", "on": True}])
    svc._save()
    again = _service(tmp_path)
    assert again.engine.rules["twap"]["min_usd_per_hour"] == 2e6 and not again.engine.rules["level"]["on"]
    assert again.engine.cooldown_ms == 30 * 60_000 and again.engine.coins == ["ETH"]
    assert len(again.engine.levels) == 1 and again.engine.levels[0].price == 2700
    pub = json.dumps(again.public())
    assert "SECRET" not in pub and again.public()["telegram"]["connected"]
    assert oct((tmp_path / "alerts.json").stat().st_mode)[-3:] == "600"
    with pytest.raises(AlertError):
        again.add_level("DOGE", 1)


def test_only_approved_chats_get_messages_and_replays_never_send(tmp_path):
    svc = _service(tmp_path)
    svc.cfg["telegram"].update(token="1:x", bot="@b")
    calls = []

    async def fake_api(method, token=None, **params):
        calls.append((method, params))
        if method == "getUpdates":
            return [{"message": {"chat": {"id": 7, "type": "private", "first_name": "Deji"}}},
                    {"my_chat_member": {"chat": {"id": -100, "type": "channel", "title": "Analytix"}}},
                    {"message": {"chat": {"id": 9, "type": "private", "first_name": "Stranger"}}}]
        return {}
    svc._api = fake_api
    pub = asyncio.run(svc.discover())
    assert {c["id"]: c["on"] for c in pub["telegram"]["chats"]} == {7: False, -100: False, 9: False}
    assert not svc.telegram_ready                                     # found, but nobody approved yet
    svc.set_chat(7, True)
    svc.set_chat(-100, True)
    assert svc.telegram_ready

    async def send_one():
        task = asyncio.create_task(svc._sender())
        await svc._queue.put(Alert("a", "ETH", "level", 0, "up", "t"))
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(send_one())
    sent_to = [p["chat_id"] for m, p in calls if m == "sendMessage"]
    assert sorted(sent_to) == [-100, 7]                               # never the stranger
    assert not _service(tmp_path, mode="replay").telegram_ready       # a replay can't ping your phone

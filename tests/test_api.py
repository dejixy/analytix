import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from ingestion.synthetic import generate_session


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = generate_session(tmp_path_factory.mktemp("api") / "session.jsonl")
    app = create_app(mode="replay", replay_file=path, speed=float("inf"), loop=False, serve_frontend=False,
                     alerts_file=tmp_path_factory.mktemp("alerts") / "alerts.json")
    with TestClient(app) as c:
        deadline = time.time() + 60
        while time.time() < deadline and not c.get("/api/health").json()["feed"]["finished"]:
            time.sleep(0.2)
        yield c


def test_health_reports_finished_replay(client):
    h = client.get("/api/health").json()
    assert h["feed"]["mode"] == "replay" and h["feed"]["finished"]
    assert h["coins"]["ETH"]["engine_runs"] > 1000


def test_snapshot_shape(client):
    s = client.get("/api/snapshot").json()
    assert set(s["explanations"]) == {"1m", "10m", "60m", "6h", "12h", "24h", "1w"}
    ex = s["explanations"]["1m"]
    assert ex["headline"] and ex["drivers"] and "move_pct" in ex["move"]
    assert len(s["book"]["bids"]) > 0 and len(s["trades"]) == 40
    assert 300 <= len(s["series"]) <= 901      # 25-minute sample, one point per 4s
    assert s["events"], "expected the scripted moves in the event log"
    # the absorption phase leaves bid levels that the long liquidation later breaks
    breaks = [e for e in s["market_events"] if e["kind"] == "level_break"]
    assert any(e["direction"] == "down" and e["title"].startswith("Price fell through") for e in breaks)
    # every card carries its timeframe's summary rows
    assert [m["key"] for m in s["explanations"]["1m"]["summary"]] == ["flow", "who", "forced", "book", "liquidity"]
    assert [m["key"] for m in s["explanations"]["24h"]["summary"]] == ["trend", "vwap", "positioning", "funding", "flow"]
    assert isinstance(s["levels"], list)


def test_explain_endpoint_and_unknown_window(client):
    assert client.get("/api/explain/10m").json()["window"] == "10m"
    assert client.get("/api/explain/3h").status_code == 404


def test_events_filter(client):
    evs = client.get("/api/events", params={"window": "1m", "limit": 5}).json()
    assert 0 < len(evs) <= 5 and all(e["window"] == "1m" for e in evs)


def test_websocket_sends_snapshot_on_connect(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["coin"] == "ETH" and "explanations" in msg


def test_explain_any_moment_still_in_memory(client):
    evs = client.get("/api/events", params={"window": "1m", "limit": 500}).json()
    crash = min(evs, key=lambda e: e["explanation"]["move"]["move_pct"])          # the scripted long liquidation
    r = client.get("/api/explain_at", params={"t": crash["peak_ms"], "window": "1m"}).json()
    assert r["window"] == "1m" and r["note"] == "" and r["at_ms"] == crash["peak_ms"]
    assert abs(r["explanation"]["move"]["move_pct"] - crash["explanation"]["move"]["move_pct"]) < 0.05
    assert "cascade" in r["explanation"]["headline"]
    # 60m ending mid-session isn't in memory (the session is 25 minutes): it falls back to a shorter timeframe
    r60 = client.get("/api/explain_at", params={"t": crash["peak_ms"], "window": "60m"}).json()
    assert r60["window"] == "10m" and r60["note"].startswith("60m isn't in memory that far back")
    # before the data starts
    assert client.get("/api/explain_at", params={"t": crash["peak_ms"] - 86_400_000}).status_code == 404


def test_plan_endpoint_answers_with_the_rough_model_in_a_short_replay(client):
    r = client.get("/api/plan", params={"side": "long", "margin": 1000, "leverage": 5, "hours": 24})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["notional"] == 5000 and p["side"] == 1 and p["model"]["kind"] == "rough"   # 25 minutes isn't history
    assert p["liq_price"] < p["entry"] and 0 <= p["liq_prob"] <= 1
    assert p["costs"]["fees"] == 4.5 and set(p["outcome"]) == {"p5", "p25", "p50", "p75", "p95"}
    assert any(n.startswith("Rough estimate") for n in p["notes"])
    assert p["calibration_pending"] is False and p["calibration"] is None      # no history to check in a replay
    assert len(p["fan"]) >= 2 and set(p["tail"]) == {"p1", "es5", "p_lose_half"} and p["margin_mode"] == "isolated"
    cross = client.get("/api/plan", params={"margin": 1000, "leverage": 5, "account": 3000}).json()
    assert cross["margin_mode"] == "cross" and cross["liq_price"] < p["liq_price"]
    assert client.get("/api/plan", params={"margin": 1000, "account": 500}).status_code == 400
    short = client.get("/api/plan", params={"side": "short", "hours": 1}).json()
    assert short["liq_price"] > short["entry"] and short["hours"] == 1
    assert client.get("/api/plan", params={"side": "sideways"}).status_code == 400
    assert client.get("/api/plan", params={"hours": 500}).status_code == 422


def test_alert_settings_round_trip_and_telegram_needs_a_bot(client):
    a = client.get("/api/alerts").json()
    assert a["rules"]["cascade"]["on"] and a["telegram"]["connected"] is False and a["telegram"]["enabled"] is False
    r = client.put("/api/alerts", json={"rules": {"volatility": {"min_ratio": 3}}, "cooldown_min": 20})
    assert r.status_code == 200 and r.json()["rules"]["volatility"]["min_ratio"] == 3
    assert client.put("/api/alerts", json={"cooldown_min": 0}).status_code == 400
    lv = client.post("/api/alerts/levels", json={"coin": "ETH", "price": 2700}).json()["price_levels"]
    assert len(lv) == 1 and lv[0]["price"] == 2700
    assert client.delete(f"/api/alerts/levels/{lv[0]['id']}").json()["price_levels"] == []
    assert client.post("/api/alerts/telegram/discover").status_code == 400      # no bot connected yet
    assert client.post("/api/alerts/telegram/token", json={"token": "nonsense"}).status_code == 400
    assert "alerts" in client.get("/api/snapshot").json()


def test_edges_shows_the_schedule_even_in_replay(client):
    d = client.get("/api/edges").json()
    assert d["enabled"] is False and d["events"] and len(d["roll"]) == 2
    assert {m["ticker"] for m in d["markets"]} >= {"CL", "GOLD"} and d["board"][0]["key"] == "weekend"


def test_the_public_link_sees_the_bets_page_only(client):
    cf = {"cf-connecting-ip": "203.0.113.9", "host": "quiet-river.trycloudflare.com"}
    r = client.get("/", headers=cf, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/bets"
    pub = client.get("/api/bets", headers=cf)
    assert pub.status_code == 200 and pub.json()["public"] is True and pub.headers["x-robots-tag"].startswith("noindex")
    for path in ("/api/health", "/api/snapshot", "/terminal", "/analyzer", "/api/analyzer", "/docs", "/openapi.json"):
        assert client.get(path, headers=cf).status_code == 404, path
    assert client.post("/api/bets/odds", headers=cf, json={"book": "x", "rows": []}).status_code == 403   # no token set
    assert client.post("/api/alerts", headers=cf, json={}).status_code == 404
    combo = client.get("/api/bets/combo?target=50&days=3", headers=cf)
    assert combo.status_code == 200 and combo.json()["target"] == 50 and combo.json()["days"] == 3
    code = client.get("/api/bets/combo/code?target=5", headers=cf).json()
    assert code["status"] == "none" and "laptop" not in code["why"] and "collector" not in code["why"]   # no owner's notes
    assert "SportyBet slip" in client.get("/api/bets/combo/code?target=5", headers={"host": "localhost:8001"}).json()["why"]
    assert client.get("/api/bets/code-jobs", headers=cf).status_code == 403                      # the laptop's side: a token
    assert client.get("/api/health", headers={"host": "quiet-river.trycloudflare.com"}).status_code == 404   # host alone
    assert client.get("/robots.txt", headers=cf).text.startswith("User-agent: *")
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers=cf):
            pass
    # the SSH tunnel (localhost) still sees everything, and isn't marked public
    assert client.get("/api/health", headers={"host": "localhost:8001"}).status_code == 200
    assert client.get("/api/bets", headers={"host": "localhost:8001"}).json()["public"] is False


def test_scrub():
    from api.public import scrub
    assert scrub("Client error for url 'https://api.the-odds-api.com/v4/sports?apiKey=abc123&x=1'") == \
        "Client error for url 'https://api.the-odds-api.com/v4/sports?apiKey=…&x=1'"


def test_cache_headers():
    import asyncio
    from api.public import CacheHeaders

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"cache-control", b"x")]})
        await send({"type": "http.response.body", "body": b""})

    def headers(path):
        got = []

        async def send(msg):
            got.append(msg)
        asyncio.run(CacheHeaders(app)({"type": "http", "path": path}, None, send))
        return dict(got[0]["headers"]).get(b"cache-control")
    assert headers("/assets/index-abc.js") == b"public, max-age=31536000, immutable"
    assert headers("/bets") == b"no-cache" and headers("/api/bets") == b"x"


def test_the_collector_through_the_public_link_needs_the_token(client, monkeypatch):
    cf = {"cf-connecting-ip": "203.0.113.9", "host": "quiet-river.trycloudflare.com"}
    monkeypatch.setenv("ANALYTIX_BETS_TOKEN", "s3cret-token")
    body = {"book": "bet9ja", "rows": [{"home": "Arsenal", "away": "Fulham", "t_ms": 1_800_000_000_000, "odds": {"home": 1.4}}]}
    assert client.post("/api/bets/odds", headers=cf, json=body).status_code == 403
    assert client.post("/api/bets/odds", headers={**cf, "x-bets-token": "wrong"}, json=body).status_code == 403
    ok = client.post("/api/bets/odds", headers={**cf, "x-bets-token": "s3cret-token"}, json=body)
    assert ok.status_code == 200 and ok.json()["book"] == "bet9ja" and ok.json()["games"] == 1
    big = client.post("/api/bets/odds", headers={**cf, "x-bets-token": "s3cret-token", "content-length": "9000000"}, content=b"{}")
    assert big.status_code == 413
    assert client.get("/api/bets/code-jobs", headers=cf).status_code == 403
    assert client.get("/api/bets/code-jobs", headers={**cf, "x-bets-token": "s3cret-token"}).json() == []
    done = client.post("/api/bets/code-jobs", headers={**cf, "x-bets-token": "s3cret-token"}, json={"id": "nope", "code": "X"})
    assert done.status_code == 200 and done.json() == {"ok": False}
    # the tunnel needs it too once it's set
    assert client.get("/api/bets/code-jobs", headers={"host": "localhost:8001"}).status_code == 403
    assert client.post("/api/health", headers={**cf, "x-bets-token": "s3cret-token"}, json={}).status_code == 404   # nothing else


def test_the_collector_remembers_the_link(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location("naijaOdds", P(__file__).parent.parent / "scripts" / "naijaOdds.py")
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    monkeypatch.setattr(N, "SAVED", str(tmp_path / "naijaOdds.json"))
    assert N.saved() == {}
    (tmp_path / "naijaOdds.json").write_text('{"server": "https://quiet-river.trycloudflare.com", "token": "t"}')
    assert N.saved()["server"].endswith("trycloudflare.com")
    (tmp_path / "naijaOdds.json").write_text("not json")
    assert N.saved() == {}

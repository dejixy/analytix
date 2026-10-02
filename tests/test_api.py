import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from ingestion.synthetic import generate_session


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = generate_session(tmp_path_factory.mktemp("api") / "session.jsonl")
    app = create_app(mode="replay", replay_file=path, speed=float("inf"), loop=False, serve_frontend=False)
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
    assert breaks and breaks[0]["direction"] == "down" and breaks[0]["title"].startswith("Bid level")
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

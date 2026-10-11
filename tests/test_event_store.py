from datetime import datetime, timedelta, timezone

from api.runtime import Runtime
from api.serializers import build_snapshot
from ingestion.replay import read_session
from ingestion.synthetic import generate_session
from storage.eventStore import EventStore

START = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(second=0, microsecond=0)   # within the week kept


def _runtime(tmp_path):
    return Runtime("live", coins=["ETH"], bars_db=tmp_path / "bars.sqlite", backfill_enabled=False, record=False,
                   record_file=None, alerts_file=tmp_path / "alerts.json", watch_file=tmp_path / "watch.json")


def test_store_writes_only_new_or_changed_events(tmp_path):
    st = EventStore(tmp_path / "e.sqlite")
    rows = [("a", "move", 1, {"id": "a", "x": 1}), ("b", "market", 2, {"id": "b"})]
    assert st.save("ETH", rows) == 2
    assert st.save("ETH", rows) == 0                                   # unchanged: nothing written
    assert st.save("ETH", [("a", "move", 1, {"id": "a", "x": 2})]) == 1
    assert EventStore(tmp_path / "e.sqlite").load("ETH", "move", 0, 10) == [{"id": "a", "x": 2}]


def test_the_event_log_survives_a_restart(tmp_path):
    rt = _runtime(tmp_path)
    rt._load_history()
    pipe = rt.pipeline("ETH")
    for _, msg in read_session(generate_session(tmp_path / "s.jsonl", start=START)):
        pipe.on_message(msg)
    live = build_snapshot(rt, "ETH")
    assert live["events"] and live["market_events"]
    rt._save_events()
    rt.event_store.close()

    after = _runtime(tmp_path)                                          # a fresh process, same disk
    after._load_history()
    an = after.pipeline("ETH").analyzer
    assert len(an.archived_moves) == len(pipe.analyzer.events) and an.archived_market
    snap = build_snapshot(after, "ETH")
    assert len(snap["events"]) == len(live["events"]) and len(snap["market_events"]) == len(live["market_events"])
    assert all(not e["active"] and isinstance(e["id"], str) for e in snap["events"])
    assert [e["peak_ms"] for e in snap["events"]] == [e["peak_ms"] for e in live["events"]]
    assert {e["title"] for e in snap["market_events"]} == {e["title"] for e in live["market_events"]}
    assert len({e["id"] for e in snap["market_events"]}) == len(snap["market_events"])   # unique across runs

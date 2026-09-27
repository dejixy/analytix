"""
End-to-end: replay the scripted synthetic session through the real pipeline
and check the engine finds the stories we planted.
"""
from datetime import datetime, timezone

import pytest

from ingestion.replay import read_session
from ingestion.synthetic import SCRIPT, generate_session
from models.signalModel import Direction
from pipeline import Pipeline

START = datetime(2026, 9, 24, 14, 20, tzinfo=timezone.utc)
T0 = int(START.timestamp() * 1000)


def regime(name: str):
    return next(r for r in SCRIPT if r.name == name)


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    path = generate_session(tmp_path_factory.mktemp("data") / "session.jsonl", start=START)
    pipe = Pipeline("ETH")
    snapshots = {}
    checkpoints = {"absorption": T0 + 480_000, "cascade": T0 + 1_005_000}
    for _, msg in read_session(path):
        pipe.on_message(msg)
        for key, at in checkpoints.items():
            if key not in snapshots and pipe.state.now_ms >= at:
                snapshots[key] = dict(pipe.analyzer.latest)
    return pipe, snapshots


def events_in(pipe, window, start_s, end_s):
    return [e for e in pipe.analyzer.events.recent(1000, window)
            if T0 + start_s * 1000 <= e.peak_ms <= T0 + end_s * 1000]


def test_no_events_dropped_and_engine_ticked(replayed):
    pipe, _ = replayed
    assert pipe.state.dropped == 0
    assert pipe.analyzer.runs >= 1400


def test_long_liquidation_cascade_is_found_and_explained(replayed):
    pipe, _ = replayed
    r = regime("long_liquidation")
    evs = events_in(pipe, "1m", r.start_s, r.end_s + 30)
    assert evs, "cascade not detected"
    ex = max(evs, key=lambda e: abs(e.explanation.move.z)).explanation
    assert ex.move.direction is Direction.DOWN
    assert "long-liquidation-style cascade" in ex.headline
    assert ex.drivers[0].name in {"liquidations", "volume_imbalance"}
    assert ex.confidence > 0.7


def test_squeeze_reads_as_shorts_covering(replayed):
    pipe, _ = replayed
    r = regime("short_squeeze")
    evs = events_in(pipe, "1m", r.start_s, r.end_s + 30)
    assert evs and all(e.explanation.move.direction is Direction.UP for e in evs)
    funding = evs[0].explanation.signals["funding"]
    assert "shorts covering" in funding.phrase or "flushed" in funding.phrase


def test_absorption_is_called_out(replayed):
    _, snaps = replayed
    ex = snaps["absorption"]["1m"]
    assert "absorbed" in ex.headline
    assert ex.signals["volume_imbalance"].direction is Direction.DOWN


def test_cascade_snapshot_has_all_drivers_aligned(replayed):
    _, snaps = replayed
    ex = snaps["cascade"]["1m"]
    supporting = {d.name for d in ex.drivers if d.alignment.value == "supports"}
    assert {"liquidations", "volume_imbalance", "depth_delta"} <= supporting


def test_timeframes_tell_different_stories(replayed):
    _, snaps = replayed
    heads = {w: snaps["cascade"][w].headline for w in ("1m", "10m", "60m")}
    assert len(set(heads.values())) == 3, heads


def test_longer_windows_describe_the_move_shape(replayed):
    pipe, _ = replayed
    r = regime("long_liquidation")
    ev = max(events_in(pipe, "10m", r.start_s, r.end_s + 600), key=lambda e: abs(e.explanation.move.z))
    assert ev.explanation.shape == "burst" and "one sharp minute" in ev.explanation.headline

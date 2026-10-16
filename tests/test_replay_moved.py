import gzip
import json

from ingestion.replay import read_recordings, read_session


def _line(t):
    return json.dumps({"recv_ms": t, "msg": {"channel": "trades", "data": []}}) + "\n"


def test_a_recording_gzipped_mid_replay_is_read_from_the_gz(tmp_path):
    hour = tmp_path / "eth_20261015_2300.jsonl"
    with gzip.open(hour.with_name(hour.name + ".gz"), "wt") as fh:      # the recorder compressed it and deleted the .jsonl
        fh.write(_line(1) + _line(2))
    assert [t for t, _ in read_session(hour)] == [1, 2]


def test_a_recording_that_vanished_is_skipped_not_fatal(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text(_line(1))
    gone = tmp_path / "b.jsonl"
    assert list(read_session(gone)) == []
    assert [r for _, r, _ in read_recordings([a])] == [1]

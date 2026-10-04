import gzip

from ingestion.recorder import HOUR_MS, HourlyRecorder, hour_stamp
from ingestion.replay import read_recordings, read_session, recording_files

H0 = 1_790_000_000_000 // HOUR_MS * HOUR_MS          # an exact UTC hour, in the past


def _msg(i):
    return {"channel": "trades", "data": [{"coin": "ETH", "tid": i}]}


def test_hours_roll_into_gzipped_files_and_read_back_in_order(tmp_path):
    rec = HourlyRecorder(tmp_path, "eth", background=False, min_free_gb=0)
    times = [H0 + i * 300_000 for i in range(27)]           # every 5 minutes for 2h10m
    for i, t in enumerate(times):
        rec.write(_msg(i), t)
    rec.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"eth_{hour_stamp(H0 // HOUR_MS + k)}.jsonl.gz" for k in (0, 1)] + \
                    [f"eth_{hour_stamp(H0 // HOUR_MS + 2)}.jsonl"]          # the open hour stays plain
    rows = list(read_recordings([tmp_path]))
    assert [m["data"][0]["tid"] for _, _, m in rows] == list(range(27))
    assert [fresh for fresh, _, _ in rows] == [True] + [False] * 26       # one continuous session across files


def test_a_long_silence_starts_a_new_session(tmp_path):
    rec = HourlyRecorder(tmp_path, "eth", background=False, min_free_gb=0)
    rec.write(_msg(0), H0)
    rec.write(_msg(1), H0 + 60_000)
    rec.write(_msg(2), H0 + 60_000 + 11 * 60_000)        # recorder was off for 11 minutes
    rec.close()
    assert [fresh for fresh, _, _ in read_recordings([tmp_path])] == [True, False, True]


def test_leftover_plain_hours_are_compressed_on_start_and_never_overwritten(tmp_path):
    stamp = hour_stamp(H0 // HOUR_MS)
    (tmp_path / f"eth_{stamp}.jsonl").write_text('{"recv_ms":1,"msg":{"a":1}}\n')
    with gzip.open(tmp_path / f"eth_{stamp}.jsonl.gz", "wt") as fh:          # an earlier run of the same hour
        fh.write('{"recv_ms":0,"msg":{"a":0}}\n')
    HourlyRecorder(tmp_path, "eth", background=False, min_free_gb=0).close()
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"eth_{stamp}.jsonl.gz", f"eth_{stamp}_2.jsonl.gz"]
    assert len(recording_files([tmp_path])) == 2


def test_a_recording_cut_off_mid_write_is_read_up_to_the_damage(tmp_path):
    plain = tmp_path / "cut.jsonl"
    plain.write_text('{"recv_ms":1,"msg":{"a":1}}\n{"recv_ms":2,"msg":{"a"')
    assert [r for r, _ in read_session(plain)] == [1]
    full = gzip.compress(b'{"recv_ms":1,"msg":{}}\n' * 2000)
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(full[: len(full) // 2 + 20])
    assert 0 < sum(1 for _ in read_session(cut)) <= 2000


def test_recording_pauses_when_the_disk_is_nearly_full(tmp_path):
    rec = HourlyRecorder(tmp_path, "eth", background=False, min_free_gb=10_000_000)   # more than any disk
    rec.write(_msg(0), H0)
    rec.close()
    assert rec.paused and rec.lines == 0

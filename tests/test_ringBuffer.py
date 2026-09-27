import pytest

from buffer.ringBuffer import RingBuffer
from tests.helpers import T0, trade


def filled(max_seconds: int = 900, n_seconds: int = 1000) -> RingBuffer:
    buf = RingBuffer(max_seconds=max_seconds)
    for s in range(n_seconds):
        buf.push(trade(T0 + s * 1000))
    return buf


def test_evicts_relative_to_newest_item_not_wall_clock():
    buf = filled(max_seconds=900, n_seconds=1000)
    newest = buf.newest.timestamp
    assert buf.oldest.timestamp >= newest - 900_000
    assert len(buf) == 901  # inclusive of the item exactly at the cutoff


def test_window_returns_oldest_first_and_respects_bounds():
    buf = filled()
    w = buf.window(60)
    assert len(w) == 60
    assert [t.timestamp for t in w] == sorted(t.timestamp for t in w)
    assert w[-1].timestamp == buf.newest.timestamp
    assert w[0].timestamp > buf.newest.timestamp - 60_000


def test_window_with_shared_clock():
    buf = filled(n_seconds=100)
    later_now = buf.newest.timestamp + 30_000  # another stream's clock is 30s ahead
    assert len(buf.window(60, now_ms=later_now)) == 30


def test_window_guard_against_asking_for_more_than_held():
    buf = RingBuffer(max_seconds=60)
    with pytest.raises(ValueError):
        buf.window(900)


def test_window_named_and_unknown_name():
    buf = filled()
    assert len(buf.window_named("1m")) == 60
    with pytest.raises(ValueError):
        buf.window_named("3h")


def test_rejects_out_of_order_items():
    buf = RingBuffer(max_seconds=60)
    assert buf.push(trade(T0 + 5_000))
    assert not buf.push(trade(T0 + 1_000))
    assert buf.push(trade(T0 + 5_000, tid=2))  # equal timestamps are fine
    assert len(buf) == 2


def test_latest_at_is_state_as_of_a_moment():
    buf = filled(n_seconds=10)
    assert buf.latest_at(T0 + 4_500).timestamp == T0 + 4_000
    assert buf.latest_at(T0 - 1) is None


def test_empty_buffer_reads():
    buf = RingBuffer(max_seconds=60)
    assert buf.window(60) == []
    assert buf.newest is None and buf.latest_at(T0) is None
    assert buf.covered_seconds() == 0.0

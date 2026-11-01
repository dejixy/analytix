"""The long-tail recorder (api/longtail.py): which coins it records, and that it records without touching the
dashboard."""
import asyncio
from types import SimpleNamespace

from api import longtail
from api.longtail import LongTailRecorder, pick


def test_pick_skips_the_main_and_busiest_coins_and_keeps_current_ones():
    vols = {"BTC": 9e9, "ETH": 5e9, "XRP": 2e9, "DOGE": 1e9, "A": 5e7, "B": 4e7, "C": 3e7, "D": 2e7, "E": 1e6}
    got = pick(vols, exclude={"BTC", "ETH"}, current=[], n=3, skip=2, min_usd=2e6)
    assert got == ["A", "B", "C"]                         # XRP and DOGE skipped as busiest, E below the minimum
    vols["Z"] = 4.5e7                                     # a new busy coin appears
    assert pick(vols, {"BTC", "ETH"}, current=["A", "C", "D"], n=3, skip=2, min_usd=2e6) == ["A", "C", "D"]
    assert pick(vols, {"BTC", "ETH"}, current=["A", "E"], n=3, skip=2, min_usd=2e6) == ["A", "Z", "B"]


def test_it_starts_recording_once_the_scanner_has_volumes(tmp_path, monkeypatch):
    started = {}

    class FakeClient:
        def __init__(self, coins, on_message, status, recorder=None):
            started.update(coins=list(coins), recorder=recorder)
            self.on_message = on_message

        async def run(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(longtail, "HyperliquidClient", FakeClient)
    monkeypatch.setattr(longtail, "WAIT_S", 0.01)
    scanner = SimpleNamespace(contexts={})
    rt = SimpleNamespace(mode="live", record=True, coins=["BTC"], scanner=scanner)
    rec = LongTailRecorder(rt, folder=tmp_path, enabled=True)
    monkeypatch.setattr(longtail, "LONGTAIL_SKIP", 0)

    async def go():
        await rec.start()
        await asyncio.sleep(0.05)
        assert not started                                # no volumes yet: waits
        scanner.contexts.update({"BTC": {"day_volume": 9e9}, "AAA": {"day_volume": 3e7}})
        await asyncio.sleep(0.1)
        await rec.stop()

    asyncio.run(go())
    assert "AAA" in started["coins"] and "BTC" not in started["coins"]
    assert started["recorder"].prefix == "longtail" and started["recorder"].folder == tmp_path
    assert rec.client.on_message({"channel": "trades"}) is None              # nothing reaches the dashboard


def test_off_unless_the_main_feed_is_recorded(tmp_path):
    rt = SimpleNamespace(mode="live", record=False, coins=["BTC"])
    assert not LongTailRecorder(rt, folder=tmp_path, enabled=True).enabled
    rt.record = True
    assert LongTailRecorder(rt, folder=tmp_path, enabled=True).enabled

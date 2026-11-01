"""The live loud-market gauge (api/regime.py) and Degen trading smaller in loud windows."""
import asyncio
from types import SimpleNamespace

import numpy as np

from api.regime import RegimeService
from models.barModel import Bar

M = 60_000


def bars(days=5, seed=4):
    """Minute bars: busy, jumpy 13:00-16:00 UTC every day, calm otherwise."""
    rng = np.random.default_rng(seed)
    t0 = 1_790_000_000_000 // 86_400_000 * 86_400_000
    out, px = [], 100.0
    for i in range(days * 1440):
        t = t0 + i * M
        loud = 13 <= (t // 3_600_000) % 24 < 16
        px *= 1 + rng.standard_normal() * (0.0012 if loud else 0.0003)
        out.append(Bar(t, 60, px, px, px, px, fills=int(rng.poisson(60 if loud else 10))))
    return out


def test_the_gauge_calls_the_busy_hours_loud():
    bs = bars()
    pipe = SimpleNamespace(state=SimpleNamespace(bars=[]))
    rt = SimpleNamespace(mode="live", coins=["ETH"], pipelines={"ETH": pipe}, store=None)
    svc = RegimeService(rt, keep_days=10)
    svc.bars["ETH"] = {b.timestamp: b for b in bs}
    import api.regime as regime
    real = regime.time.time

    def at(ms):
        regime.time.time = lambda: ms / 1000
        asyncio.run(svc.refresh())
        return svc.of("ETH")
    try:
        end = bs[-1].timestamp + M
        day = end // 86_400_000 * 86_400_000 - 86_400_000          # the last full day
        loud, quiet = at(day + 14 * 3_600_000 + 30 * M), at(day + 6 * 3_600_000 + 30 * M)
    finally:
        regime.time.time = real
    assert loud["regime"] == "loud" and quiet["regime"] == "quiet"
    assert loud["move_bps"] > 2 * quiet["move_bps"]


def test_no_reading_before_enough_bars():
    rt = SimpleNamespace(mode="live", coins=["ETH"], pipelines={}, store=None)
    svc = RegimeService(rt, keep_days=10)
    svc.bars["ETH"] = {b.timestamp: b for b in bars(days=2)}          # under three days: not enough yet
    import api.regime as regime
    real = regime.time.time
    regime.time.time = lambda: (max(svc.bars["ETH"]) + M) / 1000
    try:
        asyncio.run(svc.refresh())
    finally:
        regime.time.time = real
    asyncio.run(svc.refresh())
    assert svc.of("ETH")["regime"] is None and svc.public() == {"ETH": svc.of("ETH")}

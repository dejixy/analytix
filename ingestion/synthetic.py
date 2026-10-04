"""
A small scripted market simulator that emits a session in Hyperliquid's exact
wire format (trades / l2Book / activeAssetCtx), recorded as JSONL.

Why it exists: the dashboard and tests need data when there's no network, and
a scripted session has *known answers*. We know a long-liquidation cascade
starts at 16:00 into the session, so a test can assert the engine finds it.

It is deliberately simple — not a realistic market model. Replace it with a
real recording (scripts/record.py) as soon as you have one.
"""
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TICK = 0.1
LEVELS = 20
DT = 0.1                 # seconds per simulation step (~one block)
BOOK_EVERY_S = 1.0
CTX_EVERY_S = 3.0
NOISE_BPS_SQRT_S = 0.5   # random-walk volatility of the mid, on top of flow impact
IMPACT_BPS_PER_COIN = 0.08  # each taker order nudges the mid in its direction
BASE_LEVEL_SIZE = 12.0   # coins per book level before multipliers
BASE_FUNDING = 0.0000125 # 0.00125%/h ≈ 11% APR — Hyperliquid's baseline
# One algorithmic seller works a big order through the quiet stretch — a slice every 30s, no tx hash — and
# passive bids absorb it: the "95% sell, price flat" tape that's really one TWAP.
TWAP_START_S, TWAP_END_S, TWAP_EVERY_S, TWAP_SLICE = 680, 950, 30.0, 60.0


@dataclass(frozen=True)
class Regime:
    name: str
    start_s: int
    end_s: int
    drift_bps_s: float       # price drift on top of flow impact (e.g. a passive buyer lifting bids)
    buy_prob: float          # share of aggressive orders that are buys
    trade_rate: float        # aggressive orders per second
    bid_depth: float         # target multiplier on resting bid size
    ask_depth: float         # target multiplier on resting ask size
    sweep_rate: float = 0.0  # large multi-level sweeps per second
    sweep_side: str = "B"
    oi_pct_per_min: float = 0.0
    funding_target: float = BASE_FUNDING
    story: str = ""


SCRIPT: list[Regime] = [
    Regime("chop", 0, 240, 0.0, 0.50, 4, 1.0, 1.0,
           story="Balanced two-way flow"),
    Regime("buy_pressure", 240, 360, 0.0, 0.72, 9, 1.15, 0.45, 0.06, "B", 0.5, 0.00002,
           story="Aggressive buying into thinning asks; new longs opening"),
    Regime("absorption", 360, 540, 0.42, 0.30, 8, 1.7, 1.0, 0.0, "A", 0.05, 0.00002,
           story="Heavy selling absorbed by refilling bids — price holds"),
    Regime("short_squeeze", 540, 660, 0.1, 0.70, 10, 1.1, 0.5, 0.12, "B", -0.8, 0.00003,
           story="Short squeeze: price up while open interest falls"),
    Regime("quiet", 660, 960, -0.01, 0.49, 3.5, 1.0, 1.0, 0.0, "B", 0.05, 0.00004,
           story="Quiet drift; funding climbs as longs crowd in"),
    Regime("long_liquidation", 960, 1020, -0.4, 0.18, 16, 0.35, 1.1, 0.6, "A", -2.0, 0.00003,
           story="Long liquidation cascade: sell sweeps, bids pulled, OI collapses"),
    Regime("rebound", 1020, 1200, 0.05, 0.62, 7, 1.2, 0.9, 0.02, "B", 0.2, 0.000015,
           story="Rebound on steady dip-buying"),
    Regime("chop", 1200, 1500, 0.0, 0.50, 4, 1.0, 1.0,
           story="Back to balanced chop"),
]


def regime_at(t: float) -> Regime:
    for r in SCRIPT:
        if r.start_s <= t < r.end_s:
            return r
    return SCRIPT[-1]


def _fmt_px(p: float) -> str:
    return f"{p:.1f}"


def _fmt_sz(s: float) -> str:
    return f"{s:.4f}"


def generate_session(
    path: str | Path,
    start: datetime = datetime(2026, 9, 24, 14, 20, tzinfo=timezone.utc),
    start_price: float = 2650.0,
    coin: str = "ETH",
    seed: int = 7,
) -> Path:
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0_ms = int(start.timestamp() * 1000)
    duration = SCRIPT[-1].end_s

    wallets = ["0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40)) for _ in range(400)]
    twap_wallet = "0x7a9" + "".join(rng.choice("0123456789abcdef") for _ in range(37))
    next_twap = TWAP_START_S
    mid = start_price
    bid_mult = ask_mult = 1.0
    oi = 600_000.0
    funding = BASE_FUNDING
    last_recv = t0_ms
    lines: list[str] = []

    def emit(ts: int, msg: dict) -> None:
        nonlocal last_recv
        # TCP keeps one socket's messages in order, so receive time is monotonic.
        last_recv = max(last_recv, ts + rng.randint(40, 140))
        lines.append(json.dumps({"recv_ms": last_recv, "msg": msg}, separators=(",", ":")))

    for sub in ("trades", "l2Book", "activeAssetCtx"):
        emit(t0_ms, {"channel": "subscriptionResponse",
                     "data": {"method": "subscribe", "subscription": {"type": sub, "coin": coin}}})

    def best_prices() -> tuple[float, float]:
        bb = math.floor(mid / TICK) * TICK
        return round(bb, 1), round(bb + TICK, 1)

    def level_size(mult: float, i: int) -> float:
        return BASE_LEVEL_SIZE * mult * rng.uniform(0.5, 1.5) * (1 + 0.05 * i)

    def aggressive_order(ts: int, side: str, size: float, max_levels: int, taker: str | None = None,
                         engine: bool = False) -> list[dict]:
        """Walk the book from the touch; fills of one order share a hash. Engine-executed orders (TWAP slices,
        liquidations) have none: all zeros."""
        bb, ba = best_prices()
        h = "0x" + ("0" * 64 if engine else "".join(rng.choice("0123456789abcdef") for _ in range(64)))
        fills, remaining, lvl = [], size, 0
        taker_wallet = taker or rng.choice(wallets)
        while remaining > 1e-9 and lvl < max_levels:
            px = ba + lvl * TICK if side == "B" else bb - lvl * TICK
            avail = level_size(ask_mult if side == "B" else bid_mult, lvl)
            take = min(remaining, avail)
            maker = rng.choice(wallets)
            taker = taker_wallet
            users = [taker, maker] if side == "B" else [maker, taker]
            fills.append({"coin": coin, "side": side, "px": _fmt_px(px), "sz": _fmt_sz(take),
                          "hash": h, "time": ts, "tid": rng.getrandbits(50), "users": users})
            remaining -= take
            lvl += 1
        return fills

    steps = int(duration / DT)
    next_book, next_ctx = 0.0, 0.0
    for step in range(steps):
        t = step * DT
        ts = t0_ms + int(round(t * 1000))
        r = regime_at(t)

        # depth relaxes toward the regime target over ~10s
        bid_mult += (r.bid_depth - bid_mult) * (DT / 10)
        ask_mult += (r.ask_depth - ask_mult) * (DT / 10)

        # price: drift + noise
        shock = r.drift_bps_s * DT + NOISE_BPS_SQRT_S * math.sqrt(DT) * rng.gauss(0, 1)
        mid *= math.exp(shock / 10_000)

        fills: list[dict] = []
        # ordinary aggressive orders
        n = _poisson(rng, r.trade_rate * DT)
        for _ in range(n):
            side = "B" if rng.random() < r.buy_prob else "A"
            size = rng.lognormvariate(math.log(0.9), 1.0)
            fills += aggressive_order(ts, side, size, max_levels=LEVELS)
            mid *= math.exp((1 if side == "B" else -1) * size * IMPACT_BPS_PER_COIN / 10_000)
        # large sweeps: sized off the *normal* book, so a thin book gets walked further
        if r.sweep_rate and rng.random() < r.sweep_rate * DT:
            size = BASE_LEVEL_SIZE * rng.randint(3, 7) * rng.uniform(0.9, 1.3)
            # in the liquidation phase the sweeps are the exchange closing accounts: engine-executed
            sweep = aggressive_order(ts, r.sweep_side, size, max_levels=LEVELS, engine=r.name == "long_liquidation")
            fills += sweep
            impact = len(sweep) * TICK * 0.5
            mid += impact if r.sweep_side == "B" else -impact
        # a TWAP seller working an order through the absorption phase: one zero-hash slice every 30s
        if TWAP_START_S <= t < TWAP_END_S and t >= next_twap:
            next_twap += TWAP_EVERY_S
            fills += aggressive_order(ts, "A", TWAP_SLICE * rng.uniform(0.9, 1.1), max_levels=LEVELS,
                                      taker=twap_wallet, engine=True)
        if fills:
            emit(ts, {"channel": "trades", "data": fills})

        if t >= next_book:
            next_book += BOOK_EVERY_S
            bb, ba = best_prices()
            bids = [{"px": _fmt_px(bb - i * TICK), "sz": _fmt_sz(level_size(bid_mult, i)), "n": rng.randint(1, 15)}
                    for i in range(LEVELS)]
            asks = [{"px": _fmt_px(ba + i * TICK), "sz": _fmt_sz(level_size(ask_mult, i)), "n": rng.randint(1, 15)}
                    for i in range(LEVELS)]
            emit(ts, {"channel": "l2Book", "data": {"coin": coin, "time": ts, "levels": [bids, asks]}})

        if t >= next_ctx:
            next_ctx += CTX_EVERY_S
            oi *= 1 + r.oi_pct_per_min / 100 * (CTX_EVERY_S / 60) + rng.gauss(0, 0.00003)
            funding += (r.funding_target - funding) * 0.08
            book_mid = sum(best_prices()) / 2
            oracle = book_mid * (1 - 0.00002)
            mark = book_mid * (1 + funding * 2 + rng.gauss(0, 0.00001))
            emit(ts, {"channel": "activeAssetCtx", "data": {"coin": coin, "ctx": {
                "funding": f"{funding:.8f}", "openInterest": f"{oi:.4f}",
                "prevDayPx": _fmt_px(start_price * 0.99), "dayNtlVlm": "1843250000.0",
                "premium": f"{(mark - oracle) / oracle:.6f}", "oraclePx": _fmt_px(oracle),
                "markPx": _fmt_px(mark), "midPx": f"{book_mid:.2f}",
                "impactPxs": [_fmt_px(book_mid - 0.2), _fmt_px(book_mid + 0.2)],
            }}})

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth — fine for the small lambdas used per step
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1

"""
The auto hunter's setups: moments Analytix already recognises that often come before a move, turned into trade
triggers. Each one fires once when a new opportunity appears (not every second it lasts), with a direction.

    lead       Binance moved first and Hyperliquid hasn't caught up: trade toward Binance
    flow       market orders and the resting book lean the same way on the 1m card
    winning    one side is doing most of the 1m market orders and price is moving their way (not being soaked up),
               with the book not leaning against them: join the winning side
    breakout   a significant 1m move with market orders behind it: follow it
    cascade    a new chain of sweeps or liquidations is forcing one side out: follow it
    fade       a cascade has just ended: trade the snap back
    absorb     heavy one-way market orders that aren't moving price (1m or 10m): trade against them
    level      a defended price level gives way: follow the break

The auto hunter runs every setup on every coin with three exit profiles (quick, standard, runner) and both order
styles, so its leaderboard shows which combinations survive Hyperliquid's costs.
"""
import math
from dataclasses import dataclass, field

from engine.bot import card_signal
from models.explanationModel import Significance
from models.signalModel import Direction
from models.tradeModel import TradeSide

SETUPS = {
    "lead": ("Binance leads", "Binance moved first and Hyperliquid hasn't caught up yet"),
    "flow": ("Flow + Book", "Market orders and the resting book lean the same way on the 1m card"),
    "winning": ("Winning side", "Mostly buyers (or sellers) on 1m and price moving their way, with the book not against them"),
    "breakout": ("Breakout", "A significant 1m move with market orders behind it"),
    "cascade": ("Cascade", "A chain of sweeps or liquidations forcing one side out: follow it"),
    "fade": ("Cascade fade", "A cascade just ended: trade the snap back"),
    "absorb": ("Absorption", "Heavy one-way flow that isn't moving price: trade against it"),
    "level": ("Level break", "A defended price level gave way: follow the break"),
}
EXITS = {   # name: (target bps, stop bps, max hold s, limit wait s)
    "quick": (12.0, 8.0, 90, 5),
    "standard": (25.0, 15.0, 300, 10),
    "runner": (60.0, 30.0, 1800, 20),
}
EXIT_WORDS = {"quick": "Quick", "standard": "Standard", "runner": "Runner"}

LEAD_SPAN_S = 3              # Binance's move over the last few seconds
LEAD_MIN_BPS = 2.0           # at least this, and at least LEAD_SIGMAS of a normal move over the span
LEAD_SIGMAS = 2.5
LEAD_LAG_SHARE = 0.5         # Hyperliquid has made at most half of Binance's move
LEAD_LOCK_S = 5              # one trigger per burst
CASCADE_FRESH_S = 30         # a cascade counts as new for this long after it starts
FADE_FRESH_S = 30            # ...and as just-ended for this long after it ends
ABSORB_TAGS = ("price held", "rose anyway", "fell anyway")


@dataclass
class CoinMemory:
    """What each setup saw last time, so it fires on a new opportunity rather than every second of one."""
    state: dict = field(default_factory=dict)          # setup → last direction seen (for level-triggered ones)
    seen: set = field(default_factory=set)             # event ids already traded (cascades, levels)
    lead_until_ms: int = 0
    primed: bool = False                               # the first pass only records what's already going on


def _edge(mem: CoinMemory, key: str, side: int) -> int:
    """+1/-1 the first second a condition holds, 0 while it keeps holding or doesn't."""
    prev = mem.state.get(key, 0)
    mem.state[key] = side
    return side if side and side != prev else 0


def _dir(d: Direction) -> int:
    return 1 if d is Direction.UP else -1 if d is Direction.DOWN else 0


def lead_signal(st, sigma_1s_bps: float | None, now_ms: int) -> tuple[int, float, float]:
    """(side, Binance's move, Hyperliquid's move) over the last few seconds; side is 0 unless Binance moved
    clearly and Hyperliquid has made at most half of it."""
    ext = (getattr(st, "external", None) or {}).get("binance")
    last = st.books.newest
    if ext is None or not len(ext) or last is None:
        return 0, 0.0, 0.0
    q = ext.window(LEAD_SPAN_S)
    if len(q) < 2:
        return 0, 0.0, 0.0
    b_bps = (q[-1].mid / q[0].mid - 1) * 1e4
    h0 = st.books.latest_at(q[0].timestamp)
    if h0 is None or h0.mid <= 0:
        return 0, b_bps, 0.0
    h_bps = (last.mid / h0.mid - 1) * 1e4
    need = max(LEAD_MIN_BPS, LEAD_SIGMAS * (sigma_1s_bps or 1.0) * math.sqrt(LEAD_SPAN_S))
    if abs(b_bps) < need:
        return 0, b_bps, h_bps
    side = 1 if b_bps > 0 else -1
    if side * h_bps > LEAD_LAG_SHARE * abs(b_bps):
        return 0, b_bps, h_bps
    return side, b_bps, h_bps


def detect(pipe, mem: CoinMemory, now_ms: int) -> dict[str, int]:
    """Every setup's trigger for this coin right now: {setup: +1 long, -1 short}, only the ones that fired."""
    st, an = pipe.state, pipe.analyzer
    out: dict[str, int] = {}
    ex1, ex10 = an.latest.get("1m"), an.latest.get("10m")
    rows1 = {m.key: m for m in (ex1.summary if ex1 else ())}

    # lead: Binance moved, Hyperliquid hasn't yet
    baseline = getattr(an, "baseline", None)
    side, _, _ = lead_signal(st, getattr(baseline, "sigma_1s_bps", None), now_ms)
    if side and now_ms >= mem.lead_until_ms:
        out["lead"] = side
        mem.lead_until_ms = now_ms + LEAD_LOCK_S * 1000

    # flow: Flow and Book agree on 1m
    s = _edge(mem, "flow", card_signal(ex1.summary, ("flow", "book"), 0.3) if ex1 else 0)
    if s:
        out["flow"] = s

    # winning: one side dominates 1m market orders and price is going their way (notable move, not absorbed)
    w = 0
    flow, bk = rows1.get("flow"), rows1.get("book")
    if ex1 is not None and flow is not None and not flow.partial and ex1.coverage >= 0.95 \
            and ex1.move.significance is not Significance.QUIET and flow.tag not in ABSORB_TAGS:
        d = _dir(flow.lean)
        if d and d == _dir(ex1.move.direction) and (bk is None or _dir(bk.lean) != -d):
            w = d
    s = _edge(mem, "winning", w)
    if s:
        out["winning"] = s

    # breakout: a significant 1m move with flow behind it
    b = 0
    if ex1 is not None and ex1.coverage >= 0.95 and ex1.move.significance is Significance.SIGNIFICANT:
        flow = rows1.get("flow")
        d = _dir(ex1.move.direction)
        if flow is not None and not flow.partial and _dir(flow.lean) == d:
            b = d
    s = _edge(mem, "breakout", b)
    if s:
        out["breakout"] = s

    # cascade and fade: new chains of sweeps, and ones that just ended
    for c in list(getattr(getattr(an, "cascades", None), "_items", ())):
        side = 1 if c.side is TradeSide.BUY else -1
        if ("c", c.id) not in mem.seen and now_ms - c.start_ms <= CASCADE_FRESH_S * 1000:
            mem.seen.add(("c", c.id))
            out["cascade"] = side
        if c.done and ("f", c.id) not in mem.seen:
            mem.seen.add(("f", c.id))
            if now_ms - c.end_ms <= FADE_FRESH_S * 1000:
                out["fade"] = -side

    # absorb: heavy flow one way, price not following
    a = 0
    for ex in (ex1, ex10):
        flow = next((m for m in (ex.summary if ex else ()) if m.key == "flow"), None)
        if ex is not None and flow is not None and not flow.partial and ex.coverage >= 0.95 and flow.tag in ABSORB_TAGS:
            selling = " sell" in f" {flow.value}"
            a = 1 if selling else -1
            break
    s = _edge(mem, "absorb", a)
    if s:
        out["absorb"] = s

    # level: a defended level broke
    for e in (an.market_events() if hasattr(an, "market_events") else ()):
        if e.kind == "level_break" and ("l", e.id) not in mem.seen:
            mem.seen.add(("l", e.id))
            d = _dir(e.direction)
            if d and now_ms - e.ts <= 30_000:
                out["level"] = d

    if len(mem.seen) > 5000:
        mem.seen = set(list(mem.seen)[-2000:])
    if not mem.primed:                     # don't trade whatever was already under way when the bot started
        mem.primed = True
        return {}
    return out

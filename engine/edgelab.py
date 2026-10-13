"""
Edge lab: replays the server's recordings through the real engine and measures every trading signal the way it
would actually trade, so an idea is tested on weeks of data in minutes instead of days of live shadow trading.

The rules come from the noise-and-cost review:

    events      signals the same way on the same coin within EVENT_GAP_S are one event with several tags, so one
                market move is counted once however many setups saw it
    prices      entry at the ask (long) or bid (short) a set delay after the signal, exit at the bid or ask:
                never the mid, so the spread is always paid
    speed       every event is priced at several entry delays (0.25 to 2 s) against the same exit, to see how much
                of the edge survives the time it takes to act
    costs       fees per side at the chosen tier, for a market round trip and for market in, limit out (a closing
                limit at the touch, filled only if a trade prints through it within 3 s, otherwise crossing)
    excursions  how far each trade went against (MAE) and for (MFE) before each hold time, to size stops beyond
                the dips winners make
    features    coin, session, volatility and spread against usual, the Binance move's size, Bybit agreeing, which
                way the book leans, how many setups fired together
    validation  results on the first part of the data (train) and the rest (test); a filter or setup counts only
                if it holds on test, and the bar is t ≥ 3 on train because so many things are tried
"""
import math
import time
from dataclasses import dataclass, field
from statistics import median

from config import WINDOWS
from engine.analyzer import Analyzer
from engine.hunter import SETUPS, CoinMemory, detect, lead_check
from ingestion.parsers import coin_of, parse_message
from state import MarketState

LATENCIES_MS = (250, 500, 1000, 2000)
BASE_LATENCY_MS = 500
HORIZONS_S = (5, 10, 30, 60, 120, 300, 900)
EVENT_GAP_S = 10
MAKER_WAIT_MS = 3000
LAB_WINDOWS = {k: WINDOWS[k] for k in ("1m", "10m", "60m")}       # all the setups read
SESSIONS = ((0, 7, "Asia"), (7, 13, "Europe"), (13, 20, "US"), (20, 24, "Late"))
LEAD_SIZES = ((0, 4, "under 4"), (4, 7, "4–7"), (7, 12, "7–12"), (12, 1e9, "12+"))


def session_of(t_ms: int) -> str:
    h = time.gmtime(t_ms / 1000).tm_hour
    return next(lab for lo, hi, lab in SESSIONS if lo <= h < hi)


@dataclass
class Event:
    id: int
    coin: str
    side: int
    t_ms: int
    last_ms: int
    tags: set
    mid0: float
    sigma_1s: float | None
    features: dict
    entries: dict = field(default_factory=dict)       # entry delay ms → price paid
    exits: dict = field(default_factory=dict)         # hold s → price a market close gets
    makers: dict = field(default_factory=dict)        # hold s → (price, filled as maker)
    mfe: dict = field(default_factory=dict)           # hold s → best excursion so far, bps
    mae: dict = field(default_factory=dict)           # hold s → worst excursion so far, bps
    best: float = -math.inf
    worst: float = math.inf
    waiting: dict = field(default_factory=dict)       # hold s → (closing limit price, deadline ms)

    @property
    def done(self) -> bool:
        return len(self.exits) == len(HORIZONS_S) and len(self.makers) == len(HORIZONS_S) and \
            len(self.entries) == len(LATENCIES_MS)

    def entry(self, latency: int = BASE_LATENCY_MS) -> float | None:
        return self.entries.get(latency)

    def gross(self, h: int) -> float | None:
        """bps a market round trip made before fees: in after the base delay, out h seconds later."""
        e, x = self.entry(), self.exits.get(h)
        return self.side * (x / e - 1) * 1e4 if e and x else None

    def maker_gross(self, h: int) -> tuple[float, bool] | None:
        e, m = self.entry(), self.makers.get(h)
        return (self.side * (m[0] / e - 1) * 1e4, m[1]) if e and m else None

    def latency_gross(self, latency: int, h: int = 30) -> float | None:
        """The same exit, entered after a different delay: what acting slower or faster is worth."""
        e, x = self.entries.get(latency), self.exits.get(h)
        return self.side * (x / e - 1) * 1e4 if e and x else None


class Tracker:
    """Prices a coin's open events as the recorded book and trades roll forward."""

    def __init__(self):
        self.pending: list[Event] = []
        self.prev: tuple[int, float, float] | None = None     # the book before the current one (ts, bid, ask)

    def add(self, e: Event) -> None:
        self.pending.append(e)

    def on_book(self, ts: int, bid: float, ask: float) -> list[Event]:
        prev = self.prev
        finished = []
        for e in self.pending:
            for lat in LATENCIES_MS:
                due = e.t_ms + lat
                if lat not in e.entries and ts >= due:
                    b, a = (prev[1], prev[2]) if prev and prev[0] <= due and ts > due else (bid, ask)
                    e.entries[lat] = a if e.side > 0 else b              # a market buy pays the ask
            entry = e.entry()
            if entry:
                exit_px = bid if e.side > 0 else ask
                x = e.side * (exit_px / entry - 1) * 1e4
                e.best, e.worst = max(e.best, x), min(e.worst, x)
                for h in HORIZONS_S:
                    due = e.t_ms + BASE_LATENCY_MS + h * 1000
                    if h not in e.exits and ts >= due:
                        b, a = (prev[1], prev[2]) if prev and prev[0] <= due and ts > due else (bid, ask)
                        e.exits[h] = b if e.side > 0 else a
                        e.mfe[h], e.mae[h] = e.best, e.worst
                        e.waiting[h] = ((a if e.side > 0 else b), due + MAKER_WAIT_MS)   # close with a limit
            for h, (px, deadline) in list(e.waiting.items()):
                if (e.side > 0 and bid >= px) or (e.side < 0 and ask <= px):
                    e.makers[h] = (px, True)
                    del e.waiting[h]
                elif ts >= deadline:
                    e.makers[h] = ((bid if e.side > 0 else ask), False)          # nobody took it: cross
                    del e.waiting[h]
            if e.done:
                finished.append(e)
        if finished:
            self.pending = [e for e in self.pending if not e.done]
        self.prev = (ts, bid, ask)
        return finished

    def on_trade(self, price: float) -> None:
        for e in self.pending:
            for h, (px, _) in list(e.waiting.items()):
                if (e.side > 0 and price > px) or (e.side < 0 and price < px):
                    e.makers[h] = (px, True)
                    del e.waiting[h]


class LabPipe:
    """A coin's state and analyzer, running only the windows the setups read (or none, for Binance leads alone)."""

    def __init__(self, coin: str, lead_only: bool = False):
        self.coin = coin
        self.state = MarketState(coin)
        self.analyzer = Analyzer(self.state, windows=LAB_WINDOWS)
        self.lead_only = lead_only
        self._baseline_ms = 0

    def on_message(self, msg: dict) -> None:
        self.state.apply_many(parse_message(msg, self.coin))
        if self.lead_only:
            now = self.state.now_ms
            if now - self._baseline_ms >= 60_000 and self.state.book is not None:
                self.analyzer.baseline = self.analyzer.compute_baseline()
                self._baseline_ms = now
        else:
            self.analyzer.maybe_run()


class Lab:
    """Feeds recorded messages through LabPipes, turns signals into events and prices them."""

    def __init__(self, coins: list[str], lead_only: bool = False, lead_min_bps: float = 0.0):
        self.coins = list(coins)
        self.lead_only = lead_only
        self.lead_min_bps = lead_min_bps
        self.events: list[Event] = []
        self.signals = 0
        self._next = 1
        self.reset()

    def reset(self) -> None:
        """A gap in the recordings: start the engine fresh; events still open are dropped (they can't be priced)."""
        self.pipes = {c: LabPipe(c, self.lead_only) for c in self.coins}
        self.mems = {c: CoinMemory() for c in self.coins}
        self.trackers = {c: Tracker() for c in self.coins}
        self.open: dict[str, Event] = {}
        self._runs = {c: 0 for c in self.coins}

    def on_message(self, msg: dict) -> None:
        coin = coin_of(msg)
        pipe = self.pipes.get(coin or "")
        if pipe is None:
            return
        pipe.on_message(msg)
        st, ch = pipe.state, msg.get("channel")
        now = st.now_ms
        if ch == "l2Book":
            b = st.books.newest
            if b is not None and b.best_bid > 0 and b.best_ask > 0:
                self.events.extend(self.trackers[coin].on_book(b.timestamp, b.best_bid, b.best_ask))
        elif ch == "trades":
            tr = self.trackers[coin]
            for t in msg.get("data") or []:
                try:
                    tr.on_trade(float(t["px"]))
                except (KeyError, TypeError, ValueError):
                    pass
        elif ch == "ext" and (msg.get("data") or {}).get("venue") == "binance" and now:
            side = lead_check(pipe, self.mems[coin], now, self.lead_min_bps)       # on every Binance quote
            if side:
                self.signal(coin, "lead", side, now, dict(self.mems[coin].lead_detail))
        if not self.lead_only and pipe.analyzer.runs != self._runs[coin] and now:
            self._runs[coin] = pipe.analyzer.runs
            for setup, side in detect(pipe, self.mems[coin], now, include_lead=False).items():
                self.signal(coin, setup, side, now, {})

    def signal(self, coin: str, setup: str, side: int, t: int, extra: dict) -> None:
        self.signals += 1
        ev = self.open.get(coin)
        if ev is not None and ev.side == side and t - ev.last_ms <= EVENT_GAP_S * 1000:
            ev.tags.add(setup)
            ev.last_ms = t
            for k, v in extra.items():
                ev.features.setdefault(k, v)
            return
        pipe = self.pipes[coin]
        st, an = pipe.state, pipe.analyzer
        b = st.books.newest
        if b is None:
            return
        ex10 = an.latest.get("10m")
        spreads = [x.spread_bps for x in st.books.window(300)]
        usual = median(spreads) if spreads else None
        feats = {"coin": coin, "session": session_of(t),
                 "vol_ratio": ex10.range_ratio if ex10 is not None else None,
                 "spread_bps": b.spread_bps, "spread_ratio": b.spread_bps / usual if usual else None,
                 "book_lean": b.imbalance * side, **extra}
        ev = Event(self._next, coin, side, t, t, {setup}, b.mid,
                   getattr(an.baseline, "sigma_1s_bps", None) if an.baseline else None, feats)
        self._next += 1
        self.open[coin] = ev
        self.trackers[coin].add(ev)


# ── statistics ──────────────────────────────────────────────────────────────
def summary(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "t": None, "hit": None}
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": mean, "t": mean / (sd / math.sqrt(n)) if n > 2 and sd > 0 else None,
            "hit": sum(1 for x in xs if x > 0) / n}


def net_market(e: Event, h: int, taker_bps: float) -> float | None:
    g = e.gross(h)
    return None if g is None else g - 2 * taker_bps


def net_maker(e: Event, h: int, taker_bps: float, maker_bps: float) -> float | None:
    m = e.maker_gross(h)
    return None if m is None else m[0] - taker_bps - (maker_bps if m[1] else taker_bps)


def split(events: list[Event], train: float = 0.6) -> tuple[list[Event], list[Event]]:
    """Train on the earlier part, test on the later: tuning never sees the test events."""
    evs = sorted(events, key=lambda e: e.t_ms)
    if not evs:
        return [], []
    cut = evs[0].t_ms + (evs[-1].t_ms - evs[0].t_ms) * train
    return [e for e in evs if e.t_ms <= cut], [e for e in evs if e.t_ms > cut]


def bucket(e: Event, feature: str) -> str | None:
    f = e.features
    if feature == "coin":
        return f.get("coin")
    if feature == "session":
        return f.get("session")
    if feature == "volatility":
        v = f.get("vol_ratio")
        return None if v is None else "quiet" if v < 0.75 else "normal" if v <= 1.5 else "busy"
    if feature == "spread":
        r = f.get("spread_ratio")
        return None if r is None else "usual" if r <= 1.2 else "wide"
    if feature == "book":
        lean = f.get("book_lean")
        return None if lean is None else "with us" if lean >= 0.2 else "against us" if lean <= -0.2 else "even"
    if feature == "together":
        return "alone" if len(e.tags) == 1 else "with others"
    if feature == "lead size":
        b = f.get("lead_bps")
        return None if b is None else next(lab for lo, hi, lab in LEAD_SIZES if lo <= abs(b) < hi)
    if feature == "bybit":
        b, y = f.get("lead_bps"), f.get("bybit_bps")
        if b is None or y is None:
            return None
        return "agrees" if y * b > 0 and abs(y) >= 0.5 * abs(b) else "doesn't"
    return None


FEATURES = ("coin", "session", "volatility", "spread", "book", "together", "lead size", "bybit")


def study(events: list[Event], taker_bps: float = 4.5, maker_bps: float = 1.5, train: float = 0.6) -> dict:
    """Everything the report prints, as data."""
    done = [e for e in events if e.entry()]
    tr, te = split(done, train)
    tags = sorted({t for e in done for t in e.tags}, key=lambda k: list(SETUPS).index(k) if k in SETUPS else 99)
    out = {"events": len(done), "train_events": len(tr), "test_events": len(te), "setups": [], "tests": 0,
           "span_h": (max(e.t_ms for e in done) - min(e.t_ms for e in done)) / 3_600_000 if done else 0.0}

    def series(evs, fn):
        return [x for x in (fn(e) for e in evs) if x is not None]

    for tag in tags:
        mine_tr = [e for e in tr if tag in e.tags]
        mine_te = [e for e in te if tag in e.tags]
        if not mine_tr and not mine_te:
            continue
        holds = []
        for h in HORIZONS_S:
            holds.append({"h": h,
                          "gross": summary(series(mine_tr, lambda e: e.gross(h))),
                          "market": summary(series(mine_tr, lambda e: net_market(e, h, taker_bps))),
                          "maker": summary(series(mine_tr, lambda e: net_maker(e, h, taker_bps, maker_bps))),
                          "maker_fill": summary(series(mine_tr, lambda e: 1.0 if (e.maker_gross(h) or (0, False))[1] else 0.0))})
            out["tests"] += 2
        best = max(holds, key=lambda r: max(r["market"]["mean"] or -1e9, r["maker"]["mean"] or -1e9))
        h = best["h"]
        how = "maker" if (best["maker"]["mean"] or -1e9) >= (best["market"]["mean"] or -1e9) else "market"
        fn = (lambda e: net_maker(e, h, taker_bps, maker_bps)) if how == "maker" else (lambda e: net_market(e, h, taker_bps))
        test = summary(series(mine_te, fn))
        tr_stat = best[how]
        verdict = ("edge" if (tr_stat["t"] or 0) >= 3 and (test["mean"] or -1) > 0 and (test["t"] or 0) >= 2
                   else "promising" if (tr_stat["mean"] or -1) > 0 and (tr_stat["t"] or 0) >= 2 and (test["mean"] or -1) > 0
                   else "no edge after costs")
        lat = {lat: summary(series(mine_tr + mine_te, lambda e, lat=lat: e.latency_gross(lat, 30))) for lat in LATENCIES_MS}
        filters = []
        for feat in FEATURES:
            for val in sorted({bucket(e, feat) for e in mine_tr + mine_te} - {None}):
                a = summary(series([e for e in mine_tr if bucket(e, feat) == val], fn))
                b = summary(series([e for e in mine_te if bucket(e, feat) == val], fn))
                out["tests"] += 1
                filters.append({"feature": feat, "value": val, "train": a, "test": b,
                                "holds": (a["t"] or 0) >= 3 and (b["mean"] or -1) > 0 and (b["t"] or 0) >= 2})
        winners = [e for e in mine_tr + mine_te if (e.gross(h) or 0) > 0 and h in e.mae]
        maes = sorted(-e.mae[h] for e in winners)
        stop = None
        if len(maes) >= 10:
            q = maes[int(0.9 * (len(maes) - 1))]
            stop_bps = max(1.0, q)
            capped = [(-stop_bps if e.mae.get(h, 0) <= -stop_bps else e.gross(h)) for e in mine_tr + mine_te if e.gross(h) is not None]
            stop = {"winners_mae_p50": maes[len(maes) // 2], "winners_mae_p90": q, "stop_bps": stop_bps,
                    "with_stop": summary([x - 2 * taker_bps for x in capped]),
                    "sigma_units": (q / ((sum(e.sigma_1s for e in winners if e.sigma_1s) / max(1, sum(1 for e in winners if e.sigma_1s))) * math.sqrt(h)))
                    if any(e.sigma_1s for e in winners) else None}
        out["setups"].append({"tag": tag, "label": SETUPS.get(tag, (tag,))[0], "events": len(mine_tr) + len(mine_te),
                              "holds": holds, "best_h": h, "best_how": how, "train": tr_stat, "test": test,
                              "verdict": verdict, "latency": lat, "filters": filters, "stop": stop})
    return out

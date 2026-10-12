"""
Who is usually right on Hyperliquid's prediction markets, and how well its prices predict. Both are learned from
the live feed and kept across restarts.

Sharp wallets (markouts, the way market makers find informed flow)
    Every trade is checked 15 minutes and 2 hours later: did the price move the way the wallet traded? A buy of
    Yes at 40¢ with the market at 46¢ two hours later is +6 points on that trade. Per wallet, size-weighted:

        average markout     mean points gained per contract (2-hour horizon)
        t-stat              that average over its standard error: how unlikely it is to be luck

    Sharp: at least 15 trades, +1.5 points or more on average, t-stat 2 or more. Fade: the mirror image.
    Each market then shows which way sharp wallets have traded it lately.

Calibration (does 90% mean 90%?)
    Each market's odds are noted 24 hours, 6 hours and 1 hour before it closes; once it settles, the odds are
    grouped in 10-point bands and compared with how often those markets really ended Yes. Prediction markets
    are known to overprice long shots; this measures it on Hyperliquid's own markets.
"""
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

HORIZONS = {"15m": 15 * 60_000, "2h": 2 * 3_600_000}
SCORE_HORIZON = "2h"
MIN_TRADES = 15
SHARP_PTS = 1.5
SHARP_T = 2.0
FLOW_KEEP_MS = 6 * 3_600_000
MAX_PENDING = 200_000
SNAP_BEFORE = {"24h": 24 * 3_600_000, "6h": 6 * 3_600_000, "1h": 3_600_000}
BANDS = [(i / 10, (i + 1) / 10) for i in range(10)]


@dataclass(slots=True)
class WalletStat:
    n: int = 0
    w: float = 0.0          # contracts
    s: float = 0.0          # sum of markout points x contracts
    s2: float = 0.0         # sum of markout² x contracts

    def add(self, pts: float, size: float) -> None:
        self.n += 1
        self.w += size
        self.s += pts * size
        self.s2 += pts * pts * size

    @property
    def avg(self) -> float:
        return self.s / self.w if self.w else 0.0

    @property
    def t(self) -> float:
        if self.n < 2 or self.w <= 0:
            return 0.0
        var = max(self.s2 / self.w - self.avg ** 2, 1e-9)
        return self.avg / math.sqrt(var / self.n)


@dataclass
class SharpBook:
    stats: dict = field(default_factory=lambda: {h: defaultdict(WalletStat) for h in HORIZONS})
    pending: dict = field(default_factory=lambda: {h: deque() for h in HORIZONS})   # per horizon, in due order:
    #                                                      (due ms, outcome, wallet, sign, price, size)
    flows: dict = field(default_factory=lambda: defaultdict(deque))   # outcome → recent (ms, wallet, signed size)
    dirty: set = field(default_factory=set)

    def on_trade(self, oid: int, t: dict, now_ms: int) -> None:
        """A live trade on outcome `oid`'s Yes side: {"t", "px", "sz", "buyer", "seller"}. Trades older than a
        minute (a feed's catch-up snapshot) are skipped: their later price isn't known any more."""
        if now_ms - t["t"] > 60_000 or sum(len(q) for q in self.pending.values()) > MAX_PENDING:
            return
        for wallet, sign in ((t.get("buyer"), 1), (t.get("seller"), -1)):
            if not wallet:
                continue
            for h, ms in HORIZONS.items():
                self.pending[h].append((t["t"] + ms, oid, wallet, sign, t["px"], t["sz"]))
            self.flows[oid].append((t["t"], wallet, sign * t["sz"]))
        q = self.flows[oid]
        while q and q[0][0] < now_ms - FLOW_KEEP_MS:
            q.popleft()

    def settle(self, now_ms: int, mid_of) -> int:
        """Score the trades whose horizon has passed, against the market's mid now (`mid_of(outcome)`; None when
        the book is too thin to say, and the trade is then dropped). Returns how many were scored."""
        done = 0
        for h, q in self.pending.items():
            while q and q[0][0] <= now_ms:
                _, oid, wallet, sign, px, size = q.popleft()
                mid = mid_of(oid)
                if mid is None:
                    continue
                self.stats[h][wallet].add(sign * (mid - px) * 100, size)
                self.dirty.add((wallet, h))
                done += 1
        return done

    def label(self, wallet: str) -> str | None:
        st = self.stats[SCORE_HORIZON].get(wallet)
        if not st or st.n < MIN_TRADES:
            return None
        if st.avg >= SHARP_PTS and st.t >= SHARP_T:
            return "sharp"
        if st.avg <= -SHARP_PTS and st.t <= -SHARP_T:
            return "fade"
        return None

    def wallet_row(self, wallet: str) -> dict:
        st, st15 = self.stats[SCORE_HORIZON].get(wallet), self.stats["15m"].get(wallet)
        return {"wallet": wallet, "label": self.label(wallet), "trades": st.n if st else 0,
                "avg_2h": round(st.avg, 2) if st else None, "t": round(st.t, 2) if st else None,
                "avg_15m": round(st15.avg, 2) if st15 else None}

    def flow(self, oid: int, since_ms: int) -> dict:
        """Net Yes contracts bought by sharp wallets (and by fade wallets) since a moment."""
        sharp, fade, who = 0.0, 0.0, {}
        for t, wallet, signed in self.flows.get(oid, ()):
            if t < since_ms:
                continue
            lab = self.label(wallet)
            if lab == "sharp":
                sharp += signed
                who[wallet] = who.get(wallet, 0.0) + signed
            elif lab == "fade":
                fade += signed
        wallets = sorted(({"wallet": w, "net": v, **self.wallet_row(w)} for w, v in who.items()), key=lambda r: -abs(r["net"]))
        return {"sharp_net": sharp, "fade_net": fade, "sharp_wallets": wallets[:5]}

    def summary(self) -> dict:
        st = self.stats[SCORE_HORIZON]
        labels = [self.label(w) for w in st]
        return {"wallets": len(st), "scored_trades": sum(s.n for s in st.values()),
                "sharp": labels.count("sharp"), "fade": labels.count("fade"),
                "pending": sum(len(q) for q in self.pending.values())}


@dataclass
class Calibration:
    snaps: dict = field(default_factory=lambda: defaultdict(dict))    # outcome → {"24h": odds, ...}
    results: list = field(default_factory=list)                       # (outcome, {horizon: odds}, 1|0)

    def note(self, oid: int, expiry_ms: int | None, odds: float | None, now_ms: int) -> bool:
        """Record the odds the first time we see a market inside each pre-close window. True if anything new."""
        if expiry_ms is None or odds is None or now_ms >= expiry_ms:
            return False
        new = False
        for h, before in SNAP_BEFORE.items():
            if expiry_ms - now_ms <= before and h not in self.snaps[oid]:
                self.snaps[oid][h] = odds
                new = True
        return new

    def ref_due(self, oid: int, expiry_ms: int | None, now_ms: int) -> str | None:
        """The tightest pre-close checkpoint that has no saved estimate yet, or None. Wider checkpoints first seen
        at this same moment are marked done, so one moment is never scored twice."""
        if expiry_ms is None or now_ms >= expiry_ms:
            return None
        have = self.snaps.get(oid) or {}
        due = [h for h, before in SNAP_BEFORE.items() if expiry_ms - now_ms <= before and f"ref:{h}" not in have]
        if not due:
            return None
        tight = min(due, key=lambda h: SNAP_BEFORE[h])
        for h in due:
            if h != tight:
                self.snaps[oid][f"ref:{h}"] = None
        return tight

    def note_ref(self, oid: int, horizon: str, rec: dict) -> None:
        self.snaps[oid][f"ref:{horizon}"] = rec

    def resolve(self, oid: int, outcome: int) -> tuple | None:
        snaps = self.snaps.pop(oid, None)
        if not snaps:
            return None
        row = (oid, snaps, outcome)
        self.results.append(row)
        return row

    def table(self, horizon: str = "6h") -> dict:
        rows = []
        n = 0
        for lo, hi in BANDS:
            got = [(s[horizon], y) for _, s, y in self.results
                   if isinstance(s.get(horizon), (int, float)) and lo <= s[horizon] < hi + (hi == 1.0)]
            n += len(got)
            if got:
                rows.append({"band": f"{int(lo * 100)}–{int(hi * 100)}%", "n": len(got),
                             "priced": sum(p for p, _ in got) / len(got), "happened": sum(y for _, y in got) / len(got)})
        return {"horizon": horizon, "settled": n, "rows": rows}


def resolved_value(odds: float | None) -> int | None:
    """A market that has left the listing: its last odds say how it ended, when they're clear."""
    if odds is None:
        return None
    if odds >= 0.97:
        return 1
    if odds <= 0.03:
        return 0
    return None

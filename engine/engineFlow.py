"""
Engine-executed fills: telling TWAP slices from liquidations.

Fills the matching engine generates itself carry an all-zero transaction hash.
On Hyperliquid those are TWAP slices, liquidations and auto-deleveraging. They
behave very differently, and the rules that create them make them separable:

  TWAP          Suborders at a fixed interval of at least 30 seconds, each about the
                same size (±20% if randomised), for 5 minutes to 7 days. → one wallet,
                one side, a regular cadence.
  Liquidation   A market order for the whole position — or 20% of it for positions
                over $100K, then the rest within a 30-second cooldown. In a sell-off
                many accounts hit maintenance margin in the same few seconds.
                → several wallets in the same instant on the same side, or one wallet's
                20% chunk followed by the remaining ~80%.

So an engine order is:
  twap        its wallet has ≥ 3 engine orders on one side at a regular interval ≥ 25s
  forced      ≥ 3 distinct (non-TWAP) wallets' engine orders on one side within 3s, or
              the same wallet's second engine order within 31s at ≥ 3.5× the first
  otherwise   unclassified — a lone engine fill could be a small liquidation or a TWAP's
              first slice, and we don't guess

Classification is by wallet (TWAP) and by (time, wallet) (forced), kept in sets, so
orders already in the buffers are reclassified as soon as the pattern appears.
"""
from collections import deque

from models.orderModel import AggressiveOrder
from models.tradeModel import TradeSide

TWAP_MIN_ORDERS = 3
TWAP_MIN_GAP_MS = 25_000
TWAP_GAP_TOLERANCE = 0.2          # every recent gap within ±20% (and ±3s) of the median: a fixed interval
CLUSTER_MS = 3_000
CLUSTER_WALLETS = 3
PARTIAL_LIQ_MS = 31_000
PARTIAL_LIQ_RATIO = 3.5           # 20% chunk, then the remaining 80% (4×); a TWAP catch-up is at most 3×
KEEP_MS = 3_600_000


class EngineFlow:
    def __init__(self):
        self._by_wallet: dict[tuple[str, TradeSide], deque[tuple[int, float]]] = {}
        self._recent: deque[tuple[int, str, TradeSide, float]] = deque()
        self.twap_wallets: dict[str, int] = {}          # wallet → last slice time
        self.forced: dict[tuple[int, str], float] = {}  # (time, wallet) → USD
        self._last_prune = 0

    def observe(self, o: AggressiveOrder) -> None:
        if not o.engine or not o.taker:
            return
        w, t = o.taker, o.timestamp
        hist = self._by_wallet.setdefault((w, o.side), deque())
        hist.append((t, o.notional))
        while hist and t - hist[0][0] > 15 * 60_000:
            hist.popleft()
        if w in self.twap_wallets or self._regular(hist):
            self.twap_wallets[w] = t
            for key in [k for k in self.forced if k[1] == w]:   # it was a TWAP all along
                del self.forced[key]
            return
        # one account's 20% partial liquidation, then the rest inside the cooldown
        if len(hist) >= 2:
            (t0, n0), (t1, n1) = hist[-2], hist[-1]
            if t1 - t0 <= PARTIAL_LIQ_MS and n0 > 0 and n1 >= PARTIAL_LIQ_RATIO * n0:
                self.forced[(t0, w)] = n0
                self.forced[(t1, w)] = n1
        # many accounts liquidated in the same instant
        self._recent.append((t, w, o.side, o.notional))
        while self._recent and t - self._recent[0][0] > CLUSTER_MS:
            self._recent.popleft()
        same = [(ts, wl, n) for ts, wl, sd, n in self._recent if sd is o.side and wl not in self.twap_wallets]
        if len({wl for _, wl, _ in same}) >= CLUSTER_WALLETS:
            for ts, wl, n in same:
                self.forced[(ts, wl)] = n
        self._prune(t)

    @staticmethod
    def _regular(hist: deque[tuple[int, float]]) -> bool:
        if len(hist) < TWAP_MIN_ORDERS:
            return False
        times = [t for t, _ in hist][-6:]
        gaps = [b - a for a, b in zip(times, times[1:])]
        g = sorted(gaps)[len(gaps) // 2]
        if g < TWAP_MIN_GAP_MS:
            return False
        tol = max(3_000, TWAP_GAP_TOLERANCE * g)
        return all(abs(x - g) <= tol for x in gaps)

    def _prune(self, now: int) -> None:
        if now - self._last_prune < 60_000:
            return
        self._last_prune = now
        self.twap_wallets = {w: t for w, t in self.twap_wallets.items() if now - t <= KEEP_MS}
        self.forced = {k: v for k, v in self.forced.items() if now - k[0] <= KEEP_MS}
        self._by_wallet = {k: d for k, d in self._by_wallet.items() if d and now - d[-1][0] <= 15 * 60_000}

    def view(self) -> tuple[frozenset[str], frozenset[tuple[int, str]]]:
        """Snapshots for one engine tick: TWAP wallets, and the (time, wallet) keys of forced orders."""
        return frozenset(self.twap_wallets), frozenset(self.forced)


def is_twap(o: AggressiveOrder, twap_wallets: frozenset[str] | dict) -> bool:
    return o.engine and o.taker in twap_wallets


def is_forced(o: AggressiveOrder, forced: frozenset[tuple[int, str]] | dict) -> bool:
    return o.engine and (o.timestamp, o.taker) in forced

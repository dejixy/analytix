"""
Engine-executed fills: telling TWAP slices from liquidations.

Fills the matching engine generates itself carry an all-zero transaction hash.
On Hyperliquid those are TWAP slices, liquidations and auto-deleveraging. They
behave very differently, and the rules that create them make them separable:

  TWAP          Suborders at a fixed interval of at least 30 seconds, each about the
                same size (±20% if randomised; up to 3× to catch up after a miss), for
                5 minutes to 7 days. → one wallet, one side, a regular cadence, similar sizes.
  Liquidation   A market order for the whole position — or 20% of it for positions over
                $100K, then the rest within a 30-second cooldown. In a sell-off many
                accounts hit maintenance margin in the same few seconds.

So an engine order is:
  twap        its (wallet, side) has ≥ 3 engine orders at a regular interval ≥ 25s and of
              similar size — and this order is slice-sized (≤ 4.5× the usual slice: a +20%
              randomised slice tripled by catch-up is 3.6×), so a TWAP wallet that later
              gets liquidated is still caught
  forced      after a 90-second warm-up (so running TWAPs are recognised first), ≥ 3
              distinct wallets with no recent engine history hit the same side within 3s;
              or one wallet's ≥ $20K chunk followed within 31s by ≥ 3.5× more (a 20%
              partial liquidation, then the remaining ~80%) — undone if that wallet keeps
              producing engine orders (4 inside 3 minutes is recurring flow, not a liquidation)
  otherwise   unclassified — a lone engine fill could be a small liquidation or a TWAP's
              first slice, and we don't guess

Classifications live in maps keyed by (wallet, side) and (time, wallet), so orders already
in the buffers are reclassified as soon as a pattern appears.
"""
from collections import deque

from models.orderModel import AggressiveOrder
from models.tradeModel import TradeSide

TWAP_MIN_ORDERS = 3
TWAP_MIN_GAP_MS = 25_000
TWAP_GAP_TOLERANCE = 0.2          # every recent gap within ±20% (and ±3s) of the median: a fixed interval
TWAP_SIZE_BAND = 0.35             # most slices within ±35% of the median slice (randomise is ±20%)
TWAP_MAX_SLICE = 4.5              # a +20% randomised slice tripled by catch-up is 3.6×; anything bigger isn't a slice
CLUSTER_MS = 3_000
CLUSTER_WALLETS = 3
WARMUP_MS = 90_000
RECURRING_MS = 120_000            # a wallet with an engine order this recently on the same side may be a TWAP
PARTIAL_LIQ_MS = 31_000
PARTIAL_LIQ_MIN_USD = 20_000      # 20% of a position over $100K
PARTIAL_LIQ_RATIO = 3.5           # then the remaining ~80% (4×)
RECURRING_PAIR = 4                # a 4th engine order inside 3 minutes: recurring flow (e.g. two TWAPs), not a liquidation
RECURRING_PAIR_MS = 180_000
HISTORY_MS = 15 * 60_000
KEEP_MS = 3_600_000

Key = tuple[str, TradeSide]


class EngineFlow:
    def __init__(self):
        self._hist: dict[Key, deque[tuple[int, float]]] = {}
        self._recent: deque[tuple[int, str, TradeSide, float, bool]] = deque()
        self.twaps: dict[Key, tuple[int, float, int]] = {}   # (wallet, side) → (last slice ms, slice USD, interval ms)
        self.forced: dict[tuple[int, str], float] = {}     # (time, wallet) → USD
        self._first: int | None = None
        self._cluster_keys: set[tuple[int, str]] = set()   # forced by the many-accounts rule (not undone by recurrence)
        self._last_prune = 0

    def observe(self, o: AggressiveOrder) -> None:
        if not o.engine or not o.taker:
            return
        w, t, side, usd = o.taker, o.timestamp, o.side, o.notional
        if self._first is None:
            self._first = t
        key = (w, side)
        hist = self._hist.setdefault(key, deque())
        recurring = bool(hist) and t - hist[-1][0] <= RECURRING_MS
        hist.append((t, usd))
        while hist and t - hist[0][0] > HISTORY_MS:
            hist.popleft()

        known = self.twaps.get(key)
        if known and usd <= TWAP_MAX_SLICE * known[1]:
            self.twaps[key] = (t, known[1], known[2])     # another slice of a known TWAP
            return
        if not known:
            found = self._twap_slice(hist)
            if found is not None:
                self.twaps[key] = (t, *found)
                times = {ts for ts, _ in hist}
                for k in [k for k in self.forced if k[1] == w and k[0] in times]:   # it was a TWAP all along
                    del self.forced[k]
                return

        # One account's 20% partial liquidation, then the rest inside the 30s cooldown. Not for a known TWAP
        # (its previous order is a slice), and undone if the wallet keeps going — liquidations don't recur.
        recent_n = sum(1 for ts, _ in hist if t - ts <= RECURRING_PAIR_MS)
        if recent_n >= RECURRING_PAIR:
            for k in [k for k in self.forced if k[1] == w and t - k[0] <= RECURRING_PAIR_MS
                      and k not in self._cluster_keys]:
                del self.forced[k]
        elif not known and len(hist) >= 2:
            (t0, n0), (t1, n1) = hist[-2], hist[-1]
            if t1 - t0 <= PARTIAL_LIQ_MS and n0 >= PARTIAL_LIQ_MIN_USD and n1 >= PARTIAL_LIQ_RATIO * n0:
                self.forced[(t0, w)] = n0
                self.forced[(t1, w)] = n1

        # Many accounts closed in the same instant — once running TWAPs have had time to show themselves.
        self._recent.append((t, w, side, usd, recurring))
        while self._recent and t - self._recent[0][0] > CLUSTER_MS:
            self._recent.popleft()
        if t - self._first >= WARMUP_MS:
            fresh = [(ts, wl, n) for ts, wl, sd, n, rec in self._recent
                     if sd is side and not rec and (wl, sd) not in self.twaps]
            if len({wl for _, wl, _ in fresh}) >= CLUSTER_WALLETS:
                for ts, wl, n in fresh:
                    self.forced[(ts, wl)] = n
                    self._cluster_keys.add((ts, wl))
        self._prune(t)

    @staticmethod
    def _twap_slice(hist: deque[tuple[int, float]]) -> tuple[float, int] | None:
        """(typical slice USD, interval ms) if these orders look like a TWAP (fixed interval, similar sizes)."""
        if len(hist) < TWAP_MIN_ORDERS:
            return None
        recent = list(hist)[-6:]
        times = [t for t, _ in recent]
        gaps = [b - a for a, b in zip(times, times[1:])]
        g = sorted(gaps)[len(gaps) // 2]
        if g < TWAP_MIN_GAP_MS or any(abs(x - g) > max(3_000, TWAP_GAP_TOLERANCE * g) for x in gaps):
            return None
        sizes = sorted(n for _, n in recent)
        m = sizes[len(sizes) // 2]
        if m <= 0 or sizes[-1] > TWAP_MAX_SLICE * m:
            return None
        similar = sum(1 for n in sizes if abs(n - m) <= TWAP_SIZE_BAND * m)
        return (m, g) if similar * 3 >= len(sizes) * 2 else None

    def _prune(self, now: int) -> None:
        if now - self._last_prune < 60_000:
            return
        self._last_prune = now
        self.twaps = {k: v for k, v in self.twaps.items() if now - v[0] <= KEEP_MS}
        self.forced = {k: v for k, v in self.forced.items() if now - k[0] <= KEEP_MS}
        self._cluster_keys = {k for k in self._cluster_keys if now - k[0] <= KEEP_MS}
        self._hist = {k: d for k, d in self._hist.items() if d and now - d[-1][0] <= HISTORY_MS}

    def view(self) -> tuple[dict[Key, tuple[int, float, int]], frozenset[tuple[int, str]]]:
        """Snapshots for one engine tick: running TWAPs, and the (time, wallet) keys of forced orders."""
        return dict(self.twaps), frozenset(self.forced)


def is_forced(o: AggressiveOrder, forced: frozenset[tuple[int, str]] | dict) -> bool:
    return o.engine and (o.timestamp, o.taker) in forced

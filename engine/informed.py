"""
Informed flow, models 1 and 2: find hidden orders, and score how informed each wallet is at slow horizons.

    hidden order   one wallet keeps taking one side of one coin in pieces no more than GAP_MS apart (a piece = its
                   fills within PIECE_MS, one market order sweeping levels). It counts once it has done MIN_USD in
                   MIN_PIECES+ pieces over MIN_SPAN_MS+: that moment is when the bot could know (t_detect). An opposite
                   trade or a long pause ends it. A native TWAP is one kind (a steady 30s beat); most aren't
    informedness   after each hidden order is spotted: how far price went its way over 5 to 60 minutes. Informed
                   orders move price for good (the move holds or grows); pressure from uninformed orders fades.
                   A wallet's score is its average, shrunk toward zero by how few orders it has:
                   score = n × mean / (n + k), with k from the data (spread of single orders / spread between wallets)
"""
from dataclasses import dataclass, field

import numpy as np

GAP_MS = 120_000
PIECE_MS = 2_000
MIN_USD = 50_000
MIN_PIECES = 4
MIN_SPAN_MS = 60_000


@dataclass(slots=True)
class Hidden:
    wallet: str
    coin: str
    side: int
    t_first: int
    t_last: int
    usd: float
    pieces: int = 1
    last_piece: int = 0
    gaps: list = field(default_factory=list)
    t_detect: int = -1
    usd_detect: float = 0.0
    pieces_detect: int = 0
    regular_detect: bool = False


def regular(gaps: list) -> bool:
    """A native TWAP's beat: pieces 25-35 seconds apart."""
    return len(gaps) >= 2 and all(25_000 <= g <= 35_000 for g in gaps)


class HiddenOrders:
    def __init__(self, min_usd: float = MIN_USD, min_pieces: int = MIN_PIECES, min_span_ms: int = MIN_SPAN_MS,
                 gap_ms: int = GAP_MS, keep: bool = True):
        self.min_usd, self.min_pieces, self.min_span_ms, self.gap_ms = min_usd, min_pieces, min_span_ms, gap_ms
        self.keep = keep                              # False (live): don't keep finished orders, just report spots
        self.open: dict[tuple, Hidden] = {}
        self.done: list[Hidden] = []

    def add(self, wallet: str, coin: str, side: int, t_ms: int, usd: float) -> Hidden | None:
        """One taker fill. Returns the hidden order when this fill is the one that gets it spotted."""
        key = (wallet, coin)
        h = self.open.get(key)
        if h is not None and (h.side != side or t_ms - h.t_last > self.gap_ms):
            self._close(h)
            h = None
        if h is None:
            h = self.open[key] = Hidden(wallet, coin, side, t_ms, t_ms, usd, 1, t_ms)
        else:
            if t_ms - h.last_piece > PIECE_MS:
                h.pieces += 1
                h.gaps.append(t_ms - h.last_piece)
                h.last_piece = t_ms
            h.usd += usd
            h.t_last = t_ms
        if (h.t_detect < 0 and h.usd >= self.min_usd and h.pieces >= self.min_pieces
                and t_ms - h.t_first >= self.min_span_ms):
            h.t_detect, h.usd_detect, h.pieces_detect = t_ms, h.usd, h.pieces
            h.regular_detect = regular(h.gaps)
            return h
        return None

    def prune(self, now_ms: int) -> None:
        """Forget orders that are over (no fill for gap_ms), so a live run doesn't grow forever."""
        for key in [k for k, h in self.open.items() if now_ms - h.t_last > self.gap_ms]:
            self._close(self.open.pop(key))

    def _close(self, h: Hidden) -> None:
        if h.t_detect >= 0 and self.keep:
            self.done.append(h)

    def finish(self) -> list[Hidden]:
        for h in self.open.values():
            self._close(h)
        self.open.clear()
        self.done.sort(key=lambda h: h.t_detect)
        return self.done


def shrink_k(wallet: np.ndarray, v: np.ndarray) -> float:
    """k for score = n × mean / (n + k): within-wallet variance over between-wallet variance (method of moments).
    Big k = wallets look alike, so a wallet needs many orders before its own average counts."""
    ok = np.isfinite(v)
    w, v = wallet[ok], v[ok]
    if len(v) < 20:
        return 50.0
    ids, inv = np.unique(w, return_inverse=True)
    n = np.bincount(inv).astype(float)
    mean = np.bincount(inv, weights=v) / n
    within = float(np.var(v))
    many = n >= 3
    if many.sum() < 5:
        return 50.0
    between = float(np.var(mean[many]) - np.mean(within / n[many]))
    if between <= 0:
        return 200.0
    return float(np.clip(within / between, 1.0, 200.0))


def scores(wallet: np.ndarray, v: np.ndarray, ready_ms: np.ndarray, until_ms: int, n_wallets: int, k: float) -> dict:
    """Per wallet: n, raw mean and shrunk score from the values fully known before until_ms."""
    use = np.isfinite(v) & (ready_ms <= until_ms)
    w, x = wallet[use], v[use]
    n = np.bincount(w, minlength=n_wallets).astype(float)
    s = np.bincount(w, weights=x, minlength=n_wallets)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = np.where(n > 0, s / n, 0.0)
    return {"n": n, "mean": mean, "score": n * mean / (n + k)}


def rank_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 5:
        return None
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def follow(g, t_ms: np.ndarray, side: np.ndarray, lag: int, hold: int, lev: float, coin: str, with_adv: bool = False):
    """Go the order's way `lag` seconds after t_ms, out `hold` seconds later, on one coin's one-second grid.
    → (move at the mid in bps, Papertrade result at `lev` as a share of margin), nan where the feed had a gap;
    with_adv adds the furthest the best bid/ask went against it (bps). Works order by order, so long holds don't
    need a window over every second."""
    from engine.walletstudy import kept
    n = len(g)
    mid = np.full(len(t_ms), np.nan)
    pt = np.full(len(t_ms), np.nan)
    adv = np.full(len(t_ms), np.nan)
    if not n:
        return (mid, pt, adv) if with_adv else (mid, pt)
    t0 = int(g.t[0])
    bad = np.concatenate([[0], np.cumsum(~g.ok)])
    e = t_ms // 1000 - t0 + lag
    x = e + hold
    ok = (e >= 0) & (x < n)
    ok[ok] &= (bad[x[ok] + 1] - bad[e[ok]]) == 0
    for i in np.nonzero(ok)[0]:
        a, b, s = e[i], x[i], side[i]
        move = s * (g.mid[b] / g.mid[a] - 1)
        worst = (g.mid[a] - g.bid[a + 1:b + 1].min()) / g.mid[a] if s > 0 else (g.ask[a + 1:b + 1].max() - g.mid[a]) / g.mid[a]
        mid[i] = move * 1e4
        adv[i] = max(worst, 0.0) * 1e4
        if worst >= 1 / lev:
            pt[i] = -1.0
        else:
            pt[i] = float(kept(np.array([move]), coin)[0] * lev) if move > 0 else max(move * lev, -1.0)
    return (mid, pt, adv) if with_adv else (mid, pt)

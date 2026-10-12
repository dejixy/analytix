"""
Hold clock service: keeps a one-second grid of each main coin's best bid, best ask and mid (24 hours, saved in
the bars SQLite file so a restart doesn't empty it), and answers "how long can I hold this?" for very high
leverage from it. The maths lives in engine/holdclock.py.

    every second   the finished seconds' lowest best bid, highest best ask and last mid, per coin, from the books
                   the dashboard already receives (no extra feed)
    every minute   new rows saved; anything older than 24 hours dropped
    on request     survival odds, fill drift, the target-vs-bust race and a live read of the tape, computed in a
                   worker thread (numpy) and cached for a few seconds
"""
import asyncio
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path

import numpy as np

from engine.holdclock import (
    fill_drift, make_grid, now_read, papertrade_net, race, survival, time_at,
)

log = logging.getLogger("analytix.holdclock")

KEEP_S = 24 * 3600
STALE_S = 5                       # no book update for this long and the grid marks the second as a gap
CACHE_S = 3.0
GRID_CACHE_S = 10.0
LIVE_HORIZONS = (10, 30, 60, 120, 300)


class HoldClock:
    def __init__(self, runtime, db_path: Path | None = None):
        self.rt = runtime
        self.db_path = db_path
        self.rows: dict[str, deque] = {}
        self._done: dict[str, int] = {}               # coin → last finished second written
        self._last_update: dict[str, int] = {}        # coin → second of the last real book update
        self._unsaved: dict[str, list] = {}
        self._grid: dict[str, tuple[float, object]] = {}
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._db: sqlite3.Connection | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live"

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.enabled:
            return
        if self.db_path:
            try:
                self._db = sqlite3.connect(self.db_path, check_same_thread=False)
                self._db.execute("CREATE TABLE IF NOT EXISTS hold_grid (coin TEXT NOT NULL, t INTEGER NOT NULL, "
                                 "bid REAL, ask REAL, mid REAL, ok INTEGER, PRIMARY KEY (coin, t))")
                since = int(time.time()) - KEEP_S
                self._db.execute("DELETE FROM hold_grid WHERE t < ?", (since,))
                self._db.commit()
                for coin, t, b, a, m, ok in self._db.execute(
                        "SELECT coin, t, bid, ask, mid, ok FROM hold_grid WHERE t >= ? ORDER BY coin, t", (since,)):
                    self.rows.setdefault(coin, deque()).append((t, b, a, m, bool(ok)))
                for coin, q in self.rows.items():
                    if q:
                        self._done[coin] = q[-1][0]
                log.info("hold clock: %s", ", ".join(f"{c} {len(q) / 3600:.1f}h" for c, q in self.rows.items()) or "starting empty")
            except Exception:
                log.exception("could not open the hold-clock history; it starts empty")
                self._db = None
        self._tasks = [asyncio.create_task(self._sample_loop(), name="hold-sample"),
                       asyncio.create_task(self._save_loop(), name="hold-save")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.save()
        if self._db:
            self._db.close()

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self.sample()
            except Exception:
                log.exception("hold clock sampling failed")

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.save()

    # ── the grid ───────────────────────────────────────────────────────────
    def sample(self) -> None:
        for coin in self.rt.coins:
            pipe = self.rt.pipelines.get(coin)
            if pipe is None or not pipe.state.now_ms:
                continue
            now_s = pipe.state.now_ms // 1000
            last = self._done.get(coin)
            if last is None:
                last = now_s - 2
            if now_s - 1 <= last:
                continue
            per: dict[int, list] = {}
            for b in pipe.state.books.window(min(120, now_s - last + 2)):
                s = b.timestamp // 1000
                if last < s < now_s:
                    p = per.get(s)
                    if p is None:
                        per[s] = [b.best_bid, b.best_ask, b.mid]
                    else:
                        p[0], p[1], p[2] = min(p[0], b.best_bid), max(p[1], b.best_ask), b.mid
            q = self.rows.setdefault(coin, deque())
            prev = q[-1] if q else None
            new = []
            for s in range(max(last + 1, now_s - 120), now_s):
                if s in per:
                    bid, ask, mid = per[s]
                    self._last_update[coin] = s
                    row = (s, bid, ask, mid, True)
                elif prev is not None:
                    fresh = s - self._last_update.get(coin, -10**9) <= STALE_S
                    row = (s, prev[1], prev[2], prev[3], fresh)          # the book didn't change
                else:
                    continue
                new.append(row)
                prev = row
            q.extend(new)
            self._unsaved.setdefault(coin, []).extend(new)
            self._done[coin] = now_s - 1
            while q and q[0][0] < now_s - KEEP_S:
                q.popleft()

    def save(self) -> None:
        if not self._db:
            self._unsaved.clear()
            return
        try:
            for coin, rows in self._unsaved.items():
                self._db.executemany("INSERT OR REPLACE INTO hold_grid (coin, t, bid, ask, mid, ok) VALUES (?, ?, ?, ?, ?, ?)",
                                     [(coin, t, b, a, m, int(ok)) for t, b, a, m, ok in rows])
            self._db.execute("DELETE FROM hold_grid WHERE t < ?", (int(time.time()) - KEEP_S,))
            self._db.commit()
        except sqlite3.Error:
            log.exception("could not save the hold-clock grid")
        self._unsaved.clear()

    def grid(self, coin: str):
        hit = self._grid.get(coin)
        if hit and time.time() - hit[0] < GRID_CACHE_S:
            return hit[1]
        g = make_grid(list(self.rows.get(coin) or ()))
        self._grid[coin] = (time.time(), g)
        return g

    # ── the read ───────────────────────────────────────────────────────────
    async def read(self, coin: str, side: int, leverage: float, target_pct: float | None = None,
                   entry: float | None = None, bust: float | None = None, buffer_bps: float = 0.0) -> dict:
        key = (coin, side, round(leverage, 1), target_pct, entry, bust, buffer_bps)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        live = self.live(coin, side)
        g = self.grid(coin)
        out = await asyncio.to_thread(self._compute, g, side, leverage, target_pct, entry, bust, buffer_bps, live)
        out["live"] = live
        out["coin"], out["side"], out["leverage"] = coin, side, leverage
        self._cache[key] = (time.time(), out)
        if len(self._cache) > 200:
            self._cache.clear()
        return out

    @staticmethod
    def _compute(g, side, leverage, target_pct, entry, bust, buffer_bps, live) -> dict:
        dist = abs(entry - bust) / entry if entry and bust else max(1e-6, 1 / leverage - buffer_bps / 1e4)
        mid = live.get("mid")
        curve = survival(g, side, dist)
        now_curve = survival(g, side, dist, like_now=True)
        drift = fill_drift(g)
        out = {
            "history_s": int(len(g)), "dist_bps": dist * 1e4,
            "bust_price": (entry or mid) * (1 - side * dist) if (entry or mid) else None,
            "survival": curve, "survival_like_now": now_curve,
            "safe_90_s": time_at(now_curve if any(c["survive"] is not None for c in now_curve) else curve, 0.9),
            "half_gone_s": time_at(now_curve if any(c["survive"] is not None for c in now_curve) else curve, 0.5),
            "drift": {k: {**v, "margin_pct_p50": v["p50"] * leverage / 100, "margin_pct_p90": v["p90"] * leverage / 100,
                          "margin_pct_p99": v["p99"] * leverage / 100} for k, v in drift.items()},
            "now": now_read(g),
        }
        if target_pct:
            tgt = target_pct / 100 / leverage
            r = race(g, side, dist, tgt)
            if r:
                net = papertrade_net(target_pct / 100, 0.2, leverage)
                r["target_bps"] = tgt * 1e4
                r["net_win_pct"] = net * 100
                r["ev_pct"] = (r["target_first"] * net - r["bust_first"]) * 100     # "neither" counted as flat
            out["race"] = r
        if entry and mid:
            now_dist = (mid - (entry * (1 - side * dist))) / mid * side if bust is None else \
                (mid - bust) / mid * side
            out["position"] = {"pnl_pct": side * (mid - entry) / entry * leverage * 100, "dist_now_bps": now_dist * 1e4,
                               "survival": survival(g, side, max(now_dist, 1e-6), LIVE_HORIZONS, like_now=True)
                               if now_dist > 0 else [], "busted": now_dist <= 0}
        return out

    def live(self, coin: str, side: int) -> dict:
        """The tape right now: spread against the last hour's usual, which way the book leans, and whether Binance has
        just moved and Hyperliquid hasn't, each turned into a plain warning when it's against the position."""
        pipe = self.rt.pipelines.get(coin)
        if pipe is None:
            return {}
        st = pipe.state
        books = st.books.window(3600)
        if not books:
            return {}
        last = books[-1]
        spreads = np.array([b.spread_bps for b in books[-3000:]])
        usual = float(np.median(spreads)) if len(spreads) else None
        imb = float(np.mean([b.imbalance for b in books[-10:]]))
        out = {"mid": last.mid, "bid": last.best_bid, "ask": last.best_ask, "spread_bps": last.spread_bps,
               "usual_spread_bps": usual, "imbalance": imb, "binance_bps": None, "hl_bps": None, "warnings": []}
        ext = st.external.get("binance")
        if ext and len(ext):
            q = list(ext.window(5))
            if len(q) >= 2:
                b0, b1 = (q[0].bid + q[0].ask) / 2, (q[-1].bid + q[-1].ask) / 2
                out["binance_bps"] = (b1 / b0 - 1) * 1e4
                h0 = st.books.latest_at(q[0].timestamp) if hasattr(st.books, "latest_at") else None
                if h0:
                    out["hl_bps"] = (last.mid / h0.mid - 1) * 1e4
        w = out["warnings"]
        if usual and last.spread_bps > 2.5 * usual and last.spread_bps > 1:
            w.append(f"The spread is {last.spread_bps:.1f} bps, {last.spread_bps / usual:.1f}× usual: the book is thin right now.")
        if imb * side <= -0.4:
            w.append("The book leans against you: more resting size on the side that pushes price your bust's way.")
        if out["binance_bps"] is not None and out["hl_bps"] is not None:
            gap = out["binance_bps"] - out["hl_bps"]
            if gap * side <= -1.0:
                w.append(f"Binance just moved {out['binance_bps']:+.1f} bps and Hyperliquid only {out['hl_bps']:+.1f}: "
                         "Hyperliquid often follows.")
        return out


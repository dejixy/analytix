"""
Liquidation map collector (edge idea 6): every Hyperliquid position is public, so the prices where the biggest
traders get liquidated can be mapped. Clusters of liquidation prices may pull price toward them and speed it up
through them. That can't be tested from the recordings (they hold trades, not positions), so this collects the
history now, for a study in a week or two.

    who           every wallet on either side of a trade on the watched coins, weighted by how much it trades
                  (half-life 12 hours); the top TOP_N are checked
    how often     one round every ROUND_S seconds, spread out (well inside Hyperliquid's rate limits), live mode only
    what          each position on the watched coins: size (signed), entry, liquidation price, value, leverage, and
                  the account's value; saved to data/positions.sqlite (kept KEEP_DAYS days)
"""
import asyncio
import logging
import math
import sqlite3
import time
from pathlib import Path

import httpx

from config import HL_INFO_URL
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.liqmap")

TOP_N = 300
ROUND_S = 600
HALF_LIFE_S = 12 * 3600
CONCURRENCY = 3
KEEP_DAYS = 30
MIN_WALLET_USD = 50_000               # a wallet needs this much recent trading to be worth checking


class LiqMapCollector:
    def __init__(self, runtime, db_path: Path | None, url: str = HL_INFO_URL, top_n: int = TOP_N,
                 round_s: float = ROUND_S):
        self.rt = runtime
        self.db_path = db_path
        self.url = url
        self.top_n = top_n
        self.round_s = round_s
        self.weight: dict[str, list[float]] = {}         # wallet → [decayed $ traded, as of (s)]
        self.rounds = 0
        self.last = {"t_ms": 0, "wallets": 0, "positions": 0, "failed": 0}
        self._db: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live" and self.db_path is not None

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            self._db = connect(self.db_path)
            self._db.execute("CREATE TABLE IF NOT EXISTS positions (t_ms INTEGER NOT NULL, wallet TEXT NOT NULL, "
                             "coin TEXT NOT NULL, szi REAL, entry_px REAL, liq_px REAL, value_usd REAL, leverage REAL, "
                             "account_usd REAL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS positions_t ON positions (t_ms, coin)")
            self._db.commit()
        except Exception:
            log.exception("could not open the positions history; the liquidation map won't be collected")
            self._db = None
            return
        self._task = asyncio.create_task(self._loop(), name="liqmap")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._db:
            self._db.close()

    # ── who to check ───────────────────────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        if msg.get("channel") != "trades":
            return
        now = time.time()
        for tr in msg.get("data") or []:
            users = tr.get("users") or []
            try:
                usd = float(tr["px"]) * float(tr["sz"])
            except (KeyError, TypeError, ValueError):
                continue
            for w in users:
                self._add(w.lower(), usd, now)

    def _add(self, wallet: str, usd: float, now: float) -> None:
        cur = self.weight.get(wallet)
        if cur is None:
            self.weight[wallet] = [usd, now]
            return
        cur[0] = cur[0] * math.exp(-math.log(2) * (now - cur[1]) / HALF_LIFE_S) + usd
        cur[1] = now

    def top(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        k = math.log(2) / HALF_LIFE_S
        scored = [(v * math.exp(-k * (now - t)), w) for w, (v, t) in self.weight.items()]
        scored = [x for x in scored if x[0] >= MIN_WALLET_USD]
        scored.sort(reverse=True)
        if len(self.weight) > 50 * self.top_n:          # forget the long tail now and then
            keep = {w for _, w in scored[: 10 * self.top_n]}
            self.weight = {w: v for w, v in self.weight.items() if w in keep}
        return [w for _, w in scored[: self.top_n]]

    # ── the rounds ─────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        await asyncio.sleep(120)                          # let the trade feed name some wallets first
        while True:
            t = time.time()
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("liquidation map round failed")
            await asyncio.sleep(max(30.0, self.round_s - (time.time() - t)))

    async def round(self) -> None:
        wallets = self.top()
        if not wallets:
            return
        coins = set(self.rt.coins)
        t_ms = int(time.time() * 1000)
        rows, failed = [], 0
        sem = asyncio.Semaphore(CONCURRENCY)
        gap = max(0.2, (self.round_s * 0.5) / len(wallets))          # spread the requests over half the round

        async with httpx.AsyncClient(timeout=15) as client:
            async def one(i: int, w: str):
                nonlocal failed
                await asyncio.sleep(i * gap)
                async with sem:
                    try:
                        r = await client.post(self.url, json={"type": "clearinghouseState", "user": w})
                        if r.status_code == 429:
                            await asyncio.sleep(5)
                            r = await client.post(self.url, json={"type": "clearinghouseState", "user": w})
                        r.raise_for_status()
                        rows.extend(parse_state(r.json(), w, t_ms, coins))
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        failed += 1

            await asyncio.gather(*(one(i, w) for i, w in enumerate(wallets)))
        self._save(rows, t_ms)
        self.rounds += 1
        self.last = {"t_ms": t_ms, "wallets": len(wallets), "positions": len(rows), "failed": failed}
        log.info("liquidation map: %d positions from %d wallets (%d failed)", len(rows), len(wallets), failed)

    def _save(self, rows: list[tuple], t_ms: int) -> None:
        if not self._db:
            return
        try:
            with transaction(self._db):
                self._db.executemany("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                if self.rounds % 144 == 0:                # about once a day
                    self._db.execute("DELETE FROM positions WHERE t_ms < ?", (t_ms - KEEP_DAYS * 86_400_000,))
        except sqlite3.Error as exc:
            log.warning("could not save positions: %s", exc)


def parse_state(state: dict, wallet: str, t_ms: int, coins: set[str]) -> list[tuple]:
    """One clearinghouseState → rows for the positions on the watched coins."""
    account = _f((state.get("marginSummary") or {}).get("accountValue"))
    out = []
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        coin = p.get("coin")
        szi = _f(p.get("szi"))
        if coin not in coins or not szi:
            continue
        lev = p.get("leverage") or {}
        out.append((t_ms, wallet, coin, szi, _f(p.get("entryPx")), _f(p.get("liquidationPx")), _f(p.get("positionValue")),
                    _f(lev.get("value") if isinstance(lev, dict) else lev), account))
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

"""
The Analyzer (/analyzer, /api/analyzer): where crypto's money is being made right now. It keeps the money map
(fees by category and chain, from DefiLlama) and the profit boards (Hyperliquid perps, Solana traders, Polymarket,
and any Dune queries listed in data/analyzer_dune.json) fresh in the background, live mode only.

Each source refreshes on its own clock and reports its own state (ok, when, or why not), so the page can say what
it's missing: a source without its key says which key to set.

data/analyzer_dune.json: [{"id": 1234567, "title": "NFT traders, 7 days", "category": "nfts", "chain": "Ethereum"}]
(any saved Dune query with a wallet column and a profit column in dollars).
"""
import asyncio
import json
import logging
import time

from engine import moneymap as M
from ingestion import moneymap as S

log = logging.getLogger("analytix.analyzer")

EVERY_S = {"fees": 900, "hyperliquid": 1800, "solana": 1800, "polymarket": 1800, "dune": 3600}
SOURCE_NAME = {"fees": "DefiLlama fees", "hyperliquid": "Hyperliquid leaderboard", "solana": "Birdeye (Solana traders)",
               "polymarket": "Polymarket leaderboard", "dune": "Dune queries"}
HL_WINDOWS = ("day", "week", "month")


class AnalyzerService:
    def __init__(self, runtime, dune_file=None):
        self.rt = runtime
        self.dune_file = dune_file
        self.map: dict | None = None
        self.hl: dict | None = None
        self.boards: dict[str, dict] = {}
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in EVERY_S}
        self._tasks: list[asyncio.Task] = []
        self.fetch = {"fees": S.fetch_llama, "hyperliquid": S.fetch_hl_leaderboard, "solana": S.fetch_birdeye,
                      "polymarket": S.fetch_polymarket, "dune": S.fetch_dune}     # swappable in tests

    async def start(self) -> None:
        if self.rt.mode == "live":
            self._tasks = [asyncio.create_task(self._loop(k), name=f"analyzer-{k}") for k in EVERY_S]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, key: str) -> None:
        await asyncio.sleep(5 + 7 * list(EVERY_S).index(key))
        while True:
            await self.refresh(key)
            await asyncio.sleep(EVERY_S[key])

    async def refresh(self, key: str) -> None:
        try:
            await getattr(self, f"_{key}")()
            self.status[key] = {"ok": True, "at": int(time.time() * 1000), "error": None}
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - one source down must not stop the others
            setup = "no key" in str(exc) or "no queries" in str(exc)
            self.status[key] = {"ok": False, "at": self.status[key]["at"], "setup": setup,
                                "error": (str(exc) if setup else f"{type(exc).__name__}: {exc}")[:200]}
            if "no key" not in str(exc) and "no queries" not in str(exc):
                log.warning("analyzer %s: %s", key, exc)

    async def _fees(self) -> None:
        protocols = await self.fetch["fees"]()
        if not protocols:
            raise RuntimeError("no protocols in the reply")
        self.map = M.money_map(protocols)

    async def _hyperliquid(self) -> None:
        rows = await self.fetch["hyperliquid"]()
        if not rows:
            raise RuntimeError("no rows in the reply")
        self.hl = {"n": len(rows), "windows": {w: {"crowd": M.crowd(rows, w), "top": M.top_traders(rows, w)}
                                               for w in HL_WINDOWS}}

    async def _solana(self) -> None:
        rows = await self.fetch["solana"]("1W")
        self.boards["solana"] = {"title": "Solana traders, 7 days", "category": "memes", "chain": "Solana",
                                 "source": "Birdeye", **M.board(rows)}

    async def _polymarket(self) -> None:
        rows = await self.fetch["polymarket"]("7d")
        self.boards["polymarket"] = {"title": "Polymarket, 7 days", "category": "prediction", "chain": "Polygon",
                                     "source": "Polymarket", **M.board(rows)}

    async def _dune(self) -> None:
        try:
            queries = json.loads(self.dune_file.read_text()) if self.dune_file and self.dune_file.exists() else []
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"can't read {self.dune_file}: {exc}") from exc
        if not queries:
            raise RuntimeError("no queries listed yet (data/analyzer_dune.json)")
        errors = []
        for q in queries:
            try:
                rows = await self.fetch["dune"](q["id"])
                self.boards[f"dune-{q['id']}"] = {"title": q.get("title") or f"Dune {q['id']}", "category": q.get("category", "other"),
                                                  "chain": q.get("chain", ""), "source": "Dune", **M.board(rows)}
            except Exception as exc:                      # noqa: BLE001
                errors.append(f"{q.get('id')}: {type(exc).__name__}: {exc}"[:120])
            await asyncio.sleep(1)
        if errors:
            raise RuntimeError("; ".join(errors))

    def public(self) -> dict:
        return {"now_ms": int(time.time() * 1000), "enabled": self.rt.mode == "live", "map": self.map, "hyperliquid": self.hl,
                "boards": list(self.boards.values()), "labels": M.LABEL,
                "status": {SOURCE_NAME[k]: v for k, v in self.status.items()}}

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
        self.map: dict | None = None            # the fees map (kept for older callers)
        self.maps: dict[str, dict] = {}         # fees, revenue, volume
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self.hl: dict | None = None
        self.boards: dict[str, dict] = {}
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in EVERY_S}
        self._tasks: list[asyncio.Task] = []
        self.fetch_pools, self.fetch_history = S.fetch_pools, S.fetch_history
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
        got = await self.fetch["fees"]()
        if not got.get("fees"):
            raise RuntimeError("no protocols in the reply")
        self.maps = {k: M.money_map(v) for k, v in got.items() if v}
        self.map = self.maps["fees"]

    async def _cached(self, key: tuple, ttl: float, make) -> dict:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await make()
        except Exception as exc:                          # noqa: BLE001 - shown on the page
            val = {"error": f"{type(exc).__name__}: {exc}"[:160]}
        self._cache[key] = (time.time(), val)
        if len(self._cache) > 300:
            self._cache.pop(next(iter(self._cache)))
        return val

    async def chain(self, name: str, metric: str = "fees") -> dict:
        """What's driving one chain: its biggest protocols by the chosen measure, and its busiest pools."""
        m = self.maps.get(metric) or self.maps.get("fees") or {}

        async def pools():
            network, rows = await self.fetch_pools(name)
            return {"network": network, "rows": rows}
        return {"chain": name, "metric": metric, "total": (m.get("chain_totals") or {}).get(name),
                "protocols": (m.get("on_chain") or {}).get(name, []),
                "pools": await self._cached(("pools", name), 300, pools)}

    async def protocol(self, slug: str, metric: str = "fees") -> dict:
        """One protocol's last 90 days of the chosen measure."""
        async def hist():
            return {"rows": await self.fetch_history(slug, metric)}
        return {"slug": slug, "metric": metric, **(await self._cached(("hist", slug, metric), 1800, hist))}

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
        return {"now_ms": int(time.time() * 1000), "enabled": self.rt.mode == "live", "map": self.map, "maps": self.maps,
                "hyperliquid": self.hl,
                "boards": list(self.boards.values()), "labels": M.LABEL,
                "status": {SOURCE_NAME[k]: v for k, v in self.status.items()}}

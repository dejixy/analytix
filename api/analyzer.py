"""
The Analyzer (/analyzer, /api/analyzer): where crypto's money is being made right now. It keeps the money map
(fees by category and chain, from DefiLlama) and the profit boards (Hyperliquid perps, Solana traders, Polymarket,
and any Dune queries listed in data/analyzer_dune.json) fresh in the background, live mode only.

Each source refreshes on its own clock and reports its own state (ok, when, or why not), so the page can say what
it's missing: a source without its key says which key to set.

data/analyzer_dune.json lists saved Dune queries of two kinds:
    a profit board   {"id": 1234567, "title": "NFT traders, 7 days", "category": "nfts", "chain": "Ethereum"}
                     (a wallet column and a profit column in dollars)
    an asset table   {"id": 2345678, "title": "TON collections selling now", "assets": true, "protocols": ["fragment"],
                      "chains": ["TON"], "refresh_h": 6, "explorer": "https://tonviewer.com/"}
                     (any columns; shown as they are under those protocols and chains; with refresh_h the query is
                     run again on Dune when its result is older than that, which spends Dune credits)

Assets in motion (/api/analyzer/assets): what is moving inside one protocol, by its kind: a DEX's or launchpad's
busiest pools (GeckoTerminal), a lending or yield protocol's biggest markets (DefiLlama yields), Hyperliquid's
busiest perps (the scanner), Polymarket's busiest markets, and any Dune asset table listed for it.
"""
import asyncio
import json
import logging
import time

from engine import moneymap as M
from ingestion import moneymap as S

log = logging.getLogger("analytix.analyzer")

EVERY_S = {"fees": 900, "capital": 3600, "hyperliquid": 1800, "solana": 1800, "polymarket": 1800, "dune": 3600}
SOURCE_NAME = {"fees": "DefiLlama fees", "capital": "DefiLlama stablecoins", "hyperliquid": "Hyperliquid leaderboard", "solana": "Birdeye (Solana traders)",
               "polymarket": "Polymarket leaderboard", "dune": "Dune queries"}
HL_WINDOWS = ("day", "week", "month")


class AnalyzerService:
    def __init__(self, runtime, dune_file=None, screen_log=None, heat_log=None):
        self.rt = runtime
        self.dune_file = dune_file
        self.screen_log = screen_log            # one line a day: what the screen said, to score it later
        self.heat_log = heat_log                # each hour's flagged tokens with their prices, to score the rule later
        self._heat_logged = 0.0
        self.fetch_trending = S.fetch_trending
        self.screen: dict | None = None
        self._last: dict[str, list] = {}
        self.stables: dict[str, dict] = {}
        self._logged_day = self._last_logged()
        self.map: dict | None = None            # the fees map (kept for older callers)
        self.maps: dict[str, dict] = {}         # fees, revenue, volume
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self.hl: dict | None = None
        self.boards: dict[str, dict] = {}
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in EVERY_S}
        self._tasks: list[asyncio.Task] = []
        self.fetch_pools, self.fetch_history = S.fetch_pools, S.fetch_history
        self.fetch_protocols, self.fetch_stables = S.fetch_protocols, S.fetch_stables
        self.fetch_dexes, self.fetch_dex_pools, self.fetch_yields = S.fetch_dexes, S.fetch_dex_pools, S.fetch_yields
        self.fetch_poly_markets, self.fetch_dune_table, self.run_dune = S.fetch_poly_markets, S.fetch_dune_table, S.run_dune
        self.dune_tables: dict[str, dict] = {}       # asset tables from Dune, by query id
        self._dune_ran: dict[str, float] = {}
        self.fetch = {"fees": S.fetch_llama, "capital": None, "hyperliquid": S.fetch_hl_leaderboard, "solana": S.fetch_birdeye,
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
        for k, rows in self._last.items():                # a measure that came back empty keeps its last good read
            if not got.get(k):
                got[k] = rows
        self._last = {k: v for k, v in got.items() if v}
        self.maps = {k: M.money_map(got[k]) for k in S.METRICS if got.get(k)}
        self.map = self.maps["fees"]
        try:
            meta = await self.fetch_protocols()
        except Exception as exc:                          # noqa: BLE001 - the screen still runs, without TVL and prices
            log.warning("analyzer protocols: %s", exc)
            meta = {}
        self.screen = {**M.screen(got, meta), "priced": bool(meta), "has_earnings": bool(got.get("earnings"))}
        self._log_screen()

    def _last_logged(self) -> str:
        try:
            lines = self.screen_log.read_text().strip().splitlines() if self.screen_log and self.screen_log.exists() else []
            return json.loads(lines[-1])["day"] if lines else ""
        except (OSError, ValueError, KeyError):
            return ""

    def _log_screen(self) -> None:
        """Once a UTC day, keep what the screen said (every row's market cap and whether it was a setup), so the
        setups can be scored against the rest later."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if not self.screen_log or day == self._logged_day or not self.screen or not self.screen["rows"]:
            return
        rows = [{"name": r["name"], "category": r["category"], "setup": r["setup"], "mcap": r["mcap"], "rev30": r["rev30"],
                 "share_mom": r["share_mom"], "ps_vs": r["ps_vs"]} for r in self.screen["rows"]]
        try:
            self.screen_log.parent.mkdir(parents=True, exist_ok=True)
            with self.screen_log.open("a") as fh:
                fh.write(json.dumps({"day": day, "t_ms": int(time.time() * 1000), "rows": rows}) + "\n")
            self._logged_day = day
        except OSError as exc:
            log.warning("analyzer screen log: %s", exc)

    def screen_days(self) -> int:
        try:
            return sum(1 for _ in self.screen_log.open()) if self.screen_log and self.screen_log.exists() else 0
        except OSError:
            return 0

    async def _capital(self) -> None:
        if not self.map:
            raise RuntimeError("waiting for the fee data")
        names = {c["name"]: {"Hyperliquid": "Hyperliquid L1"}.get(c["name"], c["name"]) for c in self.map["chains"]}
        got = await self.fetch_stables(list(names.values()))
        if not got:
            raise RuntimeError("no chain's stablecoin supply could be read")
        self.stables = {short: got[full] for short, full in names.items() if full in got}

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
                "tables": self._dune_for(chain=name),
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
                if q.get("assets"):
                    await self._dune_assets(q)
                    await asyncio.sleep(1)
                    continue
                rows = await self.fetch["dune"](q["id"])
                self.boards[f"dune-{q['id']}"] = {"title": q.get("title") or f"Dune {q['id']}", "category": q.get("category", "other"),
                                                  "chain": q.get("chain", ""), "source": "Dune", **M.board(rows)}
            except Exception as exc:                      # noqa: BLE001
                errors.append(f"{q.get('id')}: {type(exc).__name__}: {exc}"[:120])
            await asyncio.sleep(1)
        if errors:
            raise RuntimeError("; ".join(errors))

    async def _dune_assets(self, q: dict) -> None:
        qid = str(q["id"])
        got = await self.fetch_dune_table(q["id"])
        self.dune_tables[qid] = {"q": q, "table": M.dune_table(q, got)}
        hours = float(q.get("refresh_h") or 0)
        if hours > 0:
            try:
                age_h = (time.time() - time.mktime(time.strptime(got["ended"][:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone) / 3600 \
                    if got.get("ended") else 1e9
            except ValueError:
                age_h = 1e9
            if age_h >= hours and time.time() - self._dune_ran.get(qid, 0) >= hours * 3600:
                self._dune_ran[qid] = time.time()
                await self.run_dune(q["id"])               # its result is read on the next pass

    def _dune_for(self, protocol: str = "", chain: str = "") -> list[dict]:
        out = []
        for x in self.dune_tables.values():
            q = x["q"]
            if (protocol and protocol in [str(p).lower() for p in q.get("protocols") or []]) or \
                    (chain and chain.lower() in [str(c).lower() for c in q.get("chains") or []]):
                out.append(x["table"])
        return out

    async def assets(self, slug: str, name: str, category: str, chain: str = "") -> dict:
        """What is moving inside one protocol: a list of tables, by the protocol's kind (see the module note)."""
        async def make():
            tables, notes = list(self._dune_for(protocol=slug.lower())), []
            low = slug.lower()
            if category == "perps" and "hyperliquid" in low:
                ctx = getattr(getattr(self.rt, "scanner", None), "contexts", None) or {}
                if ctx:
                    tables.append(M.perps_table(ctx))
            elif category == "prediction" and "polymarket" in low:
                tables.append(M.markets_table(await self.fetch_poly_markets()))
            elif category in ("lending", "yield", "stables", "options"):
                async def pools():
                    return {"rows": await self.fetch_yields()}
                got = await self._cached(("yields",), 3600, pools)
                t = M.yields_table(got.get("rows") or [], low, name)
                if t:
                    tables.append(t)
            elif chain and category not in ("nfts",):
                network = S.GECKO_NETWORK.get(chain) or S.slug_of(chain)

                async def dexes():
                    return {"rows": await self.fetch_dexes(network)}
                ids = M.match_dexes(name, (await self._cached(("dexes", network), 86400, dexes)).get("rows") or [])
                pools = []
                for dex in ids:
                    pools += await self.fetch_dex_pools(network, dex)
                    await asyncio.sleep(0.4)
                if pools:
                    hot = M.heating_table({chain: (network, pools)}, f"Heating up on {name}", memes_only=category in ("memes", "bots"))
                    if hot["rows"]:
                        tables.append(hot)
                    tables.append(M.pools_table(pools, network, f"{name}'s busiest pools on {chain}"))
            if not tables:
                notes.append({"nfts": "Which collections and items are selling needs a Dune query listed for this protocol "
                                      "(data/analyzer_dune.json); none is yet.",
                              "perps": "Market-by-market numbers are read for Hyperliquid only so far.",
                              "prediction": "Market-by-market numbers are read for Polymarket only so far.",
                              "bots": "A bot or terminal has no assets of its own; open its chain to see what is trading there."
                              }.get(category, "No asset-level source matched this protocol; open its chain to see what is trading there."))
            return {"tables": tables, "notes": notes}
        return await self._cached(("assets", slug, chain), 300, make)

    async def heating(self, category: str = "memes") -> dict:
        """Tokens heating up across the main chains, from each chain's trending and busiest pools (5-minute cache)."""
        async def make():
            by, errors = {}, []
            for chain in M.HEAT_CHAINS:
                try:
                    network, trending = await self.fetch_trending(chain)
                    _n, busiest = await self.fetch_pools(chain)
                    seen = {p["address"] for p in trending}
                    by[chain] = (network, trending + [p for p in busiest if p["address"] not in seen])
                except Exception as exc:                  # noqa: BLE001 - the other chains still show
                    errors.append(f"{chain}: {type(exc).__name__}"[:60])
                await asyncio.sleep(0.5)
            if not by:
                raise RuntimeError("; ".join(errors) or "no chain could be read")
            memes = category in ("memes", "bots")
            t = M.heating_table(by, "Memes heating up right now" if memes else "Tokens heating up right now", memes_only=memes)
            self._log_heat(t["rows"])
            return {"table": t, "chains": list(by), "errors": errors}
        return await self._cached(("heating", category in ("memes", "bots")), 300, make)

    def _log_heat(self, rows: list[dict]) -> None:
        if not self.heat_log or not rows or time.time() - self._heat_logged < 3600:
            return
        try:
            self.heat_log.parent.mkdir(parents=True, exist_ok=True)
            with self.heat_log.open("a") as fh:
                fh.write(json.dumps({"t_ms": int(time.time() * 1000), "rows": [
                    {k: r.get(k) for k in ("name", "chain", "address", "price", "pace", "chg", "chg_h1", "liquidity", "fdv", "early")}
                    for r in rows]}) + "\n")
            self._heat_logged = time.time()
        except OSError as exc:
            log.warning("analyzer heat log: %s", exc)

    def public(self) -> dict:
        return {"now_ms": int(time.time() * 1000), "enabled": self.rt.mode == "live", "map": self.map, "maps": self.maps,
                "hyperliquid": self.hl, "screen": self.screen, "screen_days": self.screen_days(),
                "capital": M.capital(self.maps, self.stables) if self.maps else [],
                "boards": list(self.boards.values()), "labels": M.LABEL,
                "status": {SOURCE_NAME[k]: v for k, v in self.status.items()}}

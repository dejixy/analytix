"""
Intel: the independent reads behind the prediction-market edges. Owned by the OutcomeService (api/outcomes.py).

    every 60s     Deribit option surfaces, for the assets that have price markets (BTC, ETH, SOL)
    every 20s     ESPN scoreboards, while a game is on or starts within 30 minutes (every 5 minutes otherwise)
    every 6s      one GDELT news-volume query, rotating through the busiest events (each refreshed every 15 min)
    on demand     the latest headlines, for a market's or an event's page
    every 30s     (driven by the outcome service) sharp-wallet markouts scored, calibration odds noted

Each source is optional: if one is down, the others carry on and /api/health says which.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from engine.intel import game_chip, game_probs, league_for, match_game, news_query, news_spike, norm, headline_flags
from engine.options import build_surface, digital_above, implied_vol_at, touch_prob
from engine.sharp import HORIZONS, Calibration, SharpBook, WalletStat
from ingestion.intel import fetch_headlines, fetch_news_volume, fetch_options, fetch_scoreboard
from storage.outcomeStore import OutcomeStore

log = logging.getLogger("analytix.intel")

OPTIONS_EVERY_S = 60
OPTION_ASSETS = ("BTC", "ETH", "SOL")
GAMES_LIVE_S = 20
GAMES_IDLE_S = 300
NEWS_GAP_S = 6.0                   # GDELT asks for one request every 5 seconds at most
NEWS_TTL_S = 900
HEADLINES_TTL_S = 600
NEWS_QUERIES = 80
SPIKE_RATIO = 3.0
SPIKE_MIN = 5


class IntelService:
    def __init__(self, svc):
        self.svc = svc
        self.surfaces: dict = {}
        self.games: dict[tuple, list[dict]] = {}
        self.matches: dict[str, tuple[dict, bool]] = {}
        self.news: dict[str, dict] = {}                   # target key → {"query", "spike", "fetched"}
        self.headlines: dict[str, tuple[float, list]] = {}
        self.sharp = SharpBook()
        self.calib = Calibration()
        self.store: OutcomeStore | None = None
        self.status = {k: {"ok": None, "last_ms": None, "error": None, "items": 0} for k in ("options", "games", "news")}
        self._gdelt_lock = asyncio.Lock()
        self._gdelt_last = 0.0
        self._pending_headlines: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self.fetch_options, self.fetch_scoreboard = fetch_options, fetch_scoreboard
        self.fetch_news_volume, self.fetch_headlines = fetch_news_volume, fetch_headlines

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self, db_path=None) -> None:
        if db_path:
            try:
                self.store = OutcomeStore(db_path)
                for wallet, h, n, w, s, s2 in self.store.load_wallets():
                    if h in HORIZONS:
                        self.sharp.stats[h][wallet] = WalletStat(n, w, s, s2)
                snaps, results = self.store.load_calibration()
                self.calib.snaps.update(snaps)
                self.calib.results.extend(results)
                log.info("prediction markets: %d wallet records and %d settled markets loaded",
                         len(self.sharp.stats["2h"]), len(self.calib.results))
            except Exception:
                log.exception("could not open the prediction-market store; sharp wallets and calibration start empty")
                self.store = None
        self._tasks = [asyncio.create_task(self._options_loop(), name="intel-options"),
                       asyncio.create_task(self._games_loop(), name="intel-games"),
                       asyncio.create_task(self._news_loop(), name="intel-news")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.save()
        if self.store:
            self.store.close()

    def save(self) -> None:
        if not self.store or not self.sharp.dirty:
            return
        rows = []
        for wallet, h in list(self.sharp.dirty):
            st = self.sharp.stats[h].get(wallet)
            if st:
                rows.append((wallet, h, st.n, st.w, st.s, st.s2))
        self.sharp.dirty.clear()
        self.store.save_wallets(rows)

    def _ok(self, name: str, items: int) -> None:
        self.status[name].update(ok=True, last_ms=int(time.time() * 1000), error=None, items=items)

    def _fail(self, name: str, exc: Exception) -> None:
        self.status[name].update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        log.warning("%s source failed: %s", name, exc)

    # ── options ────────────────────────────────────────────────────────────
    async def _options_loop(self) -> None:
        while True:
            await self.refresh_options()
            await asyncio.sleep(OPTIONS_EVERY_S)

    async def refresh_options(self) -> None:
        wanted = [a for a in OPTION_ASSETS if a in set(self.svc.underlyings())]
        n = 0
        for asset in wanted:
            try:
                surf = build_surface(asset, await self.fetch_options(asset), int(time.time() * 1000))
                if surf:
                    self.surfaces[asset] = surf
                    n += len(surf.smiles)
            except Exception as exc:
                self._fail("options", exc)
                return
        if wanted:
            self._ok("options", n)

    # ── games ──────────────────────────────────────────────────────────────
    def sports_targets(self) -> list[dict]:
        """Every Hyperliquid match (a question, or a single market) with two named sides."""
        out = []
        for q in self.svc.questions.values():
            f = q.fields
            if f.get("participantA") and f.get("participantB"):
                out.append({"key": f"q{q.question}", "f": f, "start_ms": q.starts_ms})
        for m in self.svc.markets.values():
            f = m.fields
            if m.question is None and f.get("participantA") and f.get("participantB"):
                from engine.outcomes import parse_time
                out.append({"key": f"m{m.outcome}", "f": f, "start_ms": parse_time(f.get("scheduledStart"))})
        return out

    async def _games_loop(self) -> None:
        while True:
            live = await self.refresh_games()
            await asyncio.sleep(GAMES_LIVE_S if live else GAMES_IDLE_S)

    async def refresh_games(self) -> bool:
        now = int(time.time() * 1000)
        targets = []
        for t in self.sports_targets():
            lg = league_for(t["f"].get("competition"))
            if lg and (t["start_ms"] is None or now - 12 * 3_600_000 <= t["start_ms"] <= now + 48 * 3_600_000):
                targets.append((t, lg))
        boards = set()
        for t, lg in targets:
            day = datetime.fromtimestamp(((t["start_ms"] or now) - 5 * 3_600_000) / 1000, timezone.utc)
            boards.add((*lg, day.strftime("%Y%m%d")))
        if not boards:
            return False
        fetched = 0
        for sport, league, day in sorted(boards):
            try:
                self.games[(sport, league, day)] = await self.fetch_scoreboard(sport, league, day)
                fetched += 1
            except Exception as exc:
                self._fail("games", exc)
        live = False
        for t, lg in targets:
            pool = [g for (s, l, _), gs in self.games.items() if (s, l) == lg for g in gs]
            f = t["f"]
            found = match_game(f["participantA"], f["participantB"], f.get("shortNameA"), f.get("shortNameB"),
                               t["start_ms"], pool)
            if found:
                self.matches[t["key"]] = found
                g = found[0]
                if g.get("state") == "in" or (g.get("state") == "pre" and g.get("start_ms") and g["start_ms"] - now < 1_800_000):
                    live = True
        if fetched:
            self._ok("games", len(self.matches))
        return live

    def target_of(self, m) -> tuple[str, str] | None:
        """(match key, which result this market pays on: "a", "b", "draw" or "none") for a sports market."""
        if m.question is not None:
            q = self.svc.questions.get(m.question)
            if not q or not (q.fields.get("participantA") and q.fields.get("participantB")):
                return None
            if m.template == "fallback":
                return f"q{q.question}", "none"
            if m.template.startswith("sportsContestDraw"):
                return f"q{q.question}", "draw"
            who = norm(m.fields.get("participant") or m.label)
            if who == norm(q.fields["participantA"]):
                return f"q{q.question}", "a"
            if who == norm(q.fields["participantB"]):
                return f"q{q.question}", "b"
            return None
        if m.fields.get("participantA") and m.fields.get("participantB") and m.template.startswith("sportsContest"):
            return f"m{m.outcome}", "a"                         # a two-way match: Yes is the first side
        return None

    def game_of(self, m):
        if m.question is not None:
            return self.matches.get(f"q{m.question}")
        return self.matches.get(f"m{m.outcome}")

    def chip(self, m) -> dict | None:
        found = self.game_of(m)
        return game_chip(*found) if found else None

    # ── the independent chance for a market ────────────────────────────────
    def reference(self, m, now: int) -> dict | None:
        if m.kind in ("price", "touch") and m.underlying in self.surfaces and m.expiry_ms:
            surf, spot = self.surfaces[m.underlying], self.svc.spot(m.underlying)
            if m.kind == "price":
                p = digital_above(surf, spot, m.threshold, m.expiry_ms, now)
            else:
                p = touch_prob(surf, spot, m.threshold, m.expiry_ms, now)
            if p is None:
                return None
            p = min(0.995, max(0.005, p))
            iv = implied_vol_at(surf, spot, m.threshold, m.expiry_ms, now)
            return {"p": p, "source": "options",
                    "label": f"Options market (Deribit), implied vol {iv * 100:.0f}% a year at this line" if iv else "Options market (Deribit)"}
        target = self.target_of(m)
        found = self.game_of(m) if target else None
        if not found:
            return None
        probs, src, label = game_probs(*found)
        if not probs:
            return None
        role = target[1]
        if role == "none":
            if src != "final":
                return None
            p = 0.0
        else:
            p = probs.get(role)
        if p is None:
            return None
        lo, hi = (0.0, 1.0) if src == "final" else (0.005, 0.995)      # a model is never certain; a final score is
        return {"p": min(hi, max(lo, p)), "source": src, "label": label}

    # ── news ───────────────────────────────────────────────────────────────
    def news_targets(self) -> list[tuple[str, str, float]]:
        """(key, GDELT query, 24h volume) for the busiest events and single markets, price markets excluded."""
        rows = []
        for q in self.svc.questions.values():
            ms = [self.svc.markets[o] for o in q.outcomes if o in self.svc.markets]
            if not ms or ms[0].kind in ("price", "touch"):
                continue
            f = q.fields
            if f.get("participantA") and f.get("participantB"):
                names = [f["participantA"], f["participantB"]]
            else:
                ranked = sorted((m for m in ms if m.template != "fallback" and not m.template.startswith("sportsContestDraw")),
                                key=lambda m: -(self.svc.quote(m)[4] or 0))
                names = [m.fields.get("participant") or m.fields.get("candidate") or m.label for m in ranked[:3]]
            vol = sum((self.svc.ctx.get(m.yes_coin) or {}).get("volume") or 0 for m in ms)
            query = news_query(names)
            if query:
                rows.append((f"q{q.question}", query, vol))
        for m in self.svc.markets.values():
            if m.question is not None or m.kind in ("price", "touch"):
                continue
            f = m.fields
            names = [f["participantA"], f["participantB"]] if f.get("participantA") and f.get("participantB") else \
                [f.get("participant") or f.get("candidate") or f.get("company") or m.label]
            query = news_query(names)
            if query:
                rows.append((f"m{m.outcome}", query, (self.svc.ctx.get(m.yes_coin) or {}).get("volume") or 0))
        return sorted(rows, key=lambda r: -r[2])[:NEWS_QUERIES]

    async def _gdelt(self, fn, *args):
        async with self._gdelt_lock:
            wait = NEWS_GAP_S - (time.time() - self._gdelt_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await fn(*args)
            finally:
                self._gdelt_last = time.time()

    async def _news_loop(self) -> None:
        await asyncio.sleep(20)                                   # let the market list arrive first
        while True:
            try:
                await self.refresh_news_once()
            except Exception as exc:
                self._fail("news", exc)
            await asyncio.sleep(1)

    async def refresh_news_once(self) -> bool:
        targets = self.news_targets()
        now = time.time()
        due = [t for t in targets if now - (self.news.get(t[0]) or {}).get("fetched", 0) > NEWS_TTL_S
               or (self.news.get(t[0]) or {}).get("query") != t[1]]
        if not due:
            await asyncio.sleep(NEWS_GAP_S)
            return False
        key, query, _ = min(due, key=lambda t: (self.news.get(t[0]) or {}).get("fetched", 0))
        pts = await self._gdelt(self.fetch_news_volume, query)
        self.news[key] = {"query": query, "spike": news_spike(pts, int(time.time() * 1000)), "fetched": time.time()}
        self._ok("news", sum(1 for v in self.news.values() if v.get("spike")))
        return True

    def news_key(self, m) -> str:
        return f"q{m.question}" if m.question is not None else f"m{m.outcome}"

    def news_for(self, key: str) -> dict | None:
        """The spike tag for a market or event, only when coverage is clearly up on its own normal."""
        s = (self.news.get(key) or {}).get("spike")
        if not s or s.get("ratio") is None:
            return None
        return {"ratio": s["ratio"], "recent": s["recent"], "usual": s["usual"],
                "spike": s["ratio"] >= SPIKE_RATIO and s["recent"] >= SPIKE_MIN}

    def headlines_for(self, key: str) -> dict | None:
        """The latest headlines for a page, fetched in the background (GDELT is slow to call), with keyword flags."""
        n = self.news.get(key) or {}
        query = n.get("query")
        if not query:
            t = next((t for t in self.news_targets() if t[0] == key), None)
            query = t[1] if t else None
        if not query:
            return None
        hit = self.headlines.get(query)
        if (not hit or time.time() - hit[0] > HEADLINES_TTL_S) and query not in self._pending_headlines:
            self._pending_headlines.add(query)
            asyncio.get_running_loop().create_task(self._load_headlines(query))
        arts = hit[1] if hit else None
        return {"query": query, "articles": arts, "flags": headline_flags([a["title"] for a in arts or []]),
                "spike": self.news_for(key)}

    async def _load_headlines(self, query: str) -> None:
        try:
            self.headlines[query] = (time.time(), await self._gdelt(self.fetch_headlines, query))
        except Exception as exc:
            self._fail("news", exc)
        finally:
            self._pending_headlines.discard(query)

    # ── what /api/health and the list show ─────────────────────────────────
    def public_status(self) -> dict:
        return {**self.status, "surfaces": {a: len(s.smiles) for a, s in self.surfaces.items()},
                "matched_games": len(self.matches), "news_tracked": len(self.news), "sharp": self.sharp.summary(),
                "calibration_settled": len(self.calib.results)}

"""
The bets suite, live (/bets, /api/bets; engine/betting.py has the rules). Live mode only.

    history        football-data.co.uk, three seasons of 12 leagues, once a day: the calibration and team form
    sharp prices   The Odds API once a day for the main leagues (with ANALYTIX_ODDS_API_KEY; ~8 credits a day), and
                   football-data's fixtures every six hours (free; Pinnacle or the market average). The fresher wins
    Nigerian odds  pushed from a laptop in Nigeria (scripts/naijaOdds.py) to POST /api/bets/odds through the tunnel;
                   ANALYTIX_BETS_TOKEN, if set, must match its X-Bets-Token header
    paper book     every ten minutes: new flat value bets are booked at a flat stake, today's rollover pick is made once
                   (after 06:00 Lagos, and only once the last one has settled), open bets are settled from results
                   (The Odds API's scores, else football-data) and given their closing-line value
Kept in data/bets.json (the book) and data/bets_odds.json (the last push from each book).
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine import betting as B
from ingestion import football as F

log = logging.getLogger("analytix.bets")
LAGOS = ZoneInfo("Africa/Lagos")
ODDS_API_LEAGUES = ("E0", "SP1", "I1", "D1", "F1", "N1", "P1", "UCL")
EVERY_S = {"history": 86400, "fixtures": 6 * 3600, "sharp": 86400, "book": 600}
MIN_CREDITS = 40


class BetsService:
    def __init__(self, runtime, path=None, odds_path=None):
        self.rt = runtime
        self.book = B.Book(path)
        self.odds_path = Path(odds_path) if odds_path else None
        self.books: dict[str, dict] = self._load_odds()
        self.history: list[dict] = []
        self.cal: dict = {}
        self.sharp_fd: list[dict] = []
        self.sharp_oa: list[dict] = []
        self.credits: int | None = None
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in ("history", "fixtures", "sharp", "book")}
        self._board: tuple[float, list] | None = None
        self._tasks: list[asyncio.Task] = []
        self.fetch_history, self.fetch_fixtures, self.fetch_odds_api = F.fetch_history, F.fetch_fixtures, F.fetch_odds_api

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.rt.mode == "live":
            self._tasks = [asyncio.create_task(self._loop(k), name=f"bets-{k}") for k in EVERY_S]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.book.save()

    async def _loop(self, key: str) -> None:
        await asyncio.sleep({"history": 20, "fixtures": 40, "sharp": 120, "book": 300}[key])
        while True:
            await self.refresh(key)
            await asyncio.sleep(EVERY_S[key])

    async def refresh(self, key: str) -> None:
        try:
            await getattr(self, f"_{key}")()
            self.status[key] = {"ok": True, "at": int(time.time() * 1000), "error": None}
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - one part down must not stop the rest
            setup = "no key" in str(exc)
            self.status[key] = {"ok": False, "at": self.status[key]["at"], "setup": setup,
                                "error": (str(exc) if setup else f"{type(exc).__name__}: {exc}")[:200]}
            if not setup:
                log.warning("bets %s: %s", key, exc)

    async def _history(self) -> None:
        games, errors = await self.fetch_history()
        if not games:
            raise RuntimeError("; ".join(errors[:4]) or "no games read")
        self.history = sorted(games, key=lambda g: g["t_ms"])
        self.cal = B.calibration(self.history)
        self._board = None
        if errors:
            self.status["history"]["note"] = f"{len(errors)} files not read"

    async def _fixtures(self) -> None:
        fx = await self.fetch_fixtures()
        self.sharp_fd = F.sharp_from_fd(fx, int(time.time() * 1000))
        self._board = None

    async def _sharp(self) -> None:
        if self.credits is not None and self.credits < MIN_CREDITS:
            raise RuntimeError(f"only {self.credits} credits left this month; using football-data's prices until it resets")
        sports = {k: (F.LEAGUES.get(k) or F.CUPS[k]) for k in ODDS_API_LEAGUES}
        got, left, errors = await self.fetch_odds_api(sports, "odds")
        self.credits = left if left is not None else self.credits
        now = int(time.time() * 1000)
        rows = []
        for league, events in got.items():
            rows += F.parse_odds_api(events, league, sports[league][0], now)
        if not rows and errors:
            raise RuntimeError("; ".join(errors))
        self.sharp_oa = rows
        self._board = None

    async def _book(self) -> None:
        """Book new flat value bets, make today's rollover pick, settle what has finished."""
        now = int(time.time() * 1000)
        board = self.board(now)
        for x in B.value_bets(board):
            self.book.add_flat(x, now)
        day = datetime.fromtimestamp(now / 1000, LAGOS)
        if day.hour >= 6:
            pick = B.rollover_pick(board, now)
            if pick:
                self.book.add_pick(pick, day.strftime("%Y-%m-%d"), now)
        await self._settle(now)
        self.book.save()

    async def _settle(self, now: int) -> None:
        open_ = [b for b in [*self.book.flat, *self.book.picks] if b["status"] == "open" and b["t_ms"] < now - 2 * B.HOUR]
        results = [g for g in self.history if g.get("hg") is not None and g["t_ms"] > now - 10 * B.DAY]
        if open_ and os.getenv("ANALYTIX_ODDS_API_KEY") and (self.credits is None or self.credits >= MIN_CREDITS // 2):
            leagues = {b["league"] for b in open_ if b["league"] in F.LEAGUES or b["league"] in F.CUPS}
            try:
                got, left, _errors = await self.fetch_odds_api({k: (F.LEAGUES.get(k) or F.CUPS[k]) for k in leagues}, "scores")
                self.credits = left if left is not None else self.credits
                for events in got.values():
                    results += F.parse_scores(events)
            except Exception as exc:                      # noqa: BLE001 - football-data's results still settle it
                log.warning("bets scores: %s", exc)
        closing = [{"home": g["home"], "away": g["away"], "t_ms": g["t_ms"], "fair": B.devig(
                    {"1": g.get("PSCH") or g.get("AvgCH"), "X": g.get("PSCD") or g.get("AvgCD"), "2": g.get("PSCA") or g.get("AvgCA")})}
                   for g in self.history if g["t_ms"] > now - 10 * B.DAY and (g.get("PSCH") or g.get("AvgCH"))]
        self.book.settle(results, closing)

    # ── odds from the laptop ──────────────────────────────────────────────
    def push(self, payload: dict, token: str = "") -> dict:
        want = os.getenv("ANALYTIX_BETS_TOKEN", "")
        if want and token != want:
            raise PermissionError("wrong or missing X-Bets-Token")
        book, rows = F.parse_push(payload)
        if not book:
            raise ValueError("no book named")
        self.books[book] = {"t_ms": int(time.time() * 1000), "rows": rows}
        self._board = None
        self._save_odds()
        return {"book": book, "games": len(rows), "matched": self._matched(book)}

    def _matched(self, book: str) -> int:
        sharp = self.sharp()
        return sum(1 for r in self.books[book]["rows"] if B.match_fixture(r["home"], r["away"], r["t_ms"], sharp))

    def _load_odds(self) -> dict:
        try:
            return json.loads(self.odds_path.read_text()) if self.odds_path and self.odds_path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _save_odds(self) -> None:
        if self.odds_path:
            self.odds_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.odds_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.books))
            os.replace(tmp, self.odds_path)

    # ── the board ──────────────────────────────────────────────────────────
    def sharp(self) -> list[dict]:
        """The Odds API's games, then football-data's for any game it doesn't have."""
        out = list(self.sharp_oa)
        for g in self.sharp_fd:
            if not B.match_fixture(g["home"], g["away"], g["t_ms"], out, within_h=12):
                out.append(g)
        return out

    def board(self, now: int | None = None) -> list[dict]:
        now = now or int(time.time() * 1000)
        if self._board and time.time() - self._board[0] < 60:
            return self._board[1]
        b = B.build(self.books, self.sharp(), self.history, self.cal, now)
        self._board = (time.time(), b)
        return b

    def public(self) -> dict:
        now = int(time.time() * 1000)
        board = self.board(now) if self.books else []
        day = datetime.fromtimestamp(now / 1000, LAGOS).strftime("%Y-%m-%d")
        today = self.book.today_pick(day)
        candidate = B.rollover_pick(board, now) if not today else None
        chain = self.book.chain
        p_now = (today or candidate or {}).get("p")
        left = B.ROLL_TARGET - chain["day"]
        pats = []
        for m in ("1", "2", "1X", "X2"):
            for i, (lo, hi) in enumerate(B.BUCKETS):
                c = self.cal.get(("*", m, i))
                if c and c[0] >= 100:
                    pats.append({"market": m, "name": B.MARKET_NAME[m], "odds": [round(1 / hi, 2), round(1 / lo, 2)], "n": c[0],
                                 "rate": c[1] / c[0], "implied": c[2] / c[0]})
        return {"now_ms": now, "enabled": self.rt.mode == "live",
                "value": B.value_bets(board)[:60], "board_n": len(board),
                "near": [x for x in board if B.FLAT_MIN <= x["odds"] <= B.FLAT_MAX and x["market"] != "X"][:40],
                "rollover": {"today": today, "candidate": candidate, "chain": chain, "target": B.ROLL_TARGET,
                             "refills_max": B.ROLL_REFILLS, "start": B.ROLL_START,
                             "p_finish": B.chain_odds(p_now, left) if p_now else None, "days_left": left,
                             "history": self.book.picks[-30:][::-1]},
                "record": self.book.record(), "flat_recent": self.book.flat[-40:][::-1], "patterns": pats,
                "settings": {"flat": [B.FLAT_MIN, B.FLAT_MAX, B.FLAT_EDGE, B.FLAT_STAKE],
                             "roll": [B.ROLL_MIN, B.ROLL_MAX, B.ROLL_EDGE]},
                "sources": {"books": {k: {"t_ms": v["t_ms"], "games": len(v["rows"])} for k, v in self.books.items()},
                            "history_games": len(self.history), "sharp_oa": len(self.sharp_oa), "sharp_fd": len(self.sharp_fd),
                            "credits": self.credits, "status": self.status}}

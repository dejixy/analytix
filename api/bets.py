"""
The bets suite, live (/bets, /api/bets; engine/betting.py has the rules). Live mode only.

    history        football-data.co.uk, three seasons of 14 leagues, once a day: the calibration and team form
    sharp prices   The Odds API once a day for the main leagues, the European cups and any international competition in
                   season (with ANALYTIX_ODDS_API_KEY; a credit for each league that has games, none for one without),
                   and football-data's fixtures every six hours for all 14 leagues (free; Pinnacle or the market
                   average). The fresher wins. 30 minutes to 3 hours before kick-off, any league with a game that could be
                   bet is read again (a credit a league, at most every 2 hours, while 100+ credits are left)
    Nigerian odds  pushed from a laptop in Nigeria (scripts/naijaOdds.py) to POST /api/bets/odds through the tunnel;
                   ANALYTIX_BETS_TOKEN, if set, must match its X-Bets-Token header
    paper book     every ten minutes: new flat value bets are booked at a flat stake, today's rollover pick is made once
                   (after 06:00 Lagos, and only once the last one has settled), open bets are settled from results
                   (The Odds API's scores, else football-data) and given their closing-line value
Kept in data/bets.json (the book), data/bets_odds.json (the last push from each book) and data/bets_sharp.json (the last
paid sharp read, so a restart reuses it until it's a day old instead of spending credits again).

The board (every Nigerian price against its sharp game, with form and history) is built on a worker thread and kept for a
minute, or until new odds or prices arrive, so building it never holds up the rest of the app.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from api.public import scrub
from engine import betting as B
from engine import betstudy as S
from ingestion import football as F

log = logging.getLogger("analytix.bets")
LAGOS = ZoneInfo("Africa/Lagos")
ODDS_API_LEAGUES = ("E0", "E1", "E2", "E3", "SP1", "I1", "D1", "F1", "UCL", "UEL", "UECL")    # read daily; the other six
                                                                  # leagues come from football-data and the pre-kick-off read
EVERY_S = {"history": 86400, "fixtures": 6 * 3600, "sharp": 86400, "book": 600}
MIN_CREDITS = 40
FRESH_MIN_CREDITS = 100        # the pre-kick-off reads stop here, keeping the rest of the month for the daily read
FRESH_FROM_H, FRESH_TO_H, FRESH_EVERY_H = 0.5, 3, 2      # read again 30 min to 3 h before kick-off, a league every 2 h


class BetsService:
    def __init__(self, runtime, path=None, odds_path=None, sharp_path=None):
        self.rt = runtime
        self.sharp_path = Path(sharp_path) if sharp_path else None
        self.book = B.Book(path)
        self.odds_path = Path(odds_path) if odds_path else None
        self.books: dict[str, dict] = self._load_odds()
        self.history: list[dict] = []
        self.cal: dict = {}
        self.teams: B.Teams | None = None
        self.methods: dict = {}                               # each margin method's fit to three seasons of results
        self.study: dict = {}                                 # the rules replayed on three seasons (engine/betstudy.py)
        self.sharp_fd: list[dict] = []
        self.sharp_oa: list[dict] = []
        self.credits: int | None = None
        self.intl: dict[str, tuple[str, str]] = {}            # international competitions in season: {sport key: (title, key)}
        self.cups: dict[str, tuple[str, str]] = dict(F.CUPS)  # the European cups: {code: (name, sport key)}
        self._fresh_at: dict[str, int] = {}                   # when each league last had a pre-kick-off read
        self._scores_at: dict[str, float] = {}                # when each league's results were last asked for
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in ("history", "fixtures", "sharp", "book")}
        self._board: tuple[float, int, list] | None = None    # (built at, generation, rows)
        self._gen = 0                                          # bumped whenever odds, prices or history change
        self._sharp_rows: tuple[int, list] | None = None
        self._sharp_v = 0
        self._lock: asyncio.Lock | None = None
        self._rebuild_task: asyncio.Task | None = None
        self._match_n: dict[str, tuple] = {}
        self._tasks: list[asyncio.Task] = []
        self.fetch_history, self.fetch_fixtures, self.fetch_odds_api = F.fetch_history, F.fetch_fixtures, F.fetch_odds_api
        self.fetch_sports = F.fetch_sports
        self._load_sharp()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.rt.mode == "live":
            self._tasks = [asyncio.create_task(self._loop(k), name=f"bets-{k}") for k in EVERY_S]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.book.save()

    def _first_wait(self, key: str, now_s: float | None = None) -> float:
        """Seconds before a part's first read after a start: the sharp read waits until the saved one is a day old."""
        wait = {"history": 20, "fixtures": 40, "sharp": 120, "book": 300}[key]
        st = self.status[key]
        if key == "sharp" and st.get("ok") and st.get("at"):
            wait = max(wait, st["at"] / 1000 + EVERY_S["sharp"] - (now_s or time.time()))
        return wait

    async def _loop(self, key: str) -> None:
        await asyncio.sleep(self._first_wait(key))
        while True:
            await self.refresh(key)
            st = self.status[key]
            await asyncio.sleep(EVERY_S[key] if st["ok"] else min(EVERY_S[key], 3600 if st.get("setup") else 1800))

    async def refresh(self, key: str) -> None:
        try:
            await getattr(self, f"_{key}")()
            self.status[key] = {"ok": True, "at": int(time.time() * 1000), "error": None}
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - one part down must not stop the rest
            setup = "no key" in str(exc)
            self.status[key] = {"ok": False, "at": self.status[key]["at"], "setup": setup,
                                "error": scrub(str(exc) if setup else f"{type(exc).__name__}: {exc}")[:200]}
            if not setup:
                log.warning("bets %s: %s", key, scrub(exc))

    async def _history(self) -> None:
        games, errors = await self.fetch_history()
        if not games:
            raise RuntimeError("; ".join(errors[:4]) or "no games read")
        history = sorted(games, key=lambda g: g["t_ms"])

        def crunch():                                     # a worker thread: half a minute or so, once a day
            scores = B.compare_methods(history)
            B.set_method(B.best_method(scores))
            teams = B.Teams(history)
            return scores, B.calibration(history), teams, S.study(history, teams)
        self.methods, self.cal, self.teams, self.study = await asyncio.to_thread(crunch)
        self.history = history
        self._changed()
        if errors:
            self.status["history"]["note"] = f"{len(errors)} files not read"

    async def _fixtures(self) -> None:
        fx = await self.fetch_fixtures()
        self.sharp_fd = F.sharp_from_fd(fx, int(time.time() * 1000))
        self._changed(sharp=True)

    async def _sharp(self) -> None:
        if self.credits is not None and self.credits < MIN_CREDITS:
            raise RuntimeError(f"only {self.credits} credits left this month; using football-data's prices until it resets")
        try:
            listed = await self.fetch_sports()                # free: no credits
            self.intl, self.cups = F.intl_sports(listed), F.club_cups(listed)
        except Exception as exc:                          # noqa: BLE001 - the leagues still read without it
            if "no key" in str(exc):
                raise
            log.warning("bets: the list of competitions: %s", scrub(exc))
        sports = {k: self._sport(k) for k in ODDS_API_LEAGUES}
        sports.update(self.intl)
        got, left, errors = await self.fetch_odds_api(sports, "odds")
        self.credits = left if left is not None else self.credits
        now = int(time.time() * 1000)
        rows = []
        for league, events in got.items():
            rows += F.parse_odds_api(events, league, sports[league][0], now, intl=league in self.intl)
        if not rows and errors:
            raise RuntimeError("; ".join(errors))
        self.sharp_oa = rows
        self._changed(sharp=True)
        self._save_sharp(now)

    def _sport(self, code: str) -> tuple[str, str] | None:
        """(name, The Odds API sport key) for a league code, a cup or an international competition."""
        return F.LEAGUES.get(code) or self.cups.get(code) or self.intl.get(code)

    async def _pre_kickoff(self, now: int) -> None:
        """A fresh sharp price shortly before kick-off: the leagues with a game that could be bet (odds 1.15 to 1.50, no
        worse than 2% under fair) kicking off in 30 minutes to 3 hours, each at most every 2 hours. One credit a league
        with games, none for one without; skipped below FRESH_MIN_CREDITS."""
        if not os.getenv("ANALYTIX_ODDS_API_KEY") or (self.credits is not None and self.credits < FRESH_MIN_CREDITS):
            return
        board = self._board[2] if self._board else []
        want = {}
        for x in board:
            if (now + FRESH_FROM_H * B.HOUR <= x["t_ms"] <= now + FRESH_TO_H * B.HOUR and B.ROLL_MIN <= x["odds"] <= B.FLAT_MAX
                    and x["edge"] >= B.ROLL_EDGE and x["market"] != "X" and now - self._fresh_at.get(x["league"], 0) > FRESH_EVERY_H * B.HOUR):
                sport = self._sport(x["league"])
                if sport:
                    want[x["league"]] = sport
        if not want:
            return
        for k in want:
            self._fresh_at[k] = now
        got, left, _errors = await self.fetch_odds_api(want, "odds")
        self.credits = left if left is not None else self.credits
        rows = []
        for league, events in got.items():
            rows += F.parse_odds_api(events, league, want[league][0], now, intl=league in self.intl)
        if rows:
            self.sharp_oa = [r for r in self.sharp_oa if r["league"] not in got] + rows
            self._changed(sharp=True)
            self.status["sharp"]["fresh"] = {"at": now, "leagues": sorted(got)}
        self._save_sharp()

    async def _book(self) -> None:
        """Refresh the sharp price of games about to start, book new flat value bets, make today's rollover pick,
        settle what has finished."""
        now = int(time.time() * 1000)
        try:
            await self._pre_kickoff(now)
        except Exception as exc:                          # noqa: BLE001 - the day's read still stands
            log.warning("bets fresh read: %s", scrub(exc))
        board = await self.board_async(now)
        for x in B.value_bets(board, before_ms=now + B.BOOK_DAYS * B.DAY):
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
            sport = lambda k: self._sport(k) or ((k, k) if k.startswith("soccer_") else None)
            leagues = {b["league"] for b in open_ if sport(b["league"]) and time.time() - self._scores_at.get(b["league"], 0) > 3600}
            try:
                for k in leagues:                         # at most once an hour a league: two credits a read
                    self._scores_at[k] = time.time()
                got, left, _errors = await self.fetch_odds_api({k: sport(k) for k in leagues}, "scores") if leagues else ({}, None, [])
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
    async def push(self, payload: dict, token: str = "") -> dict:
        want = os.getenv("ANALYTIX_BETS_TOKEN", "")
        if want and token != want:
            raise PermissionError("wrong or missing X-Bets-Token")
        book, rows = F.parse_push(payload)
        if not book:
            raise ValueError("no book named")
        self.books[book] = {"t_ms": int(time.time() * 1000), "rows": rows}
        self._changed()
        self._save_odds()
        return {"book": book, "games": len(rows), "matched": await asyncio.to_thread(self._matched, book)}

    def _matched(self, book: str) -> int:
        """How many of a book's games found their sharp line (kept until the book or the sharp prices change)."""
        got = self.books[book]
        hit = self._match_n.get(book)
        if hit and hit[0] == (got["t_ms"], self._sharp_v):
            return hit[1]
        v = self._sharp_v
        sharp = self.sharp()
        n = sum(1 for r in got["rows"] if B.match_fixture(r["home"], r["away"], r["t_ms"], sharp))
        self._match_n[book] = ((got["t_ms"], v), n)
        return n

    def _save_sharp(self, read_ms: int | None = None) -> None:
        """The last paid sharp read, with what it needs to be reused after a restart."""
        if not self.sharp_path:
            return
        if read_ms:
            self._sharp_read_ms = read_ms
        keep = {"read_ms": getattr(self, "_sharp_read_ms", None), "rows": self.sharp_oa, "credits": self.credits,
                "intl": self.intl, "cups": self.cups, "fresh_at": self._fresh_at}
        try:
            self.sharp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.sharp_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(keep))
            os.replace(tmp, self.sharp_path)
        except OSError as exc:
            log.warning("bets: couldn't save the sharp read: %s", exc)

    def _load_sharp(self) -> None:
        try:
            got = json.loads(self.sharp_path.read_text()) if self.sharp_path and self.sharp_path.exists() else None
        except (OSError, ValueError):
            got = None
        if not got or not got.get("read_ms") or not isinstance(got.get("rows"), list):
            return
        self.sharp_oa, self.credits, self._sharp_read_ms = got["rows"], got.get("credits"), got["read_ms"]
        self.intl = {k: tuple(v) for k, v in (got.get("intl") or {}).items()}
        self.cups = {**self.cups, **{k: tuple(v) for k, v in (got.get("cups") or {}).items()}}
        self._fresh_at = {k: int(v) for k, v in (got.get("fresh_at") or {}).items()}
        self.status["sharp"] = {"ok": True, "at": got["read_ms"], "error": None, "saved": True}

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
    def _changed(self, sharp: bool = False) -> None:
        self._gen += 1
        if sharp:
            self._sharp_v += 1

    def sharp(self) -> list[dict]:
        """The Odds API's games, then football-data's for any game it doesn't have (kept until either changes)."""
        v = self._sharp_v
        if self._sharp_rows and self._sharp_rows[0] == v:
            return self._sharp_rows[1]
        out = list(self.sharp_oa)
        for g in self.sharp_fd:
            if not B.match_fixture(g["home"], g["away"], g["t_ms"], out, within_h=12):
                out.append(g)
        self._sharp_rows = (v, out)
        return out

    def _fresh(self) -> bool:
        return bool(self._board) and self._board[1] == self._gen and time.time() - self._board[0] < 60

    def _rebuild(self, now: int) -> list[dict]:
        gen = self._gen
        b = B.build(dict(self.books), self.sharp(), self.history, self.cal, now, self.teams)
        self._board = (time.time(), gen, b)
        for k in list(self.books):
            self._matched(k)
        return b

    def board(self, now: int | None = None) -> list[dict]:
        if self._fresh():
            return self._board[2]
        return self._rebuild(now or int(time.time() * 1000))

    async def board_async(self, now: int | None = None, wait: bool = True) -> list[dict]:
        """The board, built on a worker thread (one build at a time). With wait=False an old board is returned at once
        and a fresh one built behind it, so the page never waits on a build once there is one."""
        if self._fresh():
            return self._board[2]
        if not wait and self._board:
            if self._rebuild_task is None or self._rebuild_task.done():
                self._rebuild_task = asyncio.create_task(self._rebuild_quietly())
            return self._board[2]
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self._fresh():
                return self._board[2]
            return await asyncio.to_thread(self._rebuild, now or int(time.time() * 1000))

    async def _rebuild_quietly(self) -> None:
        try:
            await self.board_async()
        except Exception as exc:                          # noqa: BLE001 - the next request tries again
            log.warning("bets board: %s", scrub(exc))

    async def snapshot(self, public: bool = False) -> dict:
        """What the page shows, with the slow parts done off the event loop (public: opened through the public link)."""
        board = await self.board_async(wait=False) if self.books else []
        await asyncio.to_thread(lambda: [self._matched(k) for k in list(self.books)])
        return {**self.public(board), "public": public}

    def public(self, board: list[dict] | None = None) -> dict:
        now = int(time.time() * 1000)
        if board is None:
            board = self.board(now) if self.books else []
        day = datetime.fromtimestamp(now / 1000, LAGOS).strftime("%Y-%m-%d")
        today = self.book.today_pick(day)
        candidate = B.rollover_pick(board, now) if not today else None
        chain = self.book.chain
        p_now = (today or candidate or {}).get("p")
        left = B.ROLL_TARGET - chain["day"]
        pats = []
        for kind in ("sharp", "avg"):
            for m in ("1", "2", "1X", "X2"):
                for i, (lo, hi) in enumerate(B.BUCKETS):
                    c = self.cal.get((kind, "*", m, i))
                    if c and c[0] >= 100:
                        pats.append({"kind": kind, "market": m, "name": B.MARKET_NAME[m], "odds": [round(1 / hi, 2), round(1 / lo, 2)],
                                     "n": c[0], "rate": c[1] / c[0], "implied": c[2] / c[0]})
        return {"now_ms": now, "enabled": self.rt.mode == "live",
                "value": B.value_bets(board)[:60], "board_n": len(board), "book_days": B.BOOK_DAYS, "show_days": B.SHOW_DAYS,
                "matched": {k: self._matched(k) for k in self.books},
                "next_kick": min((x["t_ms"] for x in board), default=None),
                "near": [x for x in board if B.FLAT_MIN <= x["odds"] <= B.FLAT_MAX and x["market"] != "X"][:40],
                "rollover": {"today": today, "candidate": candidate, "chain": chain, "target": B.ROLL_TARGET,
                             "misses": B.rollover_misses(board, now) if not (today or candidate) else [],
                             "refills_max": B.ROLL_REFILLS, "start": B.ROLL_START,
                             "p_finish": B.chain_odds(p_now, left) if p_now else None, "days_left": left,
                             "history": self.book.picks[-30:][::-1]},
                "record": self.book.record(), "flat_recent": self.book.flat[-40:][::-1], "patterns": pats,
                "method": B.method(), "methods": self.methods, "study": self.study,
                "settings": {"flat": [B.FLAT_MIN, B.FLAT_MAX, B.FLAT_EDGE, B.FLAT_STAKE], "flat_low": B.FLAT_EDGE_LOW,
                             "edge_max": B.EDGE_MAX, "lower": sorted(B.LOWER_LEAGUES),
                             "roll": [B.ROLL_MIN, B.ROLL_MAX, B.ROLL_EDGE]},
                "sources": {"books": {k: {"t_ms": v["t_ms"], "games": len(v["rows"])} for k, v in self.books.items()},
                            "history_games": len(self.history), "sharp_oa": len(self.sharp_oa), "sharp_fd": len(self.sharp_fd),
                            "credits": self.credits, "status": self.status}}

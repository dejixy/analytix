"""
The bets suite, live (/bets, /api/bets; engine/betting.py has the rules). Live mode only.

    history        football-data.co.uk, three seasons of 14 leagues, once a day: the calibration and team form
    sharp prices   The Odds API once a day for the main leagues, the European cups and any international competition in
                   season (with ANALYTIX_ODDS_API_KEY; a credit for each league that has games, none for one without),
                   and football-data's fixtures every six hours for all 14 leagues (free; Pinnacle or the market
                   average). The fresher wins. 30 minutes to 3 hours before kick-off, any league with a game that could be
                   bet is read again (a credit a league, at most every 2 hours, while 100+ credits are left).
                   Over/under 2.5 goals comes only from football-data's fixtures (Pinnacle's, else the market average),
                   added to the same game when The Odds API has its win prices: free, but read a day or two before
                   kick-off, so goals bets need the higher 3% edge
    models         with the history read: the goals model's Dixon-Coles correction and how it and the count models
                   (corners, shots, shots on target, saves) did on past games (engine/goals.py, engine/corners.py), and
                   the count models as they stand
    Nigerian odds  pushed from a laptop in Nigeria (scripts/naijaOdds.py) to POST /api/bets/odds through the tunnel;
                   ANALYTIX_BETS_TOKEN, if set, must match its X-Bets-Token header
    paper book     every ten minutes: new flat value bets are booked at a flat stake, the next rollover leg is booked once
                   the last has settled (as many a day as fit, any time of day), and open bets are settled from
                   results (The Odds API's scores, else football-data) and given their closing-line value
Kept in data/bets.json (the book), data/bets_odds.json (the last push from each book) and data/bets_sharp.json (the last
paid sharp read, so a restart reuses it until it's a day old instead of spending credits again).

The board (every Nigerian price against its sharp game, with form and history) is built on a worker thread and kept for a
minute, or until new odds or prices arrive, so building it never holds up the rest of the app. The combo builder
(GET /api/bets/combo, engine/combos.py) searches the same board, kept per board and window.

SportyBet booking codes: SportyBet turns the server away (403) as Bet9ja does, so a code is made on the laptop. The page
asks for one (GET /api/bets/combo/code: the SportyBet slip for its settings is queued); the collector looks every 15
seconds (GET /api/bets/code-jobs), books the slip on SportyBet and sends the code back (POST /api/bets/code-jobs). A
code is kept for its slip until SportyBet's deadline; at most CODES_AN_HOUR new ones are asked for an hour.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from api.public import scrub
from engine import betting as B
from engine import betstudy as S
from engine import combos as C
from engine import corners as K
from engine import goals as G
from engine import vet as V
from ingestion import football as F

log = logging.getLogger("analytix.bets")
LAGOS = ZoneInfo("Africa/Lagos")
ODDS_API_LEAGUES = ("E0", "E1", "E2", "E3", "SP1", "I1", "D1", "F1", "UCL", "UEL", "UECL")    # read daily; the other six
                                                                  # leagues come from football-data and the pre-kick-off read
EVERY_S = {"history": 86400, "fixtures": 6 * 3600, "sharp": 86400, "book": 600}
ROLL_DONE_MS = 115 * 60_000           # a game is over about this long after kick-off
ROLL_SCORES_S = 900                   # the open rollover leg's league: its scores asked this often (The Odds API)
ESPN_ROLL_S, ESPN_OTHER_S = 240, 1800 # ESPN's free scoreboards: the rollover leg's league, any other open bet's
ESPN_SOCCER = {"E0": "eng.1", "E1": "eng.2", "E2": "eng.3", "E3": "eng.4", "SP1": "esp.1", "I1": "ita.1", "D1": "ger.1",
               "F1": "fra.1", "N1": "ned.1", "P1": "por.1", "SC0": "sco.1", "B1": "bel.1", "T1": "tur.1", "G1": "gre.1"}


def espn_slug(league: str) -> str | None:
    """ESPN's scoreboard for a league code or a UEFA cup's key, if it has one."""
    if league in ESPN_SOCCER:
        return ESPN_SOCCER[league]
    k = (league or "").lower()
    return ("uefa.champions" if "champs_league" in k or k == "ucl" else "uefa.europa.conf" if "conference" in k or k == "uecl"
            else "uefa.europa" if "europa_league" in k or k == "uel" else None)
MIN_CREDITS = 40
FRESH_MIN_CREDITS = 100        # the pre-kick-off reads stop here, keeping the rest of the month for the daily read
FRESH_FROM_H, FRESH_TO_H, FRESH_EVERY_H = 0.5, 3, 2      # read again 30 min to 3 h before kick-off, a league every 2 h
CODES_AN_HOUR = 30
PUBLIC_WHY = "no code right now: try again in a minute"
CODE_WAIT_S = 180            # a code not back from the laptop by then: the collector loop isn't running
VETS_AN_HOUR = 30            # tickets loaded from SportyBet for the Vet, an hour
VET_KEEP_S = 1800            # a loaded ticket is kept this long (its odds are SportyBet's at loading)
BOOKING_CODE = re.compile(r"[A-Z0-9]{4,12}")


class BetsService:
    def __init__(self, runtime, path=None, odds_path=None, sharp_path=None):
        self.rt = runtime
        self.sharp_path = Path(sharp_path) if sharp_path else None
        self.book = B.Book(path)
        self.lanes = {k: B.Book(Path(path).with_name(f"bets_{k}.json") if path else None) for k in B.LANES}   # friends' rollovers
        self.odds_path = Path(odds_path) if odds_path else None
        self.books: dict[str, dict] = self._load_odds()
        self.history: list[dict] = []
        self.cal: dict = {}
        self.teams: B.Teams | None = None
        self.methods: dict = {}                               # each margin method's fit to three seasons of results
        self.study: dict = {}                                 # the rules replayed on three seasons (engine/betstudy.py)
        self.combo_past: dict = {}                            # the combo builder replayed on them (engine/combos.py)
        self.corners = None                                   # {stat: (count model, each league's spread)}: engine/corners.py
        self.models: dict = {}                                # the goals and corners models on past games
        self._combos: tuple | None = None                     # (board, {days: {book: Solver}})
        self.sharp_fd: list[dict] = []
        self.sharp_oa: list[dict] = []
        self.credits: int | None = None
        self.intl: dict[str, tuple[str, str]] = {}            # international competitions in season: {sport key: (title, key)}
        self.cups: dict[str, tuple[str, str]] = dict(F.CUPS)  # the European cups: {code: (name, sport key)}
        self._fresh_at: dict[str, int] = {}                   # when each league last had a pre-kick-off read
        self._scores_at: dict[str, float] = {}                # when each league's results were last asked for
        self._espn_cache: dict[tuple, tuple[float, list]] = {}  # (ESPN league, day) -> (asked at, final scores)
        self.status: dict[str, dict] = {k: {"ok": None, "at": None, "error": None} for k in ("history", "fixtures", "sharp", "book")}
        self._board: tuple[float, int, list] | None = None    # (built at, generation, rows)
        self._gen = 0                                          # bumped whenever odds, prices or history change
        self._sharp_rows: tuple[int, list] | None = None
        self._sharp_v = 0
        self._lock: asyncio.Lock | None = None
        self._rebuild_task: asyncio.Task | None = None
        self._match_n: dict[str, tuple] = {}
        self._tasks: list[asyncio.Task] = []
        self.codes: dict[str, dict] = {}                      # SportyBet booking codes by slip: {signature: job}
        self.vets: dict[str, dict] = {}                       # SportyBet tickets to vet, by booking code: {code: job}
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
        for lane in self.lanes.values():
            lane.save()

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
            try:
                past = C.past(history)
            except Exception as exc:                      # noqa: BLE001 - the rest of the history still stands
                log.warning("bets combo replay: %s", exc)
                past = {}
            models, corners = {}, None
            try:                                          # the goals and corners models, and how they did on past games
                models["goals"] = G.check(history, B.devig)
                if models["goals"].get("rho") is not None:
                    G.set_rho(models["goals"]["rho"])
                for stat in K.STATS:
                    models[stat] = K.check(history, stat)
                corners = {stat: K.build(history, stat) for stat in K.STATS}
            except Exception as exc:                      # noqa: BLE001
                log.warning("bets models: %s", exc)
            return scores, B.calibration(history), teams, S.study(history, teams), past, models, corners
        (self.methods, self.cal, self.teams, self.study, self.combo_past, self.models,
         self.corners) = await asyncio.to_thread(crunch)
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
                    and x["edge"] >= B.ROLL_EDGE and x["market"] in ("1", "2", "1X", "12", "X2")    # only win prices are re-read
                    and now - self._fresh_at.get(x["league"], 0) > FRESH_EVERY_H * B.HOUR):
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
        """Refresh the sharp price of games about to start, book new flat value bets, settle what has finished, then
        book the next rollover leg (as many a day as fit: the next one as soon as the last has settled)."""
        now = int(time.time() * 1000)
        try:
            await self._pre_kickoff(now)
        except Exception as exc:                          # noqa: BLE001 - the day's read still stands
            log.warning("bets fresh read: %s", scrub(exc))
        board = await self.board_async(now)
        for x in B.value_bets(board, before_ms=now + B.BOOK_DAYS * B.DAY):
            self.book.add_flat(x, now)
        await self._settle(now)
        day = datetime.fromtimestamp(now / 1000, LAGOS).strftime("%Y-%m-%d")
        self._roll(self.book, (B.ROLL_MIN, B.ROLL_MAX), board, now, day)
        for k, lane in self.lanes.items():
            self._roll(lane, B.LANES[k]["range"], board, now, day, slip=True)
            lane.save()
        self.book.save()

    def _next(self, book: B.Book, rng: tuple, board: list[dict], now: int, slip: bool = False) -> dict | None:
        """The next leg's candidate: a single game for the main rollover, a slip of today's games for a friend's."""
        lo, hi = rng
        if slip:
            return C.roll_slip(board, dict(self.books), now, lo, hi)
        last = book.last_pick()
        return B.rollover_pick([x for x in board if not last or x["t_ms"] > last["t_ms"]], now, lo=lo, hi=hi)

    def _roll(self, book: B.Book, rng: tuple, board: list[dict], now: int, day: str, slip: bool = False) -> None:
        """One rollover's next leg: an open leg booked under another odds range (or as a single game where a slip is
        wanted) is replaced before kick-off; once the last leg has settled, the next one."""
        lo, hi = rng
        gone = book.replace_open(now, lambda p: lo <= p["odds"] <= hi and bool(p.get("legs")) == slip)
        if gone:
            log.info("bets rollover: %s v %s at %.2f replaced under the new rules", gone["home"], gone["away"], gone["odds"])
        if not book.open_pick():
            pick = self._next(book, rng, board, now, slip)
            if pick:
                book.add_pick(pick, day, now)

    async def _espn(self, open_: list[dict], roll: set, now: int) -> list[dict]:
        """Final scores from ESPN's free scoreboards (no credits) for the open bets' leagues and days: the rollover
        leg's league every few minutes, any other every half hour; the last answer kept in between."""
        from ingestion import intel as I
        want: dict = {}
        for b in open_:
            slug = espn_slug(b["league"])
            if slug:
                for shift in (0, -5 * B.HOUR):                # ESPN's days are US time: the kick-off's date both ways
                    d = datetime.fromtimestamp((b["t_ms"] + shift) / 1000, timezone.utc).strftime("%Y%m%d")
                    want[(slug, d)] = min(want.get((slug, d), ESPN_OTHER_S), ESPN_ROLL_S if b["league"] in roll else ESPN_OTHER_S)
        out = []
        for (slug, d), every in want.items():
            at, got = self._espn_cache.get((slug, d), (0.0, []))
            if time.time() - at > every:
                try:
                    games = await I.fetch_scoreboard("soccer", slug, d)
                    got = [{"home": g["home"]["team"].get("displayName") or "", "away": g["away"]["team"].get("displayName") or "",
                            "t_ms": g["start_ms"], "hg": g["home_score"], "ag": g["away_score"]}
                           for g in games if g.get("completed") and g.get("start_ms") and g.get("home_score") is not None
                           and g.get("away_score") is not None]
                    self._espn_cache[(slug, d)] = (time.time(), got)
                except Exception as exc:                  # noqa: BLE001 - The Odds API and football-data still settle it
                    log.info("bets ESPN scores %s %s: %s", slug, d, exc)
            out += got
        if len(self._espn_cache) > 200:
            self._espn_cache = dict(list(self._espn_cache.items())[-100:])
        return out

    async def _settle(self, now: int) -> None:
        # a game is over about 1h55 after kick-off. ESPN's free scoreboards are read first (the rollover leg's league
        # every few minutes, so the next leg isn't kept waiting); The Odds API's scores (two credits a league) only for
        # what ESPN didn't settle: the rollover's league every 15 minutes, any other at most once an hour
        legs = [*self.book.picks]
        for lane in self.lanes.values():                  # a friend's slip: each of its games still open
            for p in lane.picks:
                legs += [g for g in p.get("legs") or [] if p["status"] == "open" and g["status"] == "open"] or \
                    ([p] if not p.get("legs") else [])
        open_ = [b for b in [*self.book.flat, *legs] if b["status"] == "open" and b["t_ms"] < now - ROLL_DONE_MS]
        roll = {b["league"] for b in legs if b["status"] == "open"}
        results = [g for g in self.history if g.get("hg") is not None and g["t_ms"] > now - 10 * B.DAY]
        if open_:
            results += await self._espn(open_, roll, now)
            open_ = [b for b in open_ if not B.match_fixture(b["home"], b["away"], b["t_ms"], results, within_h=30)]
        if open_ and os.getenv("ANALYTIX_ODDS_API_KEY") and (self.credits is None or self.credits >= MIN_CREDITS // 2):
            sport = lambda k: self._sport(k) or ((k, k) if k.startswith("soccer_") else None)
            leagues = {b["league"] for b in open_ if sport(b["league"])
                       and time.time() - self._scores_at.get(b["league"], 0) > (ROLL_SCORES_S if b["league"] in roll else 3600)}
            try:
                for k in leagues:                         # two credits a read
                    self._scores_at[k] = time.time()
                got, left, _errors = await self.fetch_odds_api({k: sport(k) for k in leagues}, "scores") if leagues else ({}, None, [])
                self.credits = left if left is not None else self.credits
                for events in got.values():
                    results += F.parse_scores(events)
            except Exception as exc:                      # noqa: BLE001 - football-data's results still settle it
                log.warning("bets scores: %s", exc)
        closing = [{"home": g["home"], "away": g["away"], "t_ms": g["t_ms"], "fair": B.devig(
                    {"1": g.get("PSCH") or g.get("AvgCH"), "X": g.get("PSCD") or g.get("AvgCD"), "2": g.get("PSCA") or g.get("AvgCA")}),
                    "ou": B.devig({"O25": g.get("PC>2.5") or g.get("AvgC>2.5"), "U25": g.get("PC<2.5") or g.get("AvgC<2.5")})}
                   for g in self.history if g["t_ms"] > now - 10 * B.DAY and (g.get("PSCH") or g.get("AvgCH") or g.get("PC>2.5"))]
        self.book.settle(results, closing)
        for lane in self.lanes.values():
            lane.settle(results, closing)

    # ── odds from the laptop ──────────────────────────────────────────────
    @staticmethod
    def _check(token: str, public: bool = False) -> None:
        """The collector's token. Through the public link the server must have one set: without it the push and the
        code queue only answer through the SSH tunnel."""
        want = os.getenv("ANALYTIX_BETS_TOKEN", "")
        if public and not want:
            raise PermissionError("set ANALYTIX_BETS_TOKEN on the server (bash scripts/run.sh token) to send through the link")
        if want and not hmac.compare_digest(str(token or "").encode(), want.encode()):
            raise PermissionError("wrong or missing X-Bets-Token")

    async def push(self, payload: dict, token: str = "", public: bool = False) -> dict:
        self._check(token, public)
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
            m = B.match_fixture(g["home"], g["away"], g["t_ms"], out, within_h=12)
            if not m:
                out.append(g)
            elif g.get("ou_odds") and not m.get("ou_odds"):     # The Odds API's win prices, football-data's over/under
                i = next(k for k in range(len(out)) if out[k] is m)
                out[i] = {**m, **{k: g[k] for k in ("ou_odds", "ou_src", "ou_read_ms")}}
        self._sharp_rows = (v, out)
        return out

    def _fresh(self) -> bool:
        return bool(self._board) and self._board[1] == self._gen and time.time() - self._board[0] < 60

    def _rebuild(self, now: int) -> list[dict]:
        gen = self._gen
        b = B.build(dict(self.books), self.sharp(), self.history, self.cal, now, self.teams, self.corners)
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
        out = {**self.public(board), "public": public}
        if public:                                        # past picks and their results: on the laptop only
            for r in (out["rollover"], *out["lanes"]):
                r["history"] = []
        return out

    async def _solvers(self, days: int, kinds: tuple | None, leagues: tuple | None):
        """The combo builder's search for a window, markets and leagues, made once a board: (solvers, cache, days, kinds,
        leagues, now)."""
        days = days if days in C.DAYS else 1
        board = await self.board_async(wait=False) if self.books else []
        now = int(time.time() * 1000)
        if not self._combos or self._combos[0] is not board:
            self._combos = (board, {})
        kept = self._combos[1]
        kinds = tuple(k for k in B.KINDS if k in kinds) if kinds else B.KINDS
        leagues = tuple(sorted(set(leagues))) if leagues else None
        if (days, kinds, leagues) not in kept:
            kept[(days, kinds, leagues)] = await asyncio.to_thread(C.solvers, board, dict(self.books), now, days, kinds, leagues)
        return kept[(days, kinds, leagues)], kept, days, kinds, leagues, now

    async def bundle(self, target: float, days: int = 1, least: float | None = None, book: str | None = None,
                     kinds: tuple | None = None, games: int | None = None, leagues: tuple | None = None, n: int = 5) -> dict:
        """The mixer bundle: n tickets at the target, no game on more than two of them (engine/combos.py's bundle)."""
        kinds = tuple(k for k in (kinds or B.KINDS) if k != "scorers") or tuple(k for k in B.KINDS if k != "scorers")
        sv, kept, days, kinds, leagues, now = await self._solvers(days, kinds, leagues)
        games = games if games in C.GAME_CAPS else None
        key = ("bundle", days, kinds, leagues, round(float(target), 4), C.snap(least), book, games, n)
        if key not in kept:
            kept[key] = await asyncio.to_thread(C.bundle, sv, target, n, least, book, games)
        return {**kept[key], "days": days, "kinds": list(kinds), "now_ms": now, "bets": sum(x.n for x in sv.values()),
                "books": sorted(k for k, x in sv.items() if x.n), "book": book}

    async def combo(self, target: float, days: int = 1, least: float | None = None, book: str | None = None,
                    kinds: tuple | None = None, games: int | None = None, leagues: tuple | None = None) -> dict:
        """The best slip for a total-odds target over the next `days` (1, 3 or 7), and the ladder of sizes: safest games
        first, or with every game at least `least` likely (0.60 to 0.90)."""
        sv, kept, days, kinds, leagues, now = await self._solvers(days, kinds, leagues)   # leagues with no games: an empty slip
        if ("leagues", days) not in kept:
            kept[("leagues", days)] = C.leagues(self._combos[0], now, days)
        known = {d["code"]: d["name"] for d in kept[("leagues", days)]}
        games = games if games in C.GAME_CAPS else None
        out = await asyncio.to_thread(C.build, sv, target, None if leagues else self.combo_past, least, book, kinds, games)
        if "scorers" in kinds:                                # players to score: a slip of their own
            sb = self.books.get("sportybet") or {}
            key = (sb.get("t_ms"), days, round(float(target), 4), games, leagues)
            if key not in kept:
                kept[key] = await asyncio.to_thread(C.scorers, dict(self.books), now, days, target, games,
                                                    tuple(known.get(lg, lg) for lg in leagues) if leagues else None)
            out["scorers"] = kept[key]
        return {**out, "days": days, "kinds": list(kinds), "models": self.models, "now_ms": now,
                "leagues": kept[("leagues", days)], "picked": list(leagues or ())}

    # ── SportyBet booking codes, made on the laptop ───────────────────────
    async def combo_code(self, target: float, days: int = 1, least: float | None = None, public: bool = False,
                         kinds: tuple | None = None, which: str = "slip", games: int | None = None,
                         leagues: tuple | None = None, tickets: int = 5, book: str | None = None) -> dict:
        """The booking code for the SportyBet slip at these settings (or its players-to-score slip, or ticket k of the
        mixer bundle: which="t<k>"): ready, being made, or why there's none (through the public link, without the
        laptop's side of it)."""
        if which.startswith("t") and which[1:].isdigit():
            got = await self.bundle(target, days, least, book, kinds, games, leagues, tickets)   # the bundle as shown
            k = int(which[1:])
            out = self._code_job(got["tickets"][k - 1] if 1 <= k <= len(got["tickets"]) else None)
        else:
            out = await self._combo_code(target, days, least, kinds, which, games, leagues)
        return out if not public else {**out, "why": PUBLIC_WHY if out.get("why") else None}

    async def lane_code(self, lane: str, public: bool = False) -> dict:
        """The booking code for a friend's rollover slip (the open one, else the next one), when it's on SportyBet."""
        book = self.lanes.get(lane)
        if book is None:
            return {"status": "none", "why": "no such rollover"}
        s = book.open_pick()
        if s is None:
            board = await self.board_async(wait=False) if self.books else []
            s = self._next(book, B.LANES[lane]["range"], board, int(time.time() * 1000), slip=True)
        out = self._code_job(s)
        return out if not public else {**out, "why": PUBLIC_WHY if out.get("why") else None}

    def _code_job(self, s: dict | None) -> dict:
        """A booking-code job for a SportyBet slip ({legs, n, odds, last_ms}): ready, being made, or why there's none."""
        if not s or s.get("book") != "sportybet":
            return {"status": "none", "why": "no SportyBet slip right now"}
        if not all((x.get("sel") or {}).get("event") for x in s["legs"]):
            return {"status": "none", "why": "the collector on the laptop hasn't sent SportyBet's ids yet: update it"}
        sel = [{"eventId": x["sel"]["event"], "marketId": x["sel"]["market"], "specifier": x["sel"]["specifier"] or None,
                "outcomeId": x["sel"]["outcome"]} for x in s["legs"]]
        sig = "|".join(sorted(f"{x['eventId']}/{x['marketId']}/{x['specifier'] or ''}/{x['outcomeId']}" for x in sel))
        now = time.time()
        self._expire_codes(now)
        job = self.codes.get(sig)
        if job:
            return self._job_view(job)
        if sum(1 for j in self.codes.values() if now - j["asked"] < 3600) >= CODES_AN_HOUR:
            return {"status": "none", "why": f"{CODES_AN_HOUR} codes asked for in the last hour: try again later"}
        job = {"id": sig, "selections": sel, "status": "waiting", "asked": now, "games": len(s["legs"]), "odds": s["odds"],
               "until_ms": s.get("last_ms") or max(x["t_ms"] for x in s["legs"])}
        self.codes[sig] = job
        return self._job_view(job)

    async def _combo_code(self, target: float, days: int, least: float | None, kinds: tuple | None = None,
                          which: str = "slip", games: int | None = None, leagues: tuple | None = None) -> dict:
        if which == "scorers":
            kinds = tuple(dict.fromkeys((*(kinds or ()), "scorers")))
        out = await self.combo(target, days, least, "sportybet", kinds, games, leagues)
        s = (out.get("scorers") or {}).get("slip") if which == "scorers" else out["slip"]
        if not s:
            return {"status": "none", "why": "no SportyBet slip at these settings"}
        return self._code_job(s)

    def _expire_codes(self, now: float) -> None:
        for k, j in list(self.codes.items()):
            if j["status"] == "waiting" and now - j["asked"] > CODE_WAIT_S:
                j.update(status="failed", why="the laptop didn't answer: is the collector running with --loop?")
            end = (j.get("deadline_ms") or j["until_ms"]) / 1000
            if now > end or (j["status"] == "failed" and now - j["asked"] > CODE_WAIT_S + 60):
                del self.codes[k]

    @staticmethod
    def _job_view(j: dict) -> dict:
        return {k: j.get(k) for k in ("status", "code", "url", "deadline_ms", "missing", "games", "odds", "why")}

    # ── the Vet: a SportyBet ticket, loaded on the laptop, checked against the board ──
    def _expire_vets(self, now: float) -> None:
        for k, j in list(self.vets.items()):
            if j["status"] == "waiting" and now - j["asked"] > CODE_WAIT_S:
                j.update(status="failed", why="the laptop didn't answer: is the updated collector running with --loop?", laptop=True)
            if now - j["asked"] > VET_KEEP_S or (j["status"] == "failed" and now - j["asked"] > CODE_WAIT_S + 60):
                del self.vets[k]

    async def vet(self, code: str, kinds: tuple | None = None, leagues: tuple | None = None, public: bool = False) -> dict:
        """A SportyBet ticket by its booking code, game by game against the board (engine/vet.py): being loaded, or
        ready; swaps come from these markets and leagues."""
        code = (code or "").strip().upper()
        if not BOOKING_CODE.fullmatch(code):
            return {"status": "none", "why": "That isn't a SportyBet booking code (4 to 12 letters and digits)."}
        now = time.time()
        self._expire_vets(now)
        j = self.vets.get(code)
        if j is None:
            if sum(1 for x in self.vets.values() if now - x["asked"] < 3600) >= VETS_AN_HOUR:
                return {"status": "none", "why": f"{VETS_AN_HOUR} tickets vetted in the last hour: try again later."}
            j = self.vets[code] = {"id": code, "status": "waiting", "asked": now}
        if j["status"] != "ready":
            why = PUBLIC_WHY.replace("code", "answer") if public and j.get("laptop") else j.get("why")
            return {"status": j["status"], "code": code, "why": why}
        kinds = tuple(k for k in (kinds or B.KINDS) if k != "scorers") or tuple(k for k in B.KINDS if k != "scorers")
        sv, kept, _, kinds, leagues, now_ms = await self._solvers(7, kinds, leagues)
        key = ("vet", code, j["loaded"], kinds, leagues)
        if key not in kept:
            pool = sv["sportybet"].bets if "sportybet" in sv else []
            kept[key] = await asyncio.to_thread(V.vet, j["legs"], self._combos[0], pool, now_ms)
        sb = self.books.get("sportybet") or {}
        return {"status": "ready", "code": code, **kept[key], "loaded_ms": int(j["loaded"] * 1000),
                "deadline_ms": j.get("deadline_ms"), "fresh": bool(sb) and (now_ms - sb["t_ms"]) / B.HOUR <= B.ODDS_FRESH_H}

    async def vet_code(self, code: str, kinds: tuple | None = None, leagues: tuple | None = None, public: bool = False) -> dict:
        """The booking code for a vetted ticket: its kept games and its fixes."""
        got = await self.vet(code, kinds, leagues, public)
        if got.get("status") != "ready":
            return {"status": "none", "why": "the ticket isn't loaded yet"}
        v = got["vetted"]
        s = {"book": "sportybet", "legs": v["legs"], "odds": v["odds"], "last_ms": max(x["t_ms"] for x in v["legs"])} if v["legs"] else None
        out = self._code_job(s)
        return out if not public else {**out, "why": PUBLIC_WHY if out.get("why") else None}

    def vet_jobs(self, token: str = "", public: bool = False) -> list[dict]:
        """For the collector: the booking codes waiting to be loaded from SportyBet."""
        self._check(token, public)
        self._expire_vets(time.time())
        return [{"id": j["id"], "code": j["id"]} for j in self.vets.values() if j["status"] == "waiting"]

    def vet_done(self, payload: dict, token: str = "", public: bool = False) -> dict:
        """From the collector: a ticket's games as SportyBet sent them ({id, legs, deadline_ms}) or {id, error}."""
        self._check(token, public)
        j = self.vets.get(str(payload.get("id") or "").upper())
        if not j:
            return {"ok": False}
        legs = [self._vet_leg(x) for x in (payload.get("legs") or [])[:60] if isinstance(x, dict)]
        if legs and all(x["event"] for x in legs):
            j.update(status="ready", legs=legs, loaded=time.time(),
                     deadline_ms=int(payload["deadline_ms"]) if str(payload.get("deadline_ms") or "").isdigit() else None)
        else:
            j.update(status="failed", why=f"SportyBet said: {str(payload.get('error') or 'no games on that code')[:160]}")
        return {"ok": True}

    @staticmethod
    def _vet_leg(x: dict) -> dict:
        def num(v, f=float, d=0):
            try:
                return f(v)
            except (TypeError, ValueError):
                return d
        return {"event": str(x.get("event") or "")[:40], "market": str(x.get("market") or "")[:12],
                "specifier": str(x.get("specifier") or "")[:80], "outcome": str(x.get("outcome") or "")[:80],
                "odds": num(x.get("odds")), "active": num(x.get("active"), int, 1), "t_ms": num(x.get("t_ms"), int),
                **{k: str(x.get(k) or "")[:n] for k, n in (("home", 60), ("away", 60), ("league", 80), ("market_desc", 60),
                                                            ("desc", 80), ("status", 20))}}

    def code_jobs(self, token: str = "", public: bool = False) -> list[dict]:
        """For the collector: the slips waiting for a code."""
        self._check(token, public)
        self._expire_codes(time.time())
        return [{"id": j["id"], "selections": j["selections"]} for j in self.codes.values() if j["status"] == "waiting"]

    def code_done(self, payload: dict, token: str = "", public: bool = False) -> dict:
        """From the collector: SportyBet's answer for a slip ({id, code, url, deadline_ms, missing} or {id, error})."""
        self._check(token, public)
        j = self.codes.get(str(payload.get("id") or ""))
        if not j:
            return {"ok": False}
        if payload.get("code"):
            j.update(status="ready", code=str(payload["code"])[:20], url=str(payload.get("url") or "")[:300] or None,
                     deadline_ms=int(payload["deadline_ms"]) if str(payload.get("deadline_ms") or "").isdigit() else None,
                     missing=[str(m)[:120] for m in (payload.get("missing") or [])][:20])
        else:
            j.update(status="failed", why=f"SportyBet said: {str(payload.get('error') or 'no code')[:160]}")
        return {"ok": True}

    def _roll_view(self, book: B.Book, rng: tuple, board: list[dict], now: int, day: str, slip: bool = False) -> dict:
        """A rollover card's data: the open leg, or the candidate for the next one, the chain and its history."""
        lo, hi = rng
        todays = [p for p in book.picks if p["day"] == day and p["status"] != "replaced"]
        open_pick = book.open_pick()
        candidate = None
        if not open_pick and book.chain["refills"] <= B.ROLL_REFILLS:
            candidate = self._next(book, rng, board, now, slip)
        today = open_pick or (todays[-1] if todays and not candidate else None)
        chain = book.chain
        p_now = (today or candidate or {}).get("p")
        left = B.ROLL_TARGET - chain["day"]
        return {"today": today, "candidate": candidate, "chain": chain, "target": B.ROLL_TARGET, "range": [lo, hi],
                "today_legs": len(todays), "today_won": sum(1 for p in todays if p["status"] == "won"),
                "misses": B.rollover_misses(board, now, lo=lo, hi=hi) if not (today or candidate or slip) else [], "slip": slip,
                "refills_max": B.ROLL_REFILLS, "start": B.ROLL_START,
                "p_finish": B.chain_odds(p_now, left) if p_now else None, "days_left": left,
                "history": book.picks[-30:][::-1]}

    def public(self, board: list[dict] | None = None) -> dict:
        now = int(time.time() * 1000)
        if board is None:
            board = self.board(now) if self.books else []
        day = datetime.fromtimestamp(now / 1000, LAGOS).strftime("%Y-%m-%d")
        pats = []
        for kind in ("sharp", "avg"):
            for m in ("1", "2", "1X", "X2"):
                for i, (lo, hi) in enumerate(B.BUCKETS):
                    c = self.cal.get((kind, "*", m, i))
                    if c and c[0] >= 100:
                        pats.append({"kind": kind, "market": m, "name": B.MARKET_NAME[m], "odds": [round(1 / hi, 2), round(1 / lo, 2)],
                                     "n": c[0], "rate": c[1] / c[0], "implied": c[2] / c[0]})
        return {"now_ms": now, "enabled": self.rt.mode == "live",
                "value": [{**x, "rule": B.rule_of(x)} for x in B.value_bets(board)[:60]],
                "board_n": sum(1 for x in board if x["market"] in B.CORE),
                "book_days": B.BOOK_DAYS, "show_days": B.SHOW_DAYS,
                "matched": {k: self._matched(k) for k in self.books},
                "next_kick": min((x["t_ms"] for x in board), default=None),
                "near": [{**x, "rule": B.rule_of(x)} for x in board if B.FLAT_MIN <= x["odds"] <= B.FLAT_MAX and x["market"] != "X"
                         and x["market"] in B.CORE][:40],
                "rollover": self._roll_view(self.book, (B.ROLL_MIN, B.ROLL_MAX), board, now, day),
                "lanes": [{"key": k, "name": v["name"], **self._roll_view(self.lanes[k], v["range"], board, now, day, slip=True)}
                          for k, v in B.LANES.items()],
                "record": self.book.record(), "flat_recent": self.book.flat[-40:][::-1], "patterns": pats,
                "method": B.method(), "methods": self.methods, "study": self.study,
                "settings": {"flat": [B.FLAT_MIN, B.FLAT_MAX, B.FLAT_EDGE, B.FLAT_STAKE], "flat_low": B.FLAT_EDGE_LOW,
                             "edge_max": B.EDGE_MAX, "lower": sorted(B.LOWER_LEAGUES), "goals": list(B.GOALS),
                             "roll": [B.ROLL_MIN, B.ROLL_MAX, B.ROLL_EDGE]},
                "sources": {"books": {k: {"t_ms": v["t_ms"], "games": len(v["rows"])} for k, v in self.books.items()},
                            "history_games": len(self.history), "sharp_oa": len(self.sharp_oa), "sharp_fd": len(self.sharp_fd),
                            "credits": self.credits, "status": self.status}}

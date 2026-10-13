"""
Bot service, shadow mode: runs every rule on the live timeframe cards, in both styles (market and limit orders),
against every book update and trade print since the last tick. Nothing is sent to an exchange. The maths and fill
rules live in engine/bot.py.

    every second   new book updates and trades → fills, stops, targets, liquidations; then each rule's card →
                   time limits, flips, new entries; and the auto hunter's setups on every coin (engine/hunter.py) →
                   new entries for every setup × coin × exit profile that fired
    every Binance quote   Binance leads, checked the moment Binance moves rather than up to a second later
    the hunter's rules    signals the same way on a coin within 10s are one event; follow-through is measured from
                   the price you'd pay (ask / bid half a second later), not the mid; an entry is skipped when the
                   spread is over 2x usual, or when the setup's record on that coin shows it doesn't clear the fees
                   at that hold; timed exits carry a stop three normal moves away
    on the hour    a Telegram summary per rule that asked for one, and one for the auto hunter (live mode)
    storage        rules in data/bot.json; closed trades in the bars SQLite file (live mode), so the track record
                   survives restarts. Positions open during a restart are dropped.
"""
import asyncio
import json
import logging
import math
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path

from config import BOT_FILE, WINDOWS
from engine.bot import (
    LATENCY_MS, MAKER_FEE, STYLES, TAKER_FEE, Rule, Sim, break_even_win_rate, card_signal, liq_distance, stats,
)
from engine.hunter import (
    BENCH_MIN_TRADES, BENCH_T, DEFAULT_EXITS, EXIT_WORDS, EXITS, MAX_BREAK_EVEN, SETUPS, TIMED, VOL_STOP_K, CoinMemory,
    detect, lead_check, vol_stop_bps,
)
from engine.summary import layout_for

log = logging.getLogger("analytix.bot")

TICK_S = 1.0
MAX_RULES = 12
KEEP_TRADES = 5000                 # per rule and style, in memory
NO_DIRECTION = {"liquidity"}       # card rows that never lean, so can't trigger a trade
TRADING_FIELDS = ("coin", "timeframe", "signals", "min_strength", "sides", "leverage", "margin_usd", "take_profit_bps",
                  "stop_loss_bps", "max_hold_s", "exit_on_flip", "cooldown_s", "max_trades_per_hour",
                  "daily_loss_usd", "limit_wait_s")
LIMITS = {"min_strength": (0.05, 1.0), "leverage": (1, 200), "margin_usd": (1, 1e6), "take_profit_bps": (1, 2000),
          "stop_loss_bps": (1, 2000), "max_hold_s": (5, 7 * 86400), "cooldown_s": (0, 86400),
          "max_trades_per_hour": (1, 120), "daily_loss_usd": (1, 1e7), "limit_wait_s": (1, 600)}
META_EVERY_S = 3600
AUTO_VERSION = 4                   # an older saved hunter starts a fresh record on the new defaults
AUTO_STYLES = ("market", "maker_exit")      # limit entries mostly fill when the move has failed: off by default
AUTO_DEFAULT = {"on": True, "coins": [], "setups": list(SETUPS), "exits": list(DEFAULT_EXITS),
                "styles": list(AUTO_STYLES), "leverage": 20.0, "margin_usd": 100.0, "daily_loss_usd": 100.0,
                "telegram": "hourly", "benched": [], "taker_bps": 4.5, "maker_bps": 1.5, "lead_min_bps": 0.0,
                "spread_max": 2.0, "cost_gate": True, "version": AUTO_VERSION}
EVENT_GAP_S = 10                   # signals the same way on a coin this close together are one event
GATE_MIN = 30                      # a setup's record needs this many signals on a coin before it can switch entries off
PROVEN = (30, 3.0)                 # trades and t-stat to count as proven (t ≥ 3: many strategies are being tried)
PROMISING = (15, 2.0)
SPREAD_EVERY_S = 30
EDGE_MIN = 10                      # signals before a setup's edge after fees is shown
AUTO_MONEY = ("leverage", "margin_usd", "daily_loss_usd", "taker_bps", "maker_bps")   # these start a fresh record
LEAD_BUCKETS = ((2.0, 4.0, "2–4"), (4.0, 7.0, "4–7"), (7.0, 12.0, "7–12"), (12.0, 1e9, "12+"))
AUTO_COOLDOWN_S = 5
AUTO_PER_HOUR = 120
AUTO_CACHE_S = 2.0
SIGNALS_KEPT = 5000
MARKOUT_S = (10, 30, 60, 300, 900)   # how far price went the signal's way this long after it fired
MARKOUT_SAVE_S = 60


class BotError(ValueError):
    pass


class BotService:
    def __init__(self, runtime, path: Path = BOT_FILE, db_path: Path | None = None):
        self.rt = runtime
        self.path = Path(path)
        self.db_path = db_path
        self.rules: dict[str, Rule] = {}
        self.sims: dict[tuple[str, str], Sim] = {}
        self.trades: dict[tuple[str, str], list[dict]] = {}
        self.max_lev: dict[str, float] = {}
        self._cursor: dict[str, tuple[int, int]] = {}
        self._db: sqlite3.Connection | None = None
        self._tasks: list[asyncio.Task] = []
        self._hour = int(time.time() // 3600)
        self.auto = dict(AUTO_DEFAULT)
        self.auto_rules: dict[str, Rule] = {}
        self._mem: dict[str, CoinMemory] = {}
        self.signals: list[tuple] = []                          # (ms, coin, setup, side, event id), newest last
        self._open_event: dict[str, tuple[int, int, int]] = {}  # coin → (event id, side, last signal ms)
        self._event_id = 0
        self._usual_spread: dict[str, tuple[float, float | None]] = {}
        self._auto_cache: tuple[float, dict] | None = None
        self._auto_fresh = False
        self.markouts: dict[tuple[str, str, int], list[float]] = {}   # (setup, coin, s) → mid [n, Σ, Σ², wins] + paid [n, Σ, Σ², wins]
        self._pending: dict[int, deque] = {h: deque() for h in MARKOUT_S}   # s → (due ms, coin, setup, side, mid)
        self._markouts_dirty = False
        self._ticks = 0
        self._load()
        self._build_auto()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.db_path:
            try:
                self._db = sqlite3.connect(self.db_path, check_same_thread=False)
                self._db.execute("CREATE TABLE IF NOT EXISTS bot_trades (rule_id TEXT NOT NULL, style TEXT NOT NULL, "
                                 "exit_ms INTEGER NOT NULL, data TEXT NOT NULL)")
                self._db.execute("CREATE INDEX IF NOT EXISTS bot_trades_rule ON bot_trades (rule_id, style, exit_ms)")
                self._db.commit()
                if self._auto_fresh:                        # the auto hunter changed: its old record would mislead
                    self._db.execute("DELETE FROM bot_trades WHERE rule_id LIKE 'auto-%'")
                    self._db.commit()
                    log.info("auto hunter updated to version %d: starting a fresh record", AUTO_VERSION)
                for rid, style, data in self._db.execute("SELECT rule_id, style, data FROM bot_trades ORDER BY exit_ms"):
                    if rid in self.rules or rid in self.auto_rules:
                        self.trades.setdefault((rid, style), []).append(json.loads(data))
                for k, v in self.trades.items():
                    self.trades[k] = v[-KEEP_TRADES:]
                self._db.execute("CREATE TABLE IF NOT EXISTS bot_markouts2 (setup TEXT NOT NULL, coin TEXT NOT NULL, "
                                 "horizon INTEGER NOT NULL, n REAL, s REAL, s2 REAL, wins REAL, "
                                 "ne REAL, se REAL, se2 REAL, winse REAL, PRIMARY KEY (setup, coin, horizon))")
                self._db.commit()
                for setup, coin, h, *acc in self._db.execute("SELECT * FROM bot_markouts2"):
                    self.markouts[(setup, coin, int(h))] = list(acc)
            except Exception:
                log.exception("could not open the bot's trade history; it starts empty")
                self._db = None
        self._tasks = [asyncio.create_task(self._loop(), name="bot"),
                       asyncio.create_task(self._meta_loop(), name="bot-meta")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._save_markouts()
        if self._db:
            self._db.close()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick()
                self._hourly()
                self._ticks += 1
                if self._ticks % MARKOUT_SAVE_S == 0:
                    self._save_markouts()
            except Exception:
                log.exception("bot tick failed")

    async def _meta_loop(self) -> None:
        while True:
            before = dict(self.max_lev)
            for coin in {r.coin for r in self.rules.values()} | set(self._auto_coins()):
                try:
                    lev = await self.rt.planner.max_leverage(coin)
                except Exception:
                    lev = None
                if lev:
                    self.max_lev[coin] = lev
                    for sim in self.sims.values():
                        if sim.rule.coin == coin:
                            sim.max_leverage = lev
            if self.max_lev != before:
                self._build_auto()                         # cap the auto hunter's leverage at each coin's max
            await asyncio.sleep(META_EVERY_S)

    # ── the work ───────────────────────────────────────────────────────────
    def tick(self) -> list[dict]:
        closed: list[dict] = []
        auto_coins = set(self._auto_coins()) if self.auto["on"] else set()
        for coin in {s.rule.coin for s in self.sims.values()} | auto_coins:
            pipe = self.rt.pipelines.get(coin)
            if pipe is None or not pipe.state.now_ms:
                continue
            st = pipe.state
            now = st.now_ms
            sims = [s for s in self.sims.values() if s.rule.coin == coin]
            cur = self._cursor.get(coin)
            if cur and now < min(cur):                       # a replay started over: nothing open survives that
                for s in sims:
                    s.drop()
                for dq in self._pending.values():
                    dq.clear()
                cur = None
            if cur is None:
                cur = (now, now)
            span = int(min(st.books.max_seconds, max(2, (now - min(cur)) // 1000 + 2)))
            books = [b for b in st.books.window(span, now) if b.timestamp > cur[0]]
            prints = [t for t in st.trades.window(span, now) if t.timestamp > cur[1]]
            events = sorted([(b.timestamp, 0, b) for b in books] + [(t.timestamp, 1, t) for t in prints],
                            key=lambda e: (e[0], e[1]))
            full = st.book
            for ts, kind, e in events:
                for s in sims:
                    if kind == 0:
                        lv = (full.asks, full.bids) if full is not None and full.timestamp == ts else None
                        done = s.on_book(ts, e.best_bid, e.best_ask, lv)
                    else:
                        done = s.on_trade(ts, e.price)
                    if done:
                        closed.append(done)
            self._cursor[coin] = (books[-1].timestamp if books else cur[0], prints[-1].timestamp if prints else cur[1])
            last = st.books.newest
            if last is None or last.best_bid <= 0 or last.best_ask <= 0:
                continue
            funding = st.context.funding if st.context else 0.0
            for s in sims:
                if s.rule.timeframe == "auto":
                    done = s.on_tick(now, 0, last.best_bid, last.best_ask, funding)
                    if done:
                        closed.append(done)
                    continue
                ex = pipe.analyzer.latest.get(s.rule.timeframe)
                side = card_signal(ex.summary, s.rule.signals, s.rule.min_strength) if ex else 0
                done = s.on_tick(now, side, last.best_bid, last.best_ask, funding)
                if done:
                    closed.append(done)
                s.signal(now, side, last.best_bid, last.best_ask)
            if coin in auto_coins:
                mem = self._mem.setdefault(coin, CoinMemory())
                fired = detect(pipe, mem, now, float(self.auto["lead_min_bps"]), include_lead=False)
                for setup, side in fired.items():
                    if setup in self.auto["setups"]:
                        self._fire(coin, pipe, setup, side, now, last, mem)
        for t in closed:
            self._record(t)
        self._settle_markouts()
        return closed

    def on_quote(self, coin: str) -> None:
        """A Binance quote just arrived for `coin`: check Binance leads now, not at the next one-second tick."""
        if not self.auto["on"] or "lead" not in self.auto["setups"] or coin not in self._auto_coins():
            return
        pipe = self.rt.pipelines.get(coin)
        mem = self._mem.get(coin)
        if pipe is None or mem is None or not mem.primed:
            return
        st = pipe.state
        last = st.books.newest
        if not st.now_ms or last is None or last.best_bid <= 0 or last.best_ask <= 0:
            return
        side = lead_check(pipe, mem, st.now_ms, float(self.auto["lead_min_bps"]))
        if side:
            self._fire(coin, pipe, "lead", side, st.now_ms, last, mem)

    def _fire(self, coin: str, pipe, setup: str, side: int, now: int, last, mem: CoinMemory) -> None:
        """One setup fired: note it (and which event it belongs to), queue its follow-through, and enter every
        strategy on it that passes the spread and cost checks."""
        eid = self._event(coin, side, now)
        self.signals.append((now, coin, setup, side, eid))
        if len(self.signals) > SIGNALS_KEPT:
            del self.signals[: len(self.signals) - SIGNALS_KEPT]
        size = mem.strength.get("lead") if setup == "lead" else None
        for h in MARKOUT_S:
            self._pending[h].append((now + h * 1000, coin, setup, side, last.mid, size, now))
        self._auto_cache = None
        spread_ok = self._spread_ok(coin, pipe, last)
        base = pipe.analyzer.baseline
        sigma = getattr(base, "sigma_1s_bps", None) if base is not None else None
        for (rid, st), s in list(self.sims.items()):
            r = self.auto_rules.get(rid)
            if r is None or r.coin != coin or r.signals[0] != setup or not s.can_enter(now, side):
                continue
            if not spread_ok:
                s.skip("spread")
                continue
            if self.auto.get("cost_gate", True) and not self._gate_open(setup, coin, r.max_hold_s, st):
                s.skip("cost")
                continue
            timed = rid.rsplit("-", 1)[-1] in TIMED
            s.signal(now, side, last.best_bid, last.best_ask, vol_stop_bps(sigma, r.max_hold_s) if timed else None)

    def _event(self, coin: str, side: int, now: int) -> int:
        ev = self._open_event.get(coin)
        if ev is not None and ev[1] == side and now - ev[2] <= EVENT_GAP_S * 1000:
            self._open_event[coin] = (ev[0], side, now)
            return ev[0]
        self._event_id += 1
        self._open_event[coin] = (self._event_id, side, now)
        return self._event_id

    def _spread_ok(self, coin: str, pipe, last) -> bool:
        at, usual = self._usual_spread.get(coin, (0.0, None))
        if time.time() - at > SPREAD_EVERY_S:
            xs = sorted(b.spread_bps for b in pipe.state.books.window(600))
            usual = xs[len(xs) // 2] if len(xs) >= 20 else None
            self._usual_spread[coin] = (time.time(), usual)
        return usual is None or usual <= 0 or last.spread_bps <= float(self.auto.get("spread_max", 2.0)) * usual

    @staticmethod
    def _gate_h(hold_s: float) -> int:
        return max([h for h in MARKOUT_S if h <= hold_s] or [MARKOUT_S[0]])

    def _cost(self, style: str) -> float:
        t, m = float(self.auto["taker_bps"]), float(self.auto["maker_bps"])
        return {"market": 2 * t, "maker_exit": t + m, "limit": 2 * m}.get(style, 2 * t)

    def _gate_open(self, setup: str, coin: str, hold_s: float, style: str) -> bool:
        """Closed once the setup has GATE_MIN signals on this coin and, on average, price hasn't moved its way by
        more than this order type's round-trip fees by that hold (measured from the price you'd pay)."""
        acc = self.markouts.get((setup, coin, self._gate_h(hold_s)))
        if not acc or acc[4] < GATE_MIN:
            return True
        return acc[5] / acc[4] - self._cost(style) > 0

    def _settle_markouts(self) -> None:
        """Signals old enough: how far price went their way, in bps, before any fees: by the mid, and by what a
        market order paid (the ask half a second after a long signal) and would get (the bid at the horizon)."""
        for h, dq in self._pending.items():
            while dq:
                due, coin, setup, side, mid0, size, t0 = dq[0]
                pipe = self.rt.pipelines.get(coin)
                if pipe is None:
                    dq.popleft()
                    continue
                if pipe.state.now_ms < due:
                    break
                dq.popleft()
                b = pipe.state.books.latest_at(due)
                if b is None or not mid0 or b.mid <= 0:
                    continue
                move = side * (b.mid / mid0 - 1) * 1e4
                eb = pipe.state.books.latest_at(t0 + LATENCY_MS)
                paid = None
                if eb is not None and eb.best_bid > 0 and eb.best_ask > 0 and b.best_bid > 0 and b.best_ask > 0:
                    entry = eb.best_ask if side > 0 else eb.best_bid
                    out = b.best_bid if side > 0 else b.best_ask
                    paid = side * (out / entry - 1) * 1e4
                keys = [(setup, coin, h)]
                bucket = next((lab for lo, hi, lab in LEAD_BUCKETS if size is not None and lo <= size < hi), None)
                if bucket:                                     # Binance leads, by how far Binance had moved
                    keys.append((f"lead:{bucket}", "ALL", h))
                for key in keys:
                    acc = self.markouts.setdefault(key, [0.0] * 8)
                    acc[0] += 1
                    acc[1] += move
                    acc[2] += move * move
                    acc[3] += 1 if move > 0 else 0
                    if paid is not None:
                        acc[4] += 1
                        acc[5] += paid
                        acc[6] += paid * paid
                        acc[7] += 1 if paid > 0 else 0
                self._markouts_dirty = True
                self._auto_cache = None

    def _save_markouts(self) -> None:
        if not self._db or not self._markouts_dirty:
            return
        try:
            self._db.executemany("INSERT OR REPLACE INTO bot_markouts2 (setup, coin, horizon, n, s, s2, wins, ne, se, "
                                 "se2, winse) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 [(*k, *v) for k, v in self.markouts.items()])
            self._db.commit()
            self._markouts_dirty = False
        except sqlite3.Error:
            log.exception("could not save signal markouts")

    def _record(self, t: dict) -> None:
        key = (t["rule_id"], t["style"])
        lst = self.trades.setdefault(key, [])
        lst.append(t)
        if len(lst) > KEEP_TRADES:
            del lst[: len(lst) - KEEP_TRADES]
        if self._db:
            try:
                self._db.execute("INSERT INTO bot_trades (rule_id, style, exit_ms, data) VALUES (?, ?, ?, ?)",
                                 (t["rule_id"], t["style"], t["exit_ms"], json.dumps(t)))
                self._db.commit()
            except sqlite3.Error:
                log.exception("could not save a bot trade")
        self._auto_cache = None
        if t["rule_id"] in self.auto_rules:
            self._maybe_bench(key)
        rule = self.rules.get(t["rule_id"])
        sim = self.sims.get(key)
        if rule is None:                                  # the auto hunter reports hourly, not per trade
            return
        if rule.telegram == "trades":
            self._notify(rule, trade_message(rule, t))
        tripped = sim is not None and sim.stopped_for_day and sim.day_net - t["net_usd"] > -rule.daily_loss_usd
        if tripped and rule.telegram != "off":                          # this trade is the one that hit the daily limit
            self._notify(rule, stop_message(rule, t["style"], sim.day_net))

    def _hourly(self) -> None:
        hour = int(time.time() // 3600)
        if hour == self._hour:
            return
        start, end = self._hour * 3_600_000, hour * 3_600_000
        self._hour = hour
        for r in self.rules.values():
            if r.telegram != "hourly":
                continue
            msg = hourly_message(r, {st: self.trades.get((r.id, st), []) for st in STYLES}, start, end)
            if msg:
                self._notify(r, msg)
        if self.auto["on"] and self.auto["telegram"] == "hourly":
            msg = auto_hourly_message(self._auto_trades(), {k: r.name for k, r in self.auto_rules.items()}, start, end)
            if msg:
                self._notify(Rule("auto", coin="AUTO"), msg)

    def _notify(self, rule: Rule, msg: tuple[str, tuple[str, ...]]) -> None:
        if self.rt.mode != "live" or not getattr(self.rt, "alerts", None):
            return
        from engine.alerts import Alert
        title, lines = msg
        self.rt.alerts.notify(Alert(f"bot-{rule.id}-{time.time_ns()}", rule.coin, "bot", int(time.time() * 1000),
                                    "neutral", title, lines))

    # ── rules ──────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("could not read %s; the bot starts with no rules", self.path)
            return
        auto = saved.get("auto")
        if isinstance(auto, dict):
            self.auto.update({k: v for k, v in auto.items() if k in AUTO_DEFAULT})
            if (auto.get("version") or 1) < AUTO_VERSION:   # an older hunter: new defaults and a fresh record
                self._auto_fresh = True
                self.auto.update(exits=list(DEFAULT_EXITS), styles=list(AUTO_STYLES), benched=[])
            self.auto["version"] = AUTO_VERSION
        for d in saved.get("rules") or []:
            try:
                r = Rule.from_dict(d)
            except TypeError:
                continue
            self.rules[r.id] = r
            self._make_sims(r)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rules": [r.to_dict() for r in self.rules.values()], "auto": self.auto}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.exception("could not save the bot's rules")

    # ── the auto hunter ────────────────────────────────────────────────────
    def _auto_coins(self) -> list[str]:
        want = self.auto.get("coins") or []
        return [c for c in self.rt.coins if not want or c in want]

    def _auto_styles(self, r: Rule) -> list[str]:
        """The chosen order types, minus any whose target can't cover the fees (timed exits have no target)."""
        allowed = [st for st in STYLES if st in (self.auto.get("styles") or AUTO_STYLES)]
        if r.id.rsplit("-", 1)[-1] in TIMED:
            return allowed
        return [st for st in allowed if (be := break_even_win_rate(r, st)) is not None and be <= MAX_BREAK_EVEN]

    def _build_auto(self) -> None:
        """One strategy per setup × coin × exit profile × order type worth running. Benched and switched-off ones
        don't trade; strategies dropped from the settings lose their record; the rest keep theirs."""
        a = self.auto
        benched = set(a.get("benched") or [])
        rules = {}
        for coin in self._auto_coins():
            lev = min(float(a["leverage"]), self.max_lev.get(coin) or float(a["leverage"]))
            for setup in a["setups"]:
                for ex in a["exits"]:
                    tp, sl, hold, wait = EXITS[ex]
                    rid = f"auto-{setup}-{coin}-{ex}"
                    rules[rid] = Rule(rid, f"{SETUPS[setup][0]} · {coin} · {EXIT_WORDS[ex]}", coin, "auto", (setup,), 0.0,
                                      "both", lev, float(a["margin_usd"]), tp, sl, hold, False, AUTO_COOLDOWN_S,
                                      AUTO_PER_HOUR, float(a["daily_loss_usd"]), wait, "off", True,
                                      float(a["taker_bps"]) / 1e4, float(a["maker_bps"]) / 1e4)
        for rid in list(self.auto_rules):
            if rid not in rules:
                self._forget(rid)
        for rid, r in rules.items():
            old = self.auto_rules.get(rid)
            changed = old is None or old.to_dict() != r.to_dict()
            styles = self._auto_styles(r)
            for st in STYLES:
                if not a["on"] or st not in styles or f"{rid}|{st}" in benched:
                    self.sims.pop((rid, st), None)
                elif changed or (rid, st) not in self.sims:
                    self.sims[(rid, st)] = Sim(r, st, self.max_lev.get(r.coin))
        self.auto_rules = rules
        self._auto_cache = None

    def _maybe_bench(self, key: tuple[str, str]) -> None:
        """Stop a strategy that is clearly losing, so the hunter keeps going with what works."""
        lst = self.trades.get(key, [])
        if len(lst) < BENCH_MIN_TRADES:
            return
        s = stats(lst)
        tag = f"{key[0]}|{key[1]}"
        if s["avg_net_pct"] < 0 and s["t_stat"] is not None and s["t_stat"] <= BENCH_T and tag not in self.auto["benched"]:
            self.auto["benched"].append(tag)
            self.sims.pop(key, None)
            self._save()
            r = self.auto_rules.get(key[0])
            log.info("auto hunter benched %s (%s): %d trades, %+.1f%% a trade", r.name if r else key[0], key[1],
                     s["trades"], s["avg_net_pct"])

    def save_auto(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise BotError("Send the settings as an object.")
        new = {**self.auto, **{k: v for k, v in patch.items() if k in AUTO_DEFAULT}}
        new["on"] = bool(new["on"])
        new["coins"] = [c for c in self.rt.coins if c in set(new.get("coins") or [])]
        new["setups"] = [k for k in SETUPS if k in set(new.get("setups") or [])]
        new["exits"] = [k for k in EXITS if k in set(new.get("exits") or [])]
        new["styles"] = [k for k in STYLES if k in set(new.get("styles") or [])]
        new["cost_gate"] = bool(new.get("cost_gate", True))
        if not new["styles"]:
            raise BotError("Pick at least one order type.")
        if not new["setups"]:
            raise BotError("Pick at least one setup.")
        if not new["exits"]:
            raise BotError("Pick at least one exit profile.")
        for k, (lo, hi) in (("leverage", (1, 200)), ("margin_usd", (1, 1e6)), ("daily_loss_usd", (1, 1e7)),
                            ("taker_bps", (0, 10)), ("maker_bps", (-0.5, 5)), ("lead_min_bps", (0, 100)),
                            ("spread_max", (1, 20))):
            try:
                new[k] = float(new[k])
            except (TypeError, ValueError) as exc:
                raise BotError(f"{k} must be a number.") from exc
            if not lo <= new[k] <= hi:
                raise BotError(f"{k} must be between {lo:g} and {hi:g}.")
        if new["telegram"] not in ("off", "hourly"):
            raise BotError("telegram must be off or hourly.")
        new["benched"] = [str(x) for x in (new.get("benched") or []) if isinstance(x, str)]
        new["version"] = AUTO_VERSION
        fresh = any(new[k] != self.auto[k] for k in AUTO_MONEY)
        self.auto = new
        if fresh:
            self.reset_auto(rebuild=False)
        self._build_auto()
        self._save()
        return self.public()

    def reset_auto(self, rebuild: bool = True) -> dict:
        for rid in list(self.auto_rules):
            self._forget(rid)
        self.signals.clear()
        self.auto["benched"] = []
        self.markouts.clear()
        for dq in self._pending.values():
            dq.clear()
        self._open_event.clear()
        if self._db:
            self._db.execute("DELETE FROM bot_markouts2")
            self._db.commit()
        if rebuild:
            self._build_auto()
        self._auto_cache = None
        return self.public()

    def _auto_trades(self) -> dict[str, dict[str, list[dict]]]:
        return {rid: {st: self.trades.get((rid, st), []) for st in STYLES} for rid in self.auto_rules}

    @staticmethod
    def _cell(accs, paid: bool = True, cost: float = 0.0) -> dict:
        """Signals scored, the average move their way in bps (by the price paid, or the mid) less `cost`, how
        often it went their way, and a t-stat (from 10 signals)."""
        o = 4 if paid else 0
        n = sum(a[o] for a in accs)
        if not n:
            return {"n": 0, "avg_bps": None, "won": None, "t": None}
        s1, s2, w = sum(a[o + 1] for a in accs), sum(a[o + 2] for a in accs), sum(a[o + 3] for a in accs)
        mean = s1 / n
        var = (s2 - n * mean * mean) / (n - 1) if n > 1 else 0.0
        net = mean - cost
        return {"n": int(n), "avg_bps": net, "won": w / n,
                "t": net / math.sqrt(var / n) if n >= 10 and var > 0 else None}

    def setup_edge(self, setup: str, coin: str | None = None) -> dict | None:
        """The setup's best hold and order type by its follow-through after fees (all coins, or one)."""
        best = None
        for h in MARKOUT_S:
            accs = [v for (s, c, hh), v in self.markouts.items() if s == setup and hh == h and (coin is None or c == coin)]
            for style in [st for st in ("market", "maker_exit") if st in (self.auto.get("styles") or AUTO_STYLES)]:
                c = self._cell(accs, True, self._cost(style))
                if c["n"] and (best is None or c["avg_bps"] > best["avg_bps"]):
                    best = {**c, "h": h, "style": style}
        if best is not None and best["n"] < EDGE_MIN:           # too few to say: no number that invites a guess
            return {"n": best["n"], "learning": True}
        return best

    def auto_public(self) -> dict:
        hit = self._auto_cache
        if hit and time.time() - hit[0] < AUTO_CACHE_S:
            return hit[1]
        now_ms = max([p.state.now_ms for p in self.rt.pipelines.values()] + [0]) or int(time.time() * 1000)
        day_ago = now_ms - 86_400_000
        by_rule = self._auto_trades()
        recent = [x for x in self.signals if x[0] >= day_ago]
        per_setup = []
        for key, (label, help_) in SETUPS.items():
            fired = [x for x in self.signals if x[2] == key]
            skipped: dict[str, int] = {}
            for (rid, _), sim in self.sims.items():
                r = self.auto_rules.get(rid)
                if r is not None and r.signals[0] == key:
                    for why, n in sim.skipped.items():
                        skipped[why] = skipped.get(why, 0) + n
            closed = sorted({r.coin for rid, r in self.auto_rules.items() if r.signals[0] == key
                             for st in self._auto_styles(r) if not self._gate_open(key, r.coin, r.max_hold_s, st)})
            per_setup.append({"key": key, "label": label, "help": help_, "on": key in self.auto["setups"],
                              "events_24h": len({x[4] for x in recent if x[2] == key}),
                              "last_ms": fired[-1][0] if fired else None, "edge": self.setup_edge(key),
                              "gated_coins": closed, "skipped": skipped})
        board, benched, learning = [], set(self.auto["benched"]), 0
        for rid, d in by_rule.items():
            r = self.auto_rules[rid]
            for st in STYLES:
                if not d[st]:
                    continue
                s = stats(d[st])
                row = {"id": rid, "name": r.name, "style": st, "coin": r.coin, "setup": r.signals[0],
                       "trades": s["trades"], "win_rate": s["win_rate"], "net_usd": s["net_usd"], "gross_usd": s["gross_usd"],
                       "fees_usd": s["fees_usd"], "avg_net_pct": s["avg_net_pct"], "t_stat": s["t_stat"],
                       "verdict": s["verdict"], "benched": f"{rid}|{st}" in benched}
                t = s["t_stat"] or 0
                if s["net_usd"] > 0 and s["trades"] >= PROVEN[0] and t >= PROVEN[1]:
                    row["grade"] = "proven"
                elif s["net_usd"] > 0 and s["trades"] >= PROMISING[0] and t >= PROMISING[1]:
                    row["grade"] = "promising"
                elif s["trades"] >= PROMISING[0]:
                    row["grade"] = "losing" if s["net_usd"] < 0 else "flat"
                else:
                    row["grade"] = "learning"
                    learning += 1
                board.append(row)
        graded = sorted([b for b in board if b["grade"] in ("proven", "promising")],
                        key=lambda x: (x["grade"] != "proven", -(x["t_stat"] or 0)))
        worst = sorted([b for b in board if b["grade"] == "losing"], key=lambda x: x["net_usd"])[:3]
        mids = {c: (p.state.books.newest.mid if p.state.books.newest else None) for c, p in self.rt.pipelines.items()}
        open_ = []
        for rid, r in self.auto_rules.items():
            for st in STYLES:
                sim = self.sims.get((rid, st))
                o = sim.open_view(mids.get(r.coin)) if sim else None
                if o and not o.get("waiting"):
                    open_.append({**o, "name": r.name, "coin": r.coin})
        trades_24h = sum(1 for d in by_rule.values() for lst in d.values() for t in lst if t["exit_ms"] >= day_ago)
        out = {**self.auto, "now_ms": now_ms, "coins_on": self._auto_coins(), "all_coins": list(self.rt.coins),
               "setups_all": [{"key": k, "label": v[0], "help": v[1]} for k, v in SETUPS.items()],
               "exits_all": [{"key": k, "label": EXIT_WORDS[k], "target_bps": v[0], "stop_bps": v[1], "hold_s": v[2],
                              "timed": k in TIMED} for k, v in EXITS.items()],
               "styles_all": [{"key": k, "label": STYLE_WORDS[k]} for k in STYLES],
               "strategies": sum(len(self._auto_styles(r)) for r in self.auto_rules.values()),
               "running": sum(1 for (rid, _) in self.sims if rid in self.auto_rules),
               "benched_n": len(benched), "bench_min_trades": BENCH_MIN_TRADES, "vol_stop_k": VOL_STOP_K,
               "gate_min": GATE_MIN, "proven_rule": PROVEN, "promising_rule": PROMISING,
               "day": {"events": len({x[4] for x in recent}), "signals": len(recent), "trades": trades_24h},
               "total": {st: stats([t for d in by_rule.values() for t in d[st]]) for st in STYLES},
               "setup_stats": per_setup, "graded": graded[:12], "worst": worst, "learning": learning,
               "open": sorted(open_, key=lambda o: o["entry_ms"], reverse=True)[:20],
               "signals": [{"ms": x[0], "coin": x[1], "setup": SETUPS[x[2]][0], "side": x[3], "event": x[4]}
                           for x in self.signals[-12:][::-1]],
               "markouts": self.markout_table(), "markout_s": list(MARKOUT_S), "lead_sizes": self.lead_size_table(),
               "costs": {st: self._cost(st) for st in STYLES}}
        self._auto_cache = (time.time(), out)
        return out

    def markout_table(self) -> list[dict]:
        """Per setup and horizon: the average move the signal's way by the price paid (before fees), how often it
        went its way and a t-stat; per coin too."""
        out = []
        coins = sorted({c for s, c, _ in self.markouts if ":" not in s})
        for key, (label, _) in SETUPS.items():
            if key not in self.auto["setups"]:
                continue
            out.append({"key": key, "label": label,
                        "all": [self._cell([v for (s, _, h), v in self.markouts.items() if s == key and h == hh])
                                for hh in MARKOUT_S],
                        "coins": {c: [self._cell([self.markouts[(key, c, hh)]] if (key, c, hh) in self.markouts else [])
                                      for hh in MARKOUT_S] for c in coins}})
        return out

    def lead_size_table(self) -> list[dict]:
        """Binance leads split by how far Binance had moved: does a bigger lead follow through by enough to pay?"""
        return [{"label": lab, "cells": [self._cell([self.markouts[(f"lead:{lab}", "ALL", h)]]
                                                    if (f"lead:{lab}", "ALL", h) in self.markouts else []) for h in MARKOUT_S]}
                for lo, hi, lab in LEAD_BUCKETS]

    def _make_sims(self, r: Rule) -> None:
        for style in STYLES:
            self.sims[(r.id, style)] = Sim(r, style, self.max_lev.get(r.coin))

    def save_rule(self, d: dict) -> dict:
        """Add a rule, or change one (send its id). New trading settings start a fresh track record; a new name,
        Telegram choice or on/off doesn't."""
        r = self.validate(d)
        old = self.rules.get(str(d.get("id") or ""))
        if old is None:
            if len(self.rules) >= MAX_RULES:
                raise BotError(f"Up to {MAX_RULES} rules.")
            r.id = uuid.uuid4().hex[:10]
            self.rules[r.id] = r
            self._make_sims(r)
        elif any(getattr(old, f) != getattr(r, f) for f in TRADING_FIELDS):
            self._forget(old.id)
            r.id = uuid.uuid4().hex[:10]
            self.rules = {(r.id if k == old.id else k): (r if k == old.id else v) for k, v in self.rules.items()}
            self._make_sims(r)
        else:
            old.name, old.telegram, old.on = r.name, r.telegram, r.on
            r = old
        if not r.on:
            for st in STYLES:
                self.sims[(r.id, st)].drop()
        self._save()
        return self.public()

    def delete_rule(self, rule_id: str) -> dict:
        if rule_id not in self.rules:
            raise BotError("No such rule.")
        self.rules.pop(rule_id)
        self._forget(rule_id)
        self._save()
        return self.public()

    def _forget(self, rule_id: str) -> None:
        for st in STYLES:
            self.sims.pop((rule_id, st), None)
            self.trades.pop((rule_id, st), None)
        if self._db:
            self._db.execute("DELETE FROM bot_trades WHERE rule_id = ?", (rule_id,))
            self._db.commit()

    def reset_rule(self, rule_id: str) -> dict:
        if rule_id not in self.rules:
            raise BotError("No such rule.")
        for st in STYLES:
            self.trades.pop((rule_id, st), None)
            sim = self.sims[(rule_id, st)]
            sim.drop()
            sim.missed, sim.day_net, sim.stopped_for_day, sim.entries = 0, 0.0, False, []
        if self._db:
            self._db.execute("DELETE FROM bot_trades WHERE rule_id = ?", (rule_id,))
            self._db.commit()
        return self.public()

    def validate(self, d: dict) -> Rule:
        if not isinstance(d, dict):
            raise BotError("Send the rule as an object.")
        try:
            r = Rule.from_dict({**Rule("").to_dict(), **d})
        except (TypeError, ValueError) as exc:
            raise BotError(f"Couldn't read the rule: {exc}") from exc
        if r.coin not in self.rt.coins:
            raise BotError(f"The bot covers {', '.join(self.rt.coins)}.")
        if r.timeframe not in WINDOWS:
            raise BotError(f"Timeframe must be one of {', '.join(WINDOWS)}.")
        rows = [k for k in layout_for(WINDOWS[r.timeframe]) if k not in NO_DIRECTION]
        r.signals = tuple(k for k in rows if k in set(r.signals))
        if not r.signals:
            raise BotError(f"Pick at least one of the {r.timeframe} card's rows: {', '.join(rows)}.")
        for k, (lo, hi) in LIMITS.items():
            try:
                v = float(getattr(r, k))
            except (TypeError, ValueError) as exc:
                raise BotError(f"{k} must be a number.") from exc
            if not lo <= v <= hi:
                raise BotError(f"{k} must be between {lo:g} and {hi:g}.")
            setattr(r, k, int(v) if k in ("max_hold_s", "cooldown_s", "max_trades_per_hour", "limit_wait_s") else v)
        max_lev = self.max_lev.get(r.coin)
        if max_lev and r.leverage > max_lev:
            raise BotError(f"Hyperliquid allows up to {max_lev:g}× on {r.coin}.")
        liq = liq_distance(r.leverage, max_lev) * 1e4
        if r.stop_loss_bps >= liq:
            raise BotError(f"At {r.leverage:g}× {r.coin} is liquidated about {liq:.0f} bps away, so the stop has to be closer.")
        if r.sides not in ("both", "long", "short"):
            raise BotError("sides must be both, long or short.")
        if r.telegram not in ("off", "hourly", "trades"):
            raise BotError("telegram must be off, hourly or trades.")
        r.exit_on_flip, r.on = bool(r.exit_on_flip), bool(r.on)
        r.name = (str(r.name or "").strip() or default_name(r))[:60]
        r.taker_fee, r.maker_fee = float(self.auto["taker_bps"]) / 1e4, float(self.auto["maker_bps"]) / 1e4
        return r

    # ── reads ──────────────────────────────────────────────────────────────
    def public(self) -> dict:
        out = []
        for r in self.rules.values():
            pipe = self.rt.pipelines.get(r.coin)
            last = pipe.state.books.newest if pipe else None
            mid = last.mid if last else None
            ex = pipe.analyzer.latest.get(r.timeframe) if pipe else None
            rows = [{"key": m.key, "label": m.label, "lean": m.lean.value, "bar": m.bar, "tag": m.tag, "partial": m.partial}
                    for m in (ex.summary if ex else ()) if m.key in r.signals]
            styles = {}
            for st in STYLES:
                sim = self.sims.get((r.id, st))
                trades = self.trades.get((r.id, st), [])
                styles[st] = {**stats(trades, sim.missed if sim else 0),
                              "open": sim.open_view(mid) if sim else None,
                              "stopped_for_day": bool(sim and sim.stopped_for_day),
                              "day_net_usd": sim.day_net if sim else 0.0,
                              "break_even_win_rate": break_even_win_rate(r, st)}
            out.append({**r.to_dict(), "signal_now": card_signal(ex.summary, r.signals, r.min_strength) if ex else 0,
                        "rows_now": rows, "styles": styles,
                        "liq_bps": liq_distance(r.leverage, self.max_lev.get(r.coin)) * 1e4})
        recent = sorted((t for lst in self.trades.values() for t in lst[-60:]), key=lambda t: t["exit_ms"])[-60:][::-1]
        names = {**{k: r.name for k, r in self.auto_rules.items()}, **{r.id: r.name for r in self.rules.values()}}
        return {"mode": "shadow", "rules": out, "recent": [{**t, "rule": names.get(t["rule_id"], "")} for t in recent],
                "fees": {"taker": float(self.auto["taker_bps"]) / 1e4, "maker": float(self.auto["maker_bps"]) / 1e4},
                "coins": list(self.rt.coins),
                "timeframes": {k: [x for x in layout_for(v) if x not in NO_DIRECTION] for k, v in WINDOWS.items()},
                "max_leverage": {c: self.max_lev.get(c) for c in self.rt.coins}, "max_rules": MAX_RULES,
                "auto": self.auto_public()}


# ── messages (plain words, no dashes) ────────────────────────────────────────
ROW_NAMES = {"flow": "Flow", "who": "Who", "forced": "Forced", "book": "Book", "trend": "Trend", "vwap": "Avg price",
             "positioning": "Positions", "funding": "Funding"}
REASONS = {"target": "hit the target", "stop": "hit the stop", "time": "ran out of time", "flip": "signal flipped",
           "liquidated": "was liquidated"}
STYLE_WORDS = {"market": "market orders", "limit": "limit orders", "maker_exit": "market in, limit out"}


def default_name(r: Rule) -> str:
    return f"{r.coin} {r.timeframe} " + " + ".join(ROW_NAMES.get(k, k) for k in r.signals)


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _px(x: float) -> str:
    return f"{x:,.2f}" if x < 1000 else f"{x:,.1f}"


def trade_message(r: Rule, t: dict) -> tuple[str, tuple[str, ...]]:
    side = "Long" if t["side"] > 0 else "Short"
    mins = (t["exit_ms"] - t["entry_ms"]) / 60_000
    held = f"{mins:.1f} min" if mins < 90 else f"{mins / 60:.1f} h"
    title = f"Bot (shadow) {r.name}: {side} {REASONS.get(t['reason'], t['reason'])}, {_usd(t['net_usd'])} ({t['net_pct']:+.1f}%)"
    return title, (f"With {STYLE_WORDS[t['style']]}: in at {_px(t['entry_px'])}, out at {_px(t['exit_px'])} after {held}. "
                   f"Fees {_usd(-t['fees_usd'])}.",
                   "Shadow mode: no real order was placed.")


def stop_message(r: Rule, style: str, day_net: float) -> tuple[str, tuple[str, ...]]:
    return (f"Bot (shadow) {r.name}: stopped for today with {STYLE_WORDS[style]}",
            (f"Down {_usd(day_net)} today, past the ${r.daily_loss_usd:,.0f} daily limit. It starts again at 00:00 UTC.",
             "Shadow mode: no real order was placed."))


def hourly_message(r: Rule, by_style: dict[str, list[dict]], start_ms: int, end_ms: int):
    hour = {st: [t for t in lst if start_ms <= t["exit_ms"] < end_ms] for st, lst in by_style.items()}
    if not any(hour.values()):
        return None
    lines = []
    for st in STYLES:
        h, all_ = hour.get(st, []), by_style.get(st, [])
        if not all_:
            continue
        s_all = stats(all_)
        net_h = sum(t["net_usd"] for t in h)
        lines.append(f"{STYLE_WORDS[st].capitalize()}: {len(h)} trades this hour, {_usd(net_h)}. "
                     f"Since the start: {s_all['trades']} trades, {_usd(s_all['net_usd'])} after {_usd(-s_all['fees_usd'])} "
                     f"in fees, {s_all['win_rate'] * 100:.0f}% won.")
    lines.append("Shadow mode: no real orders were placed.")
    return f"Bot (shadow) {r.name}: the last hour", tuple(lines)


def auto_hourly_message(by_rule: dict[str, dict[str, list[dict]]], names: dict[str, str], start_ms: int, end_ms: int):
    hour = [t for d in by_rule.values() for lst in d.values() for t in lst if start_ms <= t["exit_ms"] < end_ms]
    if not hour:
        return None
    lines = []
    for st in STYLES:
        h = [t for t in hour if t["style"] == st]
        lines.append(f"{STYLE_WORDS[st].capitalize()}: {len(h)} trades, {_usd(sum(t['net_usd'] for t in h))}.")
    best = None
    for rid, d in by_rule.items():
        for st, lst in d.items():
            if len(lst) >= 10:
                s = stats(lst)
                if best is None or s["net_usd"] > best[2]["net_usd"]:
                    best = (rid, st, s)
    if best:
        rid, st, s = best
        name = names.get(rid, rid)
        lines.append(f"Best so far: {name} with {STYLE_WORDS[st]}, {_usd(s['net_usd'])} over {s['trades']} trades "
                     f"({s['win_rate'] * 100:.0f}% won).")
    lines.append("Shadow mode: no real orders were placed.")
    return "Bot (shadow) auto hunter: the last hour", tuple(lines)

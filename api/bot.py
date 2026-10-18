"""
The bot: big Hyperliquid TWAPs. A TWAP buys or sells a big order in slices every 30 seconds. The big ones ($20k+ a
slice) have pushed price their way over the next 5 to 30 minutes (scripts/twapStudy.py; Runs 9 and 10 in the
findings). The bot spots them live, shows them while they run, scores every one, and, with alerts on, tells you the
moment one is spotted. It is still a test: the edge was found on one week and needs a week of live results.

    spotted     api/twaptrack.py: one wallet's market orders on one coin and side, 25-35s apart, at the 4th slice,
                if the slices' median is $20k+
    what to do  the TWAP's side within FILL_S seconds, closed after your hold (5, 15 or 30 min) or when the TWAP
                stops (no slice for 35s; the bot says so)
    the record  every TWAP settled from the live book: in at the mid FILL_S after it was spotted, out after 5, 15 and
                30 min and when it stopped (at most an hour), with the worst the best bid/ask went against you (so
                busts show at any leverage). A shadow balance takes the TWAPs you could (one at a time) at your
                leverage, margin and hold
    storage     settings and the shadow balance in data/bot.json; settled TWAPs in the bars SQLite file (bot_twaps)
"""
import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path

from api import livestate
from config import BOT_FILE, HL_INFO_URL
from api.twaptrack import END_AFTER_MS, END_CAP_S, HOLDS_S, LAG_S as FILL_S, MIN_SLICE_USD, TwapTracker
from engine.bigmovers import mid_near, pt_of
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.bot")

TICK_S = 1.0
RING_KEEP_S = END_CAP_S + 5 * 60        # the longest hold is an hour
KEEP = 2000
DAY_MS = 86_400_000
LOOKUP_S = 3.0                          # the TWAP plan lookup gives up after this
STOPPED_SHOW_S = 60                     # how long "it stopped: close now" stays up
HOLDS = [*map(str, HOLDS_S), "end"]     # "300", "900", "1800", "end"
VERSION = 3                             # 3: strong TWAPs and a 15-minute hold (an older bot.json keeps its other settings)
STRONG_LEFT_USD = 250_000               # a strong TWAP still has this much to come...
STRONG_SHARE = 0.02                     # ...and that's at least this share of the coin's usual trading while it runs
LOOKUPS_PER_MIN = 20                    # early (slice 2) plan lookups, at most; well inside Hyperliquid's limits
PLAN_CACHE_S = 120

DEFAULTS = {"alerts": True, "alert_on": "strong", "telegram": False, "coins": ["BTC", "ETH"], "balance": 50.0,
            "leverage": 200.0, "margin_pct": 20.0, "hold": "900"}
ALERT_ON = ("strong", "all")
LIMITS = {"balance": (1, 1e7), "leverage": (1, 1000), "margin_pct": (1, 100)}
MONEY_KEYS = ("leverage", "hold")       # change these and the shadow restarts (they change the strategy); a new balance
                                        # or margin keeps its result (rebase) and sizes from the next trade
KNOWN_COINS = ("BTC", "ETH", "SOL", "HYPE")


class BotError(ValueError):
    pass


def hold_s(rec: dict, hold: str) -> int | None:
    """Seconds held for this TWAP at this hold setting (None while a hold-until-it-stops TWAP is still running)."""
    if hold != "end":
        return int(hold)
    return rec.get("hold_end_s")


class BotService:
    def __init__(self, runtime, path: Path = BOT_FILE, db_path: Path | None = None):
        self.rt = runtime
        self.path = Path(path)
        self.db_path = db_path
        self.cfg = dict(DEFAULTS)
        self.since_ms = int(time.time() * 1000)
        self.shadow = self._fresh_shadow()
        self.twaps = TwapTracker()
        self.ring: dict[str, dict[int, list]] = {}
        self.now_ms: dict[str, int] = {}
        self._mid: dict[str, float] = {}
        self._queue: deque = deque()              # spotted TWAPs waiting for their shadow trade, in time order
        self._told_end: set[str] = set()
        self._db: sqlite3.Connection | None = None
        self._tasks: list[asyncio.Task] = []
        self.info_url = HL_INFO_URL
        self.lookup = getattr(runtime, "mode", "") == "live"       # ask Hyperliquid for TWAP plans (live only)
        self._plans: dict[tuple, tuple] = {}                         # (wallet, coin, side) → (asked at, plan or None)
        self._asked: deque = deque()                                 # times of early lookups, for the rate cap
        self.plan_stats = {"asked": 0, "found": 0}
        self._vol: dict[str, dict[int, float]] = {}                  # coin → hour → $ traded
        self.live_path = self.path.with_name(self.path.stem + "_live.json")
        self._pauses: list[int] = []                                 # restarts (see api/livestate)
        self._saved_at = 0.0
        self._load()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.db_path:
            try:
                self._db = connect(self.db_path)
                self._db.execute("CREATE TABLE IF NOT EXISTS bot_twaps (id TEXT PRIMARY KEY, t_ms INTEGER NOT NULL, "
                                 "data TEXT NOT NULL)")
                self._db.commit()
                self.twaps.load([json.loads(d) for (d,) in self._db.execute(
                    "SELECT data FROM bot_twaps ORDER BY t_ms DESC LIMIT ?", (KEEP,))][::-1])
            except Exception:
                log.exception("could not open the bot's TWAP history; it starts empty")
                self._db = None
        self._restore()
        if self.rt.mode == "live":
            self._save()
        self._tasks = [asyncio.create_task(self._loop(), name="bot")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._flush()
        self._keep()
        if self._db:
            self._db.close()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick()
                if time.time() - self._saved_at >= livestate.SAVE_S:
                    self._keep()
            except Exception:
                log.exception("bot tick failed")

    # ── surviving a restart (api/livestate) ────────────────────────────────
    def _keep(self) -> None:
        """Save what's in flight: running and unscored TWAPs, the shadow queue, recent prices, the hourly volume."""
        if self.rt.mode != "live":
            return
        self._saved_at = time.time()
        livestate.save(self.live_path, {
            "saved_ms": max(self.now_ms.values(), default=0) or int(time.time() * 1000), "pauses": self._pauses,
            "ring": livestate.ring_dump(self.ring),
            "mid": self._mid, "vol": self._vol, "queue": [r["id"] for r in self._queue], **self.twaps.dump()})

    def _restore(self) -> None:
        saved = livestate.load(self.live_path) if self.rt.mode == "live" else None
        if not saved:
            return
        self._pauses = livestate.pauses_after(saved)
        for coin, rows in livestate.ring_load(saved.get("ring")).items():
            self.ring.setdefault(coin, {}).update(rows)
        self._mid.update({k: float(v) for k, v in (saved.get("mid") or {}).items()})
        for coin, hours in (saved.get("vol") or {}).items():
            self._vol.setdefault(coin, {}).update({int(h): float(v) for h, v in hours.items()})
        back = self.twaps.restore(saved)
        by_id = {r["id"]: r for r in self.twaps.records}
        for i in saved.get("queue") or []:
            if i in by_id:
                self._queue.append(by_id[i])
        if back:
            log.info("TWAP basket: picked up %d open TWAPs after the restart", len(back))

    # ── the live feed ──────────────────────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        ch = msg.get("channel")
        if ch == "l2Book":
            self._on_book(msg.get("data") or {})
        elif ch == "trades":
            for tr in msg.get("data") or []:
                self._on_trade(tr)

    def _on_book(self, d: dict) -> None:
        coin = d.get("coin")
        if coin not in self.cfg["coins"]:
            return
        try:
            bids, asks = d["levels"]
            bid, ask, ms = float(bids[0]["px"]), float(asks[0]["px"]), int(d["time"])
        except (KeyError, IndexError, TypeError, ValueError):
            return
        s = ms // 1000
        ring = self.ring.setdefault(coin, {})
        r = ring.get(s)
        mid = (bid + ask) / 2
        if r is None:
            ring[s] = [bid, ask, mid]
        else:
            r[0], r[1], r[2] = min(r[0], bid), max(r[1], ask), mid
        self._mid[coin] = mid
        self.now_ms[coin] = max(self.now_ms.get(coin, 0), ms)

    def _on_trade(self, tr: dict) -> None:
        coin = tr.get("coin")
        if coin not in self.cfg["coins"]:
            return
        users, side = tr.get("users") or [], tr.get("side")
        if len(users) != 2 or side not in ("B", "A"):
            return
        wallet, sd = (users[0], 1) if side == "B" else (users[1], -1)
        try:
            ms, usd = int(tr["time"]), float(tr["px"]) * float(tr["sz"])
        except (KeyError, TypeError, ValueError):
            return
        self.now_ms[coin] = max(self.now_ms.get(coin, 0), ms)
        vol = self._vol.setdefault(coin, {})
        h = ms // 3_600_000
        vol[h] = vol.get(h, 0.0) + usd
        if len(vol) > 30:
            for old in [x for x in vol if x < h - 25]:
                del vol[old]
        rec = self.twaps.on_fill(wallet.lower(), coin, sd, ms, usd)
        if rec is not None:
            self._spotted(rec)
        while self.twaps.candidates:
            self._candidate(self.twaps.candidates.pop())

    def rate(self, coin: str, now_ms: int) -> float | None:
        """The coin's usual trading, $ a second: the average of the last 24 full hours (or this hour so far)."""
        vol = self._vol.get(coin) or {}
        h = now_ms // 3_600_000
        past = [v for k, v in vol.items() if h - 24 <= k < h]
        if past:
            return sum(past) / (len(past) * 3600)
        cur = vol.get(h)
        return cur / max((now_ms % 3_600_000) / 1000, 600) if cur else None

    # ── TWAP plans from Hyperliquid ────────────────────────────────────────
    async def _fetch(self, wallet: str):
        import httpx
        async with httpx.AsyncClient(timeout=LOOKUP_S) as client:
            r = await client.post(self.info_url, json={"type": "twapHistory", "user": wallet})
            r.raise_for_status()
            return r.json()

    async def _plan_of(self, wallet: str, coin: str, side: int, start_ms: int | None) -> dict | None:
        key = (wallet, coin, side)
        got = self._plans.get(key)
        if got and time.time() - got[0] < PLAN_CACHE_S and (got[1] is not None or start_ms is None):
            return got[1]
        self.plan_stats["asked"] += 1
        try:
            plan = parse_plan(await self._fetch(wallet), coin, side, start_ms or 0, self._mid.get(coin))
        except Exception as exc:                                   # everything still works without it
            log.info("TWAP plan lookup for %s failed: %s", wallet, exc)
            plan = None
        self._plans[key] = (time.time(), plan)
        if len(self._plans) > 5000:
            cut = time.time() - PLAN_CACHE_S
            self._plans = {k: v for k, v in self._plans.items() if v[0] >= cut}
        if plan:
            self.plan_stats["found"] += 1
        return plan

    def _candidate(self, key: tuple) -> None:
        """Two $10k+ slices 30s apart: ask Hyperliquid now, so a big TWAP is caught at slice 2, not 4."""
        if not self.lookup:
            return
        now = time.time()
        while self._asked and now - self._asked[0] > 60:
            self._asked.popleft()
        if len(self._asked) >= LOOKUPS_PER_MIN:
            return
        self._asked.append(now)
        try:
            asyncio.get_running_loop().create_task(self._confirm(key))
        except RuntimeError:
            pass

    async def _confirm(self, key: tuple) -> None:
        wallet, coin, side = key
        plan = await self._plan_of(wallet, coin, side, self.twaps.run_start(key))
        if not plan or plan.get("status") not in (None, "activated") or planned_slice(plan) < MIN_SLICE_USD:
            return
        now = max(self.now_ms.values(), default=0) or int(time.time() * 1000)
        rec = self.twaps.spot(key, now)
        if rec is not None:
            rec["plan"] = plan
            self._spotted(rec, have_plan=True)

    def _spotted(self, rec: dict, have_plan: bool = False) -> None:
        rec["mid"] = self._mid.get(rec["coin"])
        rec["hold"] = self.cfg["hold"]
        rec["announced"] = False
        self._queue.append(rec)
        if have_plan or not self.lookup:
            self._decide(rec)
            return
        try:
            asyncio.get_running_loop().create_task(self._look_up(rec))
        except RuntimeError:
            self._decide(rec)

    async def _look_up(self, rec: dict) -> None:
        """Spotted at slice 4 without a plan yet: ask Hyperliquid (at most LOOKUP_S), then decide."""
        plan = await self._plan_of(rec["wallet"], rec["coin"], rec["side"], rec.get("start_ms", rec["t_ms"]))
        if plan:
            rec["plan"] = plan
        self._decide(rec)

    def _decide(self, rec: dict) -> None:
        """Strong or not, from the plan: what's still to come and its share of the coin's usual trading. Alerts
        (banner, Telegram, the shadow balance) follow your setting; every TWAP is scored either way."""
        now = max(self.now_ms.values(), default=0) or rec["t_ms"]
        rec["strength"] = strength(rec, now, self.rate(rec["coin"], now))
        rec["announced"] = bool(self.cfg["alerts"]) and (self.cfg["alert_on"] == "all" or rec["strength"]["strong"])
        if rec["announced"] and self.cfg["telegram"]:
            self._notify(spotted_message(rec, self.cfg), rec["coin"])

    # ── every second: settle, shadow trade, tell when a TWAP stops ─────────
    def tick(self) -> None:
        now = max(self.now_ms.values(), default=0)
        if now:
            self.twaps.tick(now, lambda c: self.ring.get(c, {}),
                            lambda rows, a, b: livestate.stale_for(rows, self._pauses, a, b))
        for rec in list(self._queue)[:50]:
            if (rec["announced"] and rec.get("hold") == "end" and rec["end_ms"] is not None
                    and rec["id"] not in self._told_end):
                self._told_end.add(rec["id"])
                if self.cfg["telegram"]:
                    self._notify(stopped_message(rec), rec["coin"])
        while self._queue:
            rec = self._queue[0]
            k = rec.get("hold", self.cfg["hold"])
            done = k in rec["results"]
            stale = now - rec["t_ms"] > (END_CAP_S + 300) * 1000
            if not done and not stale:
                break
            self._queue.popleft()
            self._told_end.discard(rec["id"])
            if done:
                self._shadow_trade(rec, k)
        cutoff = now // 1000 - RING_KEEP_S
        for ring in self.ring.values():
            for s in [s for s in ring if s < cutoff]:
                del ring[s]
        self._flush()

    def _shadow_trade(self, rec: dict, k: str) -> None:
        sh = self.shadow
        r = rec["results"].get(k)
        if not rec["announced"] or r is None or sh["busted"] or k != self.cfg["hold"]:
            return
        start = rec["t_ms"] + FILL_S * 1000
        if start < sh["free_ms"]:
            return                                         # still in the last trade
        margin = self.cfg["margin_pct"] / 100 * sh["start"]
        if margin > sh["balance"]:
            sh["busted"] = True
            self._save()
            return
        pt = pt_of(r, self.cfg["leverage"], rec["coin"])
        pnl = margin * pt
        sh["balance"] += pnl
        sh["trades"] += 1
        sh["wins"] += pnl > 0
        sh["busts"] += pt <= -1.0
        sh["free_ms"] = start + (hold_s(rec, k) or 0) * 1000
        sh["low"] = min(sh["low"], sh["balance"])
        if sh["balance"] < margin:
            sh["busted"] = True
        rec["shadow"] = {"pnl": round(pnl, 4), "balance": round(sh["balance"], 4), "pt": round(pt, 4)}
        if rec not in self.twaps.unsaved:
            self.twaps.unsaved.append(rec)                 # save it again with its shadow result
        self._save()

    def _flush(self) -> None:
        if not self.twaps.unsaved:
            return
        if self._db:
            try:
                with transaction(self._db):
                    self._db.executemany("INSERT OR REPLACE INTO bot_twaps (id, t_ms, data) VALUES (?, ?, ?)",
                                         [(r["id"], r["t_ms"], json.dumps(r)) for r in self.twaps.unsaved])
            except sqlite3.Error as exc:
                log.warning("could not save TWAP records (will retry): %s", exc)
                return
        self.twaps.unsaved.clear()

    def _notify(self, msg: tuple[str, tuple[str, ...]], coin: str) -> None:
        if self.rt.mode != "live" or not getattr(self.rt, "alerts", None):
            return
        from engine.alerts import Alert
        title, lines = msg
        self.rt.alerts.notify(Alert(f"bot-{time.time_ns()}", coin, "bot", int(time.time() * 1000), "neutral", title, lines))

    # ── settings ───────────────────────────────────────────────────────────
    def _fresh_shadow(self) -> dict:
        start = float(self.cfg["balance"])
        return {"start": start, "balance": start, "low": start, "trades": 0, "wins": 0, "busts": 0, "free_ms": 0,
                "busted": False, "since_ms": int(time.time() * 1000)}

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError):
            log.warning("could not read %s; using defaults", self.path)
            return
        v = saved.get("v")
        keep = {VERSION: DEFAULTS, 2: ("alerts", "telegram", "coins", "balance", "leverage", "margin_pct")}.get(
            v, ("telegram", "coins", "balance", "leverage", "margin_pct"))
        self.cfg.update({k: x for k, x in (saved.get("settings") or {}).items() if k in keep and k in DEFAULTS})
        self.shadow = self._fresh_shadow()                 # a new strategy starts a new shadow balance
        if v == VERSION:
            self.shadow.update(saved.get("shadow") or {})
        if v in (2, VERSION):
            self.since_ms = int(saved.get("since_ms") or self.since_ms)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": VERSION, "settings": self.cfg, "shadow": self.shadow,
                                       "since_ms": self.since_ms}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save the bot settings: %s", exc)

    def save_settings(self, patch: dict) -> dict:
        new = dict(self.cfg)
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                raise BotError(f"Unknown setting: {k}.")
            if k in ("alerts", "telegram"):
                new[k] = bool(v)
            elif k == "alert_on":
                if v not in ALERT_ON:
                    raise BotError("Alert on must be strong or all.")
                new[k] = v
            elif k == "coins":
                coins = [str(c).upper() for c in (v or [])]
                if not coins or any(c not in KNOWN_COINS for c in coins):
                    raise BotError(f"Coins must be some of {', '.join(KNOWN_COINS)}.")
                new[k] = coins
            elif k == "hold":
                if str(v) not in HOLDS:
                    raise BotError("Hold must be 5, 15 or 30 minutes, or until the TWAP stops.")
                new[k] = str(v)
            else:
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    raise BotError(f"{k.replace('_', ' ')} must be a number.") from None
                lo, hi = LIMITS[k]
                if not lo <= x <= hi:
                    raise BotError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
                new[k] = x
        restart = any(new[k] != self.cfg[k] for k in MONEY_KEYS)
        resize = new["balance"] != self.cfg["balance"]
        self.cfg = new
        if restart:
            self.shadow = self._fresh_shadow()
        elif resize:
            rebase(self.shadow, new["balance"], new["margin_pct"])
        self._save()
        return self.public()

    def reset_shadow(self) -> dict:
        self.shadow = self._fresh_shadow()
        self._save()
        return self.public()

    # ── what the dashboard reads ───────────────────────────────────────────
    def status(self) -> str:
        return "live" if self.cfg["alerts"] else "silent"

    def live(self) -> dict:
        """The small piece every snapshot carries, so an alert reaches every open tab within half a second."""
        now = max(self.now_ms.values(), default=0)
        active = None
        for rec in reversed(list(self._queue)[-10:]):
            if not rec["announced"]:
                continue
            k = rec.get("hold", "end")
            if k == "end":
                gone = rec["end_ms"] is not None and now > rec["end_ms"] + END_AFTER_MS + STOPPED_SHOW_S * 1000
            else:
                gone = now > rec["t_ms"] + (FILL_S + int(k)) * 1000
            if gone or now - rec["t_ms"] > (END_CAP_S + FILL_S) * 1000:
                continue
            active = {"id": rec["id"], "coin": rec["coin"], "side": rec["side"], "t_ms": rec["t_ms"], "fill_s": FILL_S,
                      "hold_s": None if k == "end" else int(k), "running": rec["end_ms"] is None,
                      "slices": rec["slices"], "usd": round(rec["usd"]), "slice_usd": round(rec["usd"] / rec["slices"]),
                      "mid": rec.get("mid"), "plan": plan_now(rec)}
            break
        return {"status": self.status(), "alert": active}

    def _move_now(self, rec: dict) -> float | None:
        """bps the mid has moved the TWAP's way since the fill (FILL_S after it was spotted), live."""
        entry = mid_near(self.ring.get(rec["coin"], {}), rec["t_ms"] // 1000 + FILL_S) or rec.get("mid")
        mid = self._mid.get(rec["coin"])
        if not entry or not mid:
            return None
        return rec["side"] * (mid / entry - 1) * 1e4

    def _row(self, rec: dict) -> dict:
        k = self.cfg["hold"]
        r = (rec.get("results") or {}).get(k)
        return {"id": rec["id"], "coin": rec["coin"], "side": rec["side"], "t_ms": rec["t_ms"],
                "wallet": rec["wallet"], "slices": rec["slices"], "usd": round(rec["usd"]),
                "slice_usd": round(rec["usd"] / max(rec["slices"], 1)), "running": rec["end_ms"] is None,
                "end_ms": rec["end_ms"], "settled": r is not None, "bps": None if r is None else round(r["bps"], 2),
                "pt": None if r is None else pt_of(r, self.cfg["leverage"], rec["coin"]),
                "move_now": None if r is not None else self._move_now(rec), "shadow": rec.get("shadow"),
                "announced": rec.get("announced", False), "plan": plan_now(rec),
                "strong": (rec.get("strength") or {}).get("strong"), "known": (rec.get("strength") or {}).get("known"),
                "share": (rec.get("strength") or {}).get("share"), "spot_slice": rec.get("spot_slice", 4)}

    def record(self, since_ms: int | None = None, group: str = "all") -> dict:
        """Per hold: how riding the spotted TWAPs did, at your leverage. group: "all", "strong", or "noplan" (no
        TWAP order on Hyperliquid for the wallet, so how much was left couldn't be known: often a trader's own
        slicing bot on the same 30s beat)."""
        lev = self.cfg["leverage"]
        keep = {"all": lambda st: True, "strong": lambda st: st.get("strong"),
                "noplan": lambda st: bool(st) and not st.get("known")}[group]
        out = {}
        for k in HOLDS:
            rs = [(r["results"][k], r["coin"]) for r in self.twaps.records
                  if (r.get("results") or {}).get(k) and (since_ms is None or r["t_ms"] >= since_ms)
                  and keep(r.get("strength") or {})]
            if not rs:
                out[k] = {"n": 0}
                continue
            b = [x["bps"] for x, _ in rs]
            pts = [pt_of(x, lev, c) for x, c in rs]
            out[k] = {"n": len(rs), "bps": sum(b) / len(b), "win": sum(v > 0 for v in b) / len(b),
                      "pt": sum(pts) / len(pts), "busts": sum(p <= -1.0 for p in pts) / len(pts)}
        return out

    def public(self) -> dict:
        now = max(self.now_ms.values(), default=0) or int(time.time() * 1000)
        recs = list(self.twaps.records)
        day = [r for r in recs if r["t_ms"] >= now - DAY_MS]
        span_h = max((now - max(now - DAY_MS, self.since_ms)) / 3.6e6, 0.25)
        running = self.twaps.running()
        rec_of = lambda since: {g: self.record(since, g) for g in ("all", "strong", "noplan")}
        return {
            "status": self.status(), "settings": self.cfg, "limits": LIMITS, "known_coins": list(KNOWN_COINS),
            "holds": HOLDS, "fill_s": FILL_S, "live": self.live(),
            "shadow": {**self.shadow, "margin": self.cfg["margin_pct"] / 100 * self.shadow["start"]},
            "today": {"spotted": len(day), "per_day": len(day) / span_h * 24, "hours": round(span_h, 1),
                      "strong": sum(1 for r in day if (r.get("strength") or {}).get("strong")),
                      "known": sum(1 for r in day if (r.get("strength") or {}).get("known"))},
            "plans": dict(self.plan_stats),
            "running": [self._row(r) for r in running][::-1],
            "recent": [self._row(r) for r in recs if r["end_ms"] is not None][-30:][::-1],
            "record": {"day": rec_of(now - DAY_MS), "week": rec_of(now - 7 * DAY_MS), "all": rec_of(None)},
            "now_ms": now,
        }


def rebase(sh: dict, start: float, margin_pct: float) -> None:
    """A new balance without losing the result so far: the profit or loss carries over in dollars, and trades are
    sized from the new balance from the next one on."""
    shift = float(start) - sh["start"]
    sh["start"], sh["balance"], sh["low"] = float(start), sh["balance"] + shift, sh["low"] + shift
    sh["busted"] = sh["balance"] < margin_pct / 100 * sh["start"]


def parse_plan(history, coin: str, side: int, start_ms: int, mid: float | None) -> dict | None:
    """From a wallet's twapHistory, the TWAP on this coin and side that started closest before start_ms (within
    two minutes after it, for clock differences). → {total and done size, minutes, start, end, total $}."""
    want = "B" if side > 0 else "A"
    best = None
    for h in history or []:
        st = (h or {}).get("state") or {}
        if st.get("coin") != coin or st.get("side") != want:
            continue
        try:
            t = int(st.get("timestamp") or h.get("time") or 0)
            t = t * 1000 if t < 1e12 else t
            sz, minutes = float(st["sz"]), float(st["minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        if t > start_ms + 120_000 or t + minutes * 60_000 < start_ms:
            continue
        if best is None or t > best[0]:
            best = (t, st, (h.get("status") or {}).get("status"))
    if best is None:
        return None
    t, st, status = best
    sz, minutes = float(st["sz"]), float(st["minutes"])
    done_sz = float(st.get("executedSz") or 0)
    done_usd = float(st.get("executedNtl") or 0)
    px = mid or (done_usd / done_sz if done_sz else None)
    return {"sz": sz, "minutes": minutes, "start_ms": t, "ends_ms": int(t + minutes * 60_000), "done_sz": done_sz,
            "done_usd": done_usd, "total_usd": sz * px if px else None, "status": status}


def planned_slice(plan: dict) -> float:
    """$ per slice the plan works out to (Hyperliquid sends one every 30 seconds)."""
    return (plan.get("total_usd") or 0) / max(plan.get("minutes", 0) * 2, 1)


def strength(rec: dict, now_ms: int, rate: float | None) -> dict:
    """Strong = $250k+ still to come and that's 2%+ of the coin's usual trading over the time left (the TWAP size
    study: weaker ones didn't move price their way)."""
    p = rec.get("plan")
    if not p or not p.get("total_usd"):
        return {"known": False, "strong": False}
    left = max(p["total_usd"] - rec["usd"], 0.0)
    left_s = max((p["ends_ms"] - now_ms) / 1000, 30.0)
    share = left / (rate * left_s) if rate else None
    return {"known": True, "strong": bool(left >= STRONG_LEFT_USD and share is not None and share >= STRONG_SHARE),
            "left_usd": round(left), "left_s": round(left_s), "share": share}


def plan_now(rec: dict) -> dict | None:
    """The plan as it stands: total $, how much is still to come (total minus what the bot has seen), when it ends."""
    p = rec.get("plan")
    if not p or not p.get("total_usd"):
        return None
    return {"total_usd": round(p["total_usd"]), "left_usd": round(max(p["total_usd"] - rec["usd"], 0)),
            "minutes": p["minutes"], "ends_ms": p["ends_ms"]}


def _usd(x: float) -> str:
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def _hold_words(k: str) -> str:
    return "when it stops (you'll get a message)" if k == "end" else f"after {int(k) // 60} min"


def spotted_message(rec: dict, cfg: dict) -> tuple[str, tuple[str, ...]]:
    side = "LONG" if rec["side"] > 0 else "SHORT"
    did = "buying" if rec["side"] > 0 else "selling"
    margin = cfg["margin_pct"] / 100 * cfg["balance"]
    px = rec.get("mid")
    p = plan_now(rec)
    plan = (f" Its plan: {_usd(p['total_usd'])} over {p['minutes']:g} min, about {_usd(p['left_usd'])} still to come, "
            f"ending {time.strftime('%H:%M', time.gmtime(p['ends_ms'] / 1000))} UTC." if p else "")
    return (f"Big TWAP: {rec['coin']} {side}",
            (f"A wallet is {did} about {_usd(rec['usd'] / rec['slices'])} every 30s ({_usd(rec['usd'])} so far).{plan}",
             f"Get in within {FILL_S}s{f' near {px:,.2f}' if px else ''}, close {_hold_words(rec.get('hold', cfg['hold']))}.",
             f"{cfg['leverage']:g}x, ${margin:,.2f} margin. Still a test."))


def stopped_message(rec: dict) -> tuple[str, tuple[str, ...]]:
    side = "long" if rec["side"] > 0 else "short"
    return (f"{rec['coin']} TWAP stopped: close your {side}",
            (f"No slice for 35s after {rec['slices']} slices ({_usd(rec['usd'])} in all).",))

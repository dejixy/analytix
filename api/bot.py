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

from config import BOT_FILE
from api.twaptrack import END_AFTER_MS, END_CAP_S, HOLDS_S, LAG_S as FILL_S, TwapTracker
from engine.bigmovers import pt_of
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.bot")

TICK_S = 1.0
RING_KEEP_S = END_CAP_S + 5 * 60        # the longest hold is an hour
KEEP = 2000
DAY_MS = 86_400_000
STOPPED_SHOW_S = 60                     # how long "it stopped: close now" stays up
HOLDS = [*map(str, HOLDS_S), "end"]     # "300", "900", "1800", "end"
VERSION = 2                             # bot.json from the big-mover bot (no version) keeps settings, not its shadow

DEFAULTS = {"alerts": True, "telegram": False, "coins": ["BTC", "ETH"], "balance": 50.0, "leverage": 200.0,
            "margin_pct": 20.0, "hold": "end"}
LIMITS = {"balance": (1, 1e7), "leverage": (1, 1000), "margin_pct": (1, 100)}
MONEY_KEYS = ("balance", "leverage", "margin_pct", "hold")          # change these and the shadow restarts
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
        if self.rt.mode == "live":
            self._save()
        self._tasks = [asyncio.create_task(self._loop(), name="bot")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._flush()
        if self._db:
            self._db.close()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick()
            except Exception:
                log.exception("bot tick failed")

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
        rec = self.twaps.on_fill(wallet.lower(), coin, sd, ms, usd)
        if rec is not None:
            self._spotted(rec)

    def _spotted(self, rec: dict) -> None:
        rec["mid"] = self._mid.get(rec["coin"])
        rec["announced"] = bool(self.cfg["alerts"])
        rec["hold"] = self.cfg["hold"]
        self._queue.append(rec)
        if rec["announced"] and self.cfg["telegram"]:
            self._notify(spotted_message(rec, self.cfg), rec["coin"])

    # ── every second: settle, shadow trade, tell when a TWAP stops ─────────
    def tick(self) -> None:
        now = max(self.now_ms.values(), default=0)
        if now:
            self.twaps.tick(now, lambda c: self.ring.get(c, {}))
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
        current = saved.get("v") == VERSION
        keep = DEFAULTS if current else ("telegram", "coins", "balance", "leverage", "margin_pct")
        self.cfg.update({k: v for k, v in (saved.get("settings") or {}).items() if k in keep and k in DEFAULTS})
        self.shadow = self._fresh_shadow()                 # the big-mover bot's shadow doesn't carry over
        if current:
            self.shadow.update(saved.get("shadow") or {})
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
        self.cfg = new
        if restart:
            self.shadow = self._fresh_shadow()
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
                      "mid": rec.get("mid")}
            break
        return {"status": self.status(), "alert": active}

    def _move_now(self, rec: dict) -> float | None:
        """bps the mid has moved the TWAP's way since the fill (FILL_S after it was spotted), live."""
        rows = self.ring.get(rec["coin"], {})
        e = rows.get(rec["t_ms"] // 1000 + FILL_S)
        mid = self._mid.get(rec["coin"])
        if not e or not mid:
            return None
        return rec["side"] * (mid / e[2] - 1) * 1e4

    def _row(self, rec: dict) -> dict:
        k = self.cfg["hold"]
        r = (rec.get("results") or {}).get(k)
        return {"id": rec["id"], "coin": rec["coin"], "side": rec["side"], "t_ms": rec["t_ms"],
                "wallet": rec["wallet"], "slices": rec["slices"], "usd": round(rec["usd"]),
                "slice_usd": round(rec["usd"] / max(rec["slices"], 1)), "running": rec["end_ms"] is None,
                "end_ms": rec["end_ms"], "settled": r is not None, "bps": None if r is None else round(r["bps"], 2),
                "pt": None if r is None else pt_of(r, self.cfg["leverage"], rec["coin"]),
                "move_now": None if r is not None else self._move_now(rec), "shadow": rec.get("shadow"),
                "announced": rec.get("announced", False)}

    def record(self, since_ms: int | None = None) -> dict:
        """Per hold: how riding every spotted TWAP did, at your leverage."""
        lev = self.cfg["leverage"]
        out = {}
        for k in HOLDS:
            rs = [(r["results"][k], r["coin"]) for r in self.twaps.records
                  if (r.get("results") or {}).get(k) and (since_ms is None or r["t_ms"] >= since_ms)]
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
        return {
            "status": self.status(), "settings": self.cfg, "limits": LIMITS, "known_coins": list(KNOWN_COINS),
            "holds": HOLDS, "fill_s": FILL_S, "live": self.live(),
            "shadow": {**self.shadow, "margin": self.cfg["margin_pct"] / 100 * self.shadow["start"]},
            "today": {"spotted": len(day), "per_day": len(day) / span_h * 24, "hours": round(span_h, 1)},
            "running": [self._row(r) for r in running][::-1],
            "recent": [self._row(r) for r in recs if r["end_ms"] is not None][-30:][::-1],
            "record": {"day": self.record(now - DAY_MS), "week": self.record(now - 7 * DAY_MS), "all": self.record()},
            "now_ms": now,
        }


def _usd(x: float) -> str:
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def _hold_words(k: str) -> str:
    return "when it stops (you'll get a message)" if k == "end" else f"after {int(k) // 60} min"


def spotted_message(rec: dict, cfg: dict) -> tuple[str, tuple[str, ...]]:
    side = "LONG" if rec["side"] > 0 else "SHORT"
    did = "buying" if rec["side"] > 0 else "selling"
    margin = cfg["margin_pct"] / 100 * cfg["balance"]
    px = rec.get("mid")
    return (f"Big TWAP: {rec['coin']} {side}",
            (f"A wallet is {did} about {_usd(rec['usd'] / rec['slices'])} every 30s ({_usd(rec['usd'])} so far).",
             f"Get in within {FILL_S}s{f' near {px:,.2f}' if px else ''}, close {_hold_words(rec.get('hold', cfg['hold']))}.",
             f"{cfg['leverage']:g}x, ${margin:,.2f} margin. Still a test."))


def stopped_message(rec: dict) -> tuple[str, tuple[str, ...]]:
    side = "long" if rec["side"] > 0 else "short"
    return (f"{rec['coin']} TWAP stopped: close your {side}",
            (f"No slice for 35s after {rec['slices']} slices ({_usd(rec['usd'])} in all).",))

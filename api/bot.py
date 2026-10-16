"""
The bot: big-mover alerts. Some Hyperliquid wallets' aggressive trades are followed by big moves their way (the
wallet and big-mover studies, scripts/walletStudy.py and scripts/bigMovers.py). The bot watches those wallets live
and tells you the moment one trades, so you can follow on Papertrade, and keeps an honest record of how every alert
did.

    the list       built from the server's recordings (the last 7 days) by scripts/bigMoversList.py, in its own
                   low-priority process, every hour. A big mover: 20+ decisions, t 2.5+, an average move of 8+ bps
                   its way within 60 seconds. No alerts while fewer than 10 wallets qualify
    an alert       a listed wallet's aggressive trades on a watched coin (BTC, ETH) reach $1k on one side within 30s.
                   Other big movers piling in on the same side within 30s join the same alert
    what to do     the same side, as fast as you can, closed after the hold (30s)
    the record     every alert is settled from the live book: in at the mid 2s, your fill delay (6s) and 10s after
                   it, out after the hold, with the worst the best bid/ask went against you (so busts show at any
                   leverage). A shadow balance takes the alerts you could (one trade at a time) at your leverage and
                   margin
    auto-pause     when the last 50 alerts average below 0 bps at your fill delay, or the last 24 hours below -1 bps,
                   alerts stop being announced (still recorded) until the last 50 recover above +1 bps
    storage        settings and the shadow balance in data/bot.json; settled alerts in the bars SQLite file
"""
import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

from config import BOT_FILE, ROOT
from engine.bigmovers import pt_of, settle
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.bot")

TICK_S = 1.0
LIST_FILE = ROOT / "data" / "bigmovers.json"
REFRESH_S = 3600
RING_KEEP_S = 20 * 60
LAGS = (2, 10)                      # always recorded, alongside your fill delay
MERGE_S = 30
KEEP_ALERTS = 5000
LAST_N = 50
PAUSE_BELOW = 0.0                   # last 50 average, bps
PAUSE_DAY_BELOW = -1.0              # last 24 h average, bps, with at least DAY_MIN alerts
DAY_MIN = 20
RESUME_ABOVE = 1.0
DAY_MS = 86_400_000

DEFAULTS = {
    "on": True, "coins": ["BTC", "ETH"], "balance": 50.0, "leverage": 200.0, "margin_pct": 20.0, "fill_s": 6,
    "hold_s": 30, "telegram": True, "auto_pause": True,
    "cutoff_bps": 8.0, "t_min": 2.5, "min_n": 20, "min_list": 10, "min_usd": 1000.0, "history_days": 7,
}
LIMITS = {"balance": (1, 1e7), "leverage": (1, 1000), "margin_pct": (1, 100), "fill_s": (1, 30), "hold_s": (5, 600),
          "cutoff_bps": (2, 50), "t_min": (1, 10), "min_n": (5, 1000), "min_list": (1, 200), "min_usd": (100, 1e7),
          "history_days": (1, 30)}
INTS = ("fill_s", "hold_s", "min_n", "min_list", "history_days")
LIST_KEYS = ("cutoff_bps", "t_min", "min_n", "min_usd", "history_days")       # change these and the list is rebuilt
MONEY_KEYS = ("balance", "leverage", "margin_pct")                            # change these and the shadow restarts
KNOWN_COINS = ("BTC", "ETH", "SOL", "HYPE")


class BotError(ValueError):
    pass


class BotService:
    def __init__(self, runtime, path: Path = BOT_FILE, db_path: Path | None = None, list_file: Path = LIST_FILE):
        self.rt = runtime
        self.path = Path(path)
        self.db_path = db_path
        self.list_file = Path(list_file)
        self.cfg = dict(DEFAULTS)
        self.shadow = self._fresh_shadow()
        self.wallets: dict[str, dict] = {}
        self.list_info: dict = {}
        self.list_state = "loading"            # loading | building | ready | empty | failed
        self.list_error = ""
        self.ring: dict[str, dict[int, list]] = {}
        self.now_ms: dict[str, int] = {}
        self._mid: dict[str, float] = {}
        self._decisions: dict[tuple, list] = {}   # (wallet, coin) → [side, first ms, last ms, usd, fired]
        self._open: dict[tuple, dict] = {}         # (coin, side) → the open alert
        self.alerts: deque = deque(maxlen=KEEP_ALERTS)
        self._pending: deque = deque()
        self.paused = False
        self.pause_reason = ""
        self._db: sqlite3.Connection | None = None
        self._tasks: list[asyncio.Task] = []
        self._building: asyncio.Task | None = None
        self._unsaved: list[dict] = []
        self._load()
        self._load_list()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.db_path:
            try:
                self._db = connect(self.db_path)
                self._db.execute("CREATE TABLE IF NOT EXISTS bot_alerts (id TEXT PRIMARY KEY, t_ms INTEGER NOT NULL, "
                                 "data TEXT NOT NULL)")
                self._db.execute("CREATE INDEX IF NOT EXISTS bot_alerts_t ON bot_alerts (t_ms)")
                self._db.commit()
                for (data,) in self._db.execute("SELECT data FROM bot_alerts ORDER BY t_ms DESC LIMIT ?", (KEEP_ALERTS,)):
                    self.alerts.appendleft(json.loads(data))
                self._check_pause(quiet=True)
            except Exception:
                log.exception("could not open the bot's alert history; it starts empty")
                self._db = None
        self._tasks = [asyncio.create_task(self._loop(), name="bot")]
        if self.rt.mode == "live":
            self._tasks.append(asyncio.create_task(self._refresh_loop(), name="bot-list"))

    async def stop(self) -> None:
        for t in self._tasks + ([self._building] if self._building else []):
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

    async def _refresh_loop(self) -> None:
        while True:
            age = time.time() * 1000 - self.list_info.get("built_ms", 0)
            if age >= REFRESH_S * 1000 or self.list_state in ("loading", "failed"):
                await self.refresh()
            await asyncio.sleep(max(60.0, REFRESH_S - age / 1000) if age < REFRESH_S * 1000 else REFRESH_S)

    async def refresh(self) -> None:
        """Rebuild the list from the recordings in a separate, low-priority process."""
        if self._building and not self._building.done():
            return
        self._building = asyncio.current_task()
        self.list_state = "building"
        c = self.cfg
        cmd = [sys.executable, "-m", "scripts.bigMoversList", "--out", str(self.list_file),
               "--days", str(c["history_days"]), "--min-usd", str(c["min_usd"]), "--min-n", str(c["min_n"]),
               "--t-min", str(c["t_min"]), "--cutoff", str(c["cutoff_bps"])]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                lines = (err or b"").decode(errors="replace").strip().splitlines()
                raise RuntimeError(lines[-1] if lines else f"exit code {proc.returncode}")
            self._load_list()
            log.info("big-mover list: %d wallets", len(self.wallets))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.list_state, self.list_error = "failed", str(exc)[:200]
            log.warning("could not build the big-mover list: %s", exc)

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
        if coin not in self.cfg["coins"] or not self.wallets:
            return
        users, side = tr.get("users") or [], tr.get("side")
        if len(users) != 2 or side not in ("B", "A"):
            return
        wallet, sd = (users[0], 1) if side == "B" else (users[1], -1)
        wallet = wallet.lower()
        if wallet not in self.wallets:
            return
        try:
            ms, usd = int(tr["time"]), float(tr["px"]) * float(tr["sz"])
        except (KeyError, TypeError, ValueError):
            return
        self.now_ms[coin] = max(self.now_ms.get(coin, 0), ms)
        key = (wallet, coin)
        d = self._decisions.get(key)
        if d is None or d[0] != sd or ms - d[2] > MERGE_S * 1000:
            d = self._decisions[key] = [sd, ms, ms, 0.0, False]
        d[2] = ms
        d[3] += usd
        if not d[4] and d[3] >= self.cfg["min_usd"]:
            d[4] = True
            self._decision(coin, wallet, sd, ms, d[3])

    def _decision(self, coin: str, wallet: str, side: int, ms: int, usd: float) -> None:
        key = (coin, side)
        a = self._open.get(key)
        if a is not None and ms - a["last_ms"] <= MERGE_S * 1000:
            if wallet not in a["wallets"]:
                a["wallets"].append(wallet)
            a["decisions"] += 1
            a["usd"] += usd
            a["last_ms"] = ms
            return
        status = self.status()
        a = {"id": f"{coin}-{ms}-{side}", "coin": coin, "side": side, "t_ms": ms, "last_ms": ms, "wallets": [wallet],
             "decisions": 1, "usd": usd, "mid": self._mid.get(coin), "announced": status == "live",
             "why": "" if status == "live" else status, "fill_s": self.cfg["fill_s"], "hold_s": self.cfg["hold_s"],
             "results": None, "shadow": None, "seen": time.time()}
        self._open[key] = a
        self.alerts.append(a)
        self._pending.append(a)
        if a["announced"] and self.cfg["telegram"]:
            self._notify(alert_message(a, self.cfg), coin)

    # ── every second: settle, shadow trade, pause ──────────────────────────
    def tick(self) -> None:
        while self._pending:
            a = self._pending[0]
            lags = sorted({*LAGS, int(self.cfg["fill_s"]), int(a["fill_s"])})
            hold = int(a["hold_s"])
            wait_s = max(lags) + hold + 2
            due = a["t_ms"] + wait_s * 1000
            now = self.now_ms.get(a["coin"], 0)
            if now < due and time.time() < a.get("seen", 0) + wait_s + 30:     # the feed stalled: settle anyway
                break
            self._pending.popleft()
            rows = self.ring.get(a["coin"], {})
            s0 = a["t_ms"] // 1000
            a["results"] = {str(lag): settle(rows, s0, lag, hold, a["side"]) for lag in lags}
            self._shadow_trade(a)
            self._unsaved.append(a)
            self._check_pause()
        cutoff = int(max(self.now_ms.values(), default=0) // 1000) - RING_KEEP_S
        for ring in self.ring.values():
            for s in [s for s in ring if s < cutoff]:
                del ring[s]
        for k in [k for k, a in self._open.items() if max(self.now_ms.values(), default=0) - a["last_ms"] > MERGE_S * 1000]:
            del self._open[k]
        for k in [k for k, d in self._decisions.items() if max(self.now_ms.values(), default=0) - d[2] > MERGE_S * 1000]:
            del self._decisions[k]
        self._flush()

    def _shadow_trade(self, a: dict) -> None:
        sh = self.shadow
        r = (a["results"] or {}).get(str(a["fill_s"]))
        if not a["announced"] or r is None or sh["busted"]:
            return
        start = a["t_ms"] + a["fill_s"] * 1000
        if start < sh["free_ms"]:
            return                                         # still in the last trade
        margin = self.cfg["margin_pct"] / 100 * sh["start"]
        if margin > sh["balance"]:
            sh["busted"] = True
            self._save()
            return
        pt = pt_of(r, self.cfg["leverage"], a["coin"])
        pnl = margin * pt
        sh["balance"] += pnl
        sh["trades"] += 1
        sh["wins"] += pnl > 0
        sh["busts"] += pt <= -1.0
        sh["free_ms"] = start + a["hold_s"] * 1000
        sh["low"] = min(sh["low"], sh["balance"])
        if sh["balance"] < margin:
            sh["busted"] = True
        a["shadow"] = {"pnl": round(pnl, 4), "balance": round(sh["balance"], 4), "pt": round(pt, 4)}
        self._save()

    def _check_pause(self, quiet: bool = False) -> None:
        if not self.cfg["auto_pause"]:
            self.paused, self.pause_reason = False, ""
            return
        lag = str(self.cfg["fill_s"])
        done = [a for a in self.alerts if a.get("results") and (a["results"].get(lag) or {}).get("bps") is not None]
        last = [a["results"][lag]["bps"] for a in done[-LAST_N:]]
        cut = (max(self.now_ms.values(), default=0) or int(time.time() * 1000)) - DAY_MS
        day = [a["results"][lag]["bps"] for a in done if a["t_ms"] >= cut]
        avg = sum(last) / len(last) if last else None
        avg_day = sum(day) / len(day) if day else None
        was = self.paused
        if not self.paused:
            if len(last) >= 30 and avg < PAUSE_BELOW:
                self.paused, self.pause_reason = True, f"the last {len(last)} alerts averaged {avg:+.1f} bps"
            elif len(day) >= DAY_MIN and avg_day < PAUSE_DAY_BELOW:
                self.paused, self.pause_reason = True, f"the last 24 hours averaged {avg_day:+.1f} bps"
        elif avg is not None and avg > RESUME_ABOVE and (avg_day is None or len(day) < DAY_MIN or avg_day >= PAUSE_DAY_BELOW):
            self.paused, self.pause_reason = False, ""
        if self.paused != was and not quiet and self.cfg["telegram"]:
            self._notify(("Bot paused" if self.paused else "Bot back on",
                          (f"Alerts stopped: {self.pause_reason}. Still watching." if self.paused
                           else f"The last {len(last)} alerts are back to {avg:+.1f} bps on average.",)), "")

    def _flush(self) -> None:
        if not self._unsaved:
            return
        if self._db:
            try:
                with transaction(self._db):
                    self._db.executemany("INSERT OR REPLACE INTO bot_alerts (id, t_ms, data) VALUES (?, ?, ?)",
                                         [(a["id"], a["t_ms"], json.dumps(a)) for a in self._unsaved])
            except sqlite3.Error as exc:
                log.warning("could not save bot alerts (will retry): %s", exc)
                del self._unsaved[:-2000]
                return
        self._unsaved.clear()

    def _notify(self, msg: tuple[str, tuple[str, ...]], coin: str) -> None:
        if self.rt.mode != "live" or not getattr(self.rt, "alerts", None):
            return
        from engine.alerts import Alert
        title, lines = msg
        self.rt.alerts.notify(Alert(f"bot-{time.time_ns()}", coin, "bot", int(time.time() * 1000), "neutral", title, lines))

    # ── state ──────────────────────────────────────────────────────────────
    def status(self) -> str:
        if not self.cfg["on"]:
            return "off"
        if self.list_state in ("loading", "building") and not self.wallets:
            return "warming"
        if len(self.wallets) < self.cfg["min_list"]:
            return "warming"
        if self.paused:
            return "paused"
        return "live"

    def _fresh_shadow(self, start: float | None = None) -> dict:
        start = float(start if start is not None else self.cfg["balance"])
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
        if "rules" in saved or "auto" in saved:            # the old rule-based bot: keep a copy, start fresh
            try:
                self.path.with_name(self.path.stem + ".old.json").write_text(json.dumps(saved), encoding="utf-8")
            except OSError:
                pass
            return
        self.cfg.update({k: v for k, v in (saved.get("settings") or {}).items() if k in DEFAULTS})
        if saved.get("shadow"):
            self.shadow.update(saved["shadow"])

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"settings": self.cfg, "shadow": self.shadow}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save the bot settings: %s", exc)

    def _load_list(self) -> None:
        try:
            info = json.loads(self.list_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            self.list_state, self.list_error = "failed", str(exc)[:200]
            return
        self.set_list(info)

    def set_list(self, info: dict) -> None:
        self.list_info = {k: v for k, v in info.items() if k != "wallets"}
        self.wallets = {w["wallet"].lower(): w for w in info.get("wallets") or []}
        self.list_state = "ready" if self.wallets else "empty"
        self.list_error = ""

    def save_settings(self, patch: dict) -> dict:
        new = dict(self.cfg)
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                raise BotError(f"Unknown setting: {k}.")
            if k in ("on", "telegram", "auto_pause"):
                new[k] = bool(v)
            elif k == "coins":
                coins = [str(c).upper() for c in (v or [])]
                if not coins or any(c not in KNOWN_COINS for c in coins):
                    raise BotError(f"Coins must be some of {', '.join(KNOWN_COINS)}.")
                new[k] = coins
            else:
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    raise BotError(f"{k.replace('_', ' ')} must be a number.") from None
                lo, hi = LIMITS[k]
                if not lo <= x <= hi:
                    raise BotError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
                new[k] = int(round(x)) if k in INTS else x
        rebuild = any(new[k] != self.cfg[k] for k in LIST_KEYS)
        restart = any(new[k] != self.cfg[k] for k in MONEY_KEYS)
        self.cfg = new
        if restart:
            self.shadow = self._fresh_shadow()
        if not new["auto_pause"]:
            self.paused, self.pause_reason = False, ""
        self._save()
        if rebuild and self.rt.mode == "live":
            asyncio.get_running_loop().create_task(self.refresh())
        return self.public()

    def reset_shadow(self) -> dict:
        self.shadow = self._fresh_shadow()
        self._save()
        return self.public()

    def start_refresh(self) -> dict:
        if self.rt.mode != "live":
            raise BotError("The list is built from live recordings, so only in live mode.")
        asyncio.get_running_loop().create_task(self.refresh())
        self.list_state = "building"
        return self.public()

    # ── what the dashboard reads ───────────────────────────────────────────
    def live(self) -> dict:
        """The small piece every snapshot carries, so an alert reaches every open tab within half a second."""
        st = self.status()
        a = self.alerts[-1] if self.alerts else None
        active = None
        if a and a["announced"]:
            now = max(self.now_ms.values(), default=0)
            if now - a["t_ms"] <= (a["fill_s"] + a["hold_s"]) * 1000:
                active = {"id": a["id"], "coin": a["coin"], "side": a["side"], "t_ms": a["t_ms"], "fill_s": a["fill_s"],
                          "hold_s": a["hold_s"], "wallets": len(a["wallets"]), "usd": round(a["usd"]), "mid": a["mid"]}
        return {"status": st, "alert": active}

    def _row(self, a: dict) -> dict:
        lag = str(self.cfg["fill_s"])
        r = (a.get("results") or {}).get(lag) if a.get("results") else None
        return {"id": a["id"], "coin": a["coin"], "side": a["side"], "t_ms": a["t_ms"], "wallets": len(a["wallets"]),
                "usd": round(a["usd"]), "announced": a["announced"], "why": a.get("why", ""),
                "settled": a.get("results") is not None, "bps": None if r is None else round(r["bps"], 2),
                "pt": None if r is None else pt_of(r, self.cfg["leverage"], a["coin"]),
                "shadow": a.get("shadow")}

    def record(self, since_ms: int | None = None, announced_only: bool = False) -> dict:
        """How the alerts did: per fill delay, the average move, win rate and Papertrade result at your leverage."""
        lev = self.cfg["leverage"]
        out = {}
        for lag in sorted({*LAGS, int(self.cfg["fill_s"])}):
            k = str(lag)
            rs = [(a["results"][k], a["coin"]) for a in self.alerts
                  if a.get("results") and a["results"].get(k) and (since_ms is None or a["t_ms"] >= since_ms)
                  and (a["announced"] or not announced_only)]
            if not rs:
                out[k] = {"n": 0}
                continue
            bps = [r["bps"] for r, _ in rs]
            pts = [pt_of(r, lev, c) for r, c in rs]
            out[k] = {"n": len(rs), "bps": sum(bps) / len(bps), "win": sum(b > 0 for b in bps) / len(bps),
                      "pt": sum(pts) / len(pts), "busts": sum(p <= -1.0 for p in pts) / len(pts)}
        return out

    def public(self) -> dict:
        now = max(self.now_ms.values(), default=0) or int(time.time() * 1000)
        settled = [a for a in self.alerts if a.get("results")]
        lag = str(self.cfg["fill_s"])
        last = [a["results"][lag]["bps"] for a in settled if (a["results"].get(lag) or {}).get("bps") is not None][-LAST_N:]
        day = [a for a in self.alerts if a["t_ms"] >= now - DAY_MS]
        return {
            "status": self.status(), "pause_reason": self.pause_reason, "settings": self.cfg, "limits": LIMITS,
            "known_coins": list(KNOWN_COINS), "live": self.live(),
            "list": {"state": self.list_state, "error": self.list_error, "count": len(self.wallets), **self.list_info,
                     "wallets": sorted(self.wallets.values(), key=lambda w: -w.get("mean_bps", 0))[:40]},
            "shadow": {**self.shadow, "margin": self.cfg["margin_pct"] / 100 * self.shadow["start"]},
            "last50": {"n": len(last), "bps": sum(last) / len(last) if last else None},
            "today": {"alerts": len(day), "per_hour": len(day) / 24},
            "record": {"day": self.record(now - DAY_MS), "week": self.record(now - 7 * DAY_MS), "all": self.record()},
            "alerts": [self._row(a) for a in list(self.alerts)[-30:]][::-1],
            "now_ms": now,
        }


def _usd(x: float) -> str:
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def _px(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f}" if x < 10_000 else f"{x:,.1f}"


def alert_message(a: dict, cfg: dict) -> tuple[str, tuple[str, ...]]:
    side = "LONG" if a["side"] > 0 else "SHORT"
    did = "bought" if a["side"] > 0 else "sold"
    margin = cfg["margin_pct"] / 100 * cfg["balance"]
    close = time.strftime("%H:%M:%S", time.gmtime((a["t_ms"] + (a["fill_s"] + a["hold_s"]) * 1000) / 1000))
    return (f"{a['coin']} {side} now",
            (f"A big mover {did} {_usd(a['usd'])} at {_px(a['mid'])}.",
             f"Get in within {a['fill_s']}s, close after {a['hold_s']}s (about {close} UTC).",
             f"{cfg['leverage']:g}x, ${margin:g} margin."))

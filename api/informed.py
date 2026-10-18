"""
Basket 2, informed flow: a live test of fading the reliably wrong (study 2, scripts/fadeStudy.py). Some wallets'
hidden orders push price and then reliably reverse. When one of them starts a new hidden order, take the other side
and hold 15 minutes.

    scores      data/informed_scores.json, rebuilt from the recordings every SCORE_REFRESH_S in a low-priority
                process (scripts/informedScores.py): each wallet's shrunk average of what price did 15 minutes after
                its hidden orders. Only the past goes in, so the live test is out of sample
    a fade      a hidden order (engine/informed.py) from a wallet scoring at or below your threshold (-5): the other
                side, in FILL_S after it's spotted, out HOLD_S later
    the record  every hidden order is settled from the live book as if faded, so the fades can be compared with
                fading everything (the control). Two shadow balances, one trade at a time each:
                  Papertrade   BTC and ETH only, your leverage, Papertrade's cut on wins
                  Hyperliquid  all coins, leverage capped at each coin's maximum, limit-order fees (3 bps a round trip)
                Hyperliquid at taker fees (9 bps) is shown in the record too
    storage     settings and shadows in data/bot_informed.json; every settled order in the bars SQLite (bot_fades)
"""
import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

from dataclasses import asdict

from api import livestate
from config import ROOT
from engine.bigmovers import mid_near, pt_of, settle
from engine.informed import Hidden, HiddenOrders
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.bot")

SCORES_FILE = ROOT / "data" / "informed_scores.json"
SCORE_REFRESH_S = 3 * 3600
TICK_S = 1.0
FILL_S = 6
HOLD_S = 900
RING_KEEP_S = 20 * 60
KEEP = 20_000
DAY_MS = 86_400_000
TAKER_RT_BPS = 9.0
MAKER_RT_BPS = 3.0
HL_MAX_LEV = {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10}
PAPERTRADE_COINS = ("BTC", "ETH")
KNOWN_COINS = ("BTC", "ETH", "SOL", "HYPE")

DEFAULTS = {"alerts": False, "telegram": False, "threshold": -5.0, "coins": list(KNOWN_COINS), "balance": 50.0,
            "leverage": 200.0, "margin_pct": 20.0}
LIMITS = {"balance": (1, 1e7), "leverage": (1, 1000), "margin_pct": (1, 100), "threshold": (-100, 0)}
MONEY_KEYS = ("balance", "leverage", "margin_pct", "threshold")


class InformedBasket:
    key, name = "informed", "Informed flow"

    def __init__(self, runtime, path: Path, db_path: Path | None = None, scores_file: Path = SCORES_FILE):
        self.rt = runtime
        self.path = Path(path)
        self.db_path = db_path
        self.scores_file = Path(scores_file)
        self.cfg = dict(DEFAULTS)
        self.shadows = {"pt": self._fresh(), "hl": self._fresh()}
        self.ho = HiddenOrders(keep=False)
        self.ring: dict[str, dict[int, list]] = {}
        self.now_ms: dict[str, int] = {}
        self._mid: dict[str, float] = {}
        self.records: deque = deque(maxlen=KEEP)
        self._pending: deque = deque()
        self._unsaved: list[dict] = []
        self.scores: dict[str, dict] = {}
        self.scores_info: dict = {}
        self.scores_state = "loading"              # loading | building | ready | failed
        self.scores_error = ""
        self._db: sqlite3.Connection | None = None
        self._tasks: list[asyncio.Task] = []
        self._building: asyncio.Task | None = None
        self.live_path = self.path.with_name(self.path.stem + "_live.json")
        self._pauses: list[int] = []                 # restarts: the last second saved before each (see api/livestate)
        self._saved_at = 0.0
        self._load()
        self._load_scores()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.db_path:
            try:
                self._db = connect(self.db_path)
                self._db.execute("CREATE TABLE IF NOT EXISTS bot_fades (id TEXT PRIMARY KEY, t_ms INTEGER NOT NULL, "
                                 "data TEXT NOT NULL)")
                self._db.commit()
                cut = int(time.time() * 1000) - 8 * DAY_MS
                for (d,) in self._db.execute("SELECT data FROM bot_fades WHERE t_ms >= ? ORDER BY t_ms", (cut,)):
                    self.records.append(json.loads(d))
            except Exception:
                log.exception("could not open the informed basket's history; it starts empty")
                self._db = None
        self._restore()
        self._tasks = [asyncio.create_task(self._loop(), name="bot-informed")]
        if self.rt.mode == "live":
            self._tasks.append(asyncio.create_task(self._refresh_loop(), name="bot-informed-scores"))

    async def stop(self) -> None:
        for t in self._tasks + ([self._building] if self._building else []):
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
                log.exception("informed basket tick failed")

    # ── surviving a restart (api/livestate) ────────────────────────────────
    def _keep(self) -> None:
        """Save what's in flight: open trades, recent prices, orders being spotted."""
        if self.rt.mode != "live":
            return
        self._saved_at = time.time()
        livestate.save(self.live_path, {
            "saved_ms": max(self.now_ms.values(), default=0) or int(time.time() * 1000), "pauses": self._pauses,
            "ring": livestate.ring_dump(self.ring),
            "mid": self._mid, "pending": list(self._pending), "orders": [asdict(h) for h in self.ho.open.values()]})

    def _restore(self) -> None:
        saved = livestate.load(self.live_path) if self.rt.mode == "live" else None
        if not saved:
            return
        self._pauses = livestate.pauses_after(saved)
        for coin, rows in livestate.ring_load(saved.get("ring")).items():
            self.ring.setdefault(coin, {}).update(rows)
        self._mid.update({k: float(v) for k, v in (saved.get("mid") or {}).items()})
        have = {r["id"] for r in self.records}
        back = sorted((r for r in saved.get("pending") or [] if r.get("id") not in have), key=lambda r: r["t_ms"])
        for r in back:
            self.records.append(r)
            self._pending.append(r)
        for d in saved.get("orders") or []:
            try:
                h = Hidden(**d)
            except TypeError:
                continue
            self.ho.open[(h.wallet, h.coin)] = h
        if back:
            log.info("informed basket: picked up %d open trades after the restart", len(back))

    async def _refresh_loop(self) -> None:
        while True:
            age = time.time() * 1000 - self.scores_info.get("built_ms", 0)
            if age >= SCORE_REFRESH_S * 1000 or self.scores_state in ("loading", "failed"):
                await self.refresh()
                age = 0
            await asyncio.sleep(max(60.0, SCORE_REFRESH_S - age / 1000))

    async def refresh(self) -> None:
        """Rebuild the wallet scores from the recordings in a separate, low-priority process."""
        if self._building and not self._building.done():
            return
        self._building = asyncio.current_task()
        self.scores_state = "building"
        cmd = [sys.executable, "-m", "scripts.informedScores", "--out", str(self.scores_file)]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                lines = (err or b"").decode(errors="replace").strip().splitlines()
                raise RuntimeError(lines[-1] if lines else f"exit code {proc.returncode}")
            self._load_scores()
            log.info("informed basket: %d wallets scored", len(self.scores))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.scores_state, self.scores_error = "failed", str(exc)[:200]
            log.warning("could not build the informed scores: %s", exc)

    def _load_scores(self) -> None:
        try:
            info = json.loads(self.scores_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            self.scores_state, self.scores_error = "failed", str(exc)[:200]
            return
        self.set_scores(info)

    def set_scores(self, info: dict) -> None:
        self.scores = {w.lower(): v for w, v in (info.get("wallets") or {}).items()}
        self.scores_info = {k: v for k, v in info.items() if k != "wallets"}
        self.scores_state, self.scores_error = "ready", ""

    # ── the live feed ──────────────────────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        ch = msg.get("channel")
        if ch == "l2Book":
            d = msg.get("data") or {}
            coin = d.get("coin")
            if coin not in self.cfg["coins"]:
                return
            try:
                bids, asks = d["levels"]
                bid, ask, ms = float(bids[0]["px"]), float(asks[0]["px"]), int(d["time"])
            except (KeyError, IndexError, TypeError, ValueError):
                return
            s, mid = ms // 1000, (bid + ask) / 2
            r = self.ring.setdefault(coin, {}).get(s)
            if r is None:
                self.ring[coin][s] = [bid, ask, mid]
            else:
                r[0], r[1], r[2] = min(r[0], bid), max(r[1], ask), mid
            self._mid[coin] = mid
            self.now_ms[coin] = max(self.now_ms.get(coin, 0), ms)
        elif ch == "trades":
            for tr in msg.get("data") or []:
                self._on_trade(tr)

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
        h = self.ho.add(wallet.lower(), coin, sd, ms, usd)
        if h is not None:
            self._spotted(h)

    def _spotted(self, h) -> None:
        sc = self.scores.get(h.wallet) or {}
        score = sc.get("score")
        fade = score is not None and score <= self.cfg["threshold"]
        rec = {"id": f"fade-{h.coin}-{h.t_detect}-{h.wallet[-6:]}", "coin": h.coin, "side": -h.side, "order_side": h.side,
               "wallet": h.wallet, "t_ms": h.t_detect, "usd": round(h.usd_detect), "pieces": h.pieces_detect,
               "twap": h.regular_detect, "score": score, "n": sc.get("n", 0), "fade": fade, "mid": self._mid.get(h.coin),
               "announced": bool(fade and self.cfg["alerts"]), "result": None, "settled": False}
        self.records.append(rec)
        self._pending.append(rec)
        if rec["announced"] and self.cfg["telegram"]:
            self._notify(fade_message(rec, self.cfg), rec["coin"])

    # ── every second: settle, shadow trade ─────────────────────────────────
    def tick(self) -> None:
        now = max(self.now_ms.values(), default=0)
        while self._pending:
            rec = self._pending[0]
            due = rec["t_ms"] + (FILL_S + HOLD_S + 2) * 1000
            if self.now_ms.get(rec["coin"], 0) < due and now - due < 120_000:
                break
            self._pending.popleft()
            rows, s0 = self.ring.get(rec["coin"], {}), rec["t_ms"] // 1000
            stale = livestate.stale_for(rows, self._pauses, s0 + FILL_S, s0 + FILL_S + HOLD_S)
            rec["result"] = settle(rows, s0, FILL_S, HOLD_S, rec["side"], stale_s=stale)
            rec["settled"] = True
            if rec["fade"] and rec["result"]:
                self._shadow(rec)
            self._unsaved.append(rec)
        if now:
            cutoff = now // 1000 - RING_KEEP_S
            for ring in self.ring.values():
                for s in [s for s in ring if s < cutoff]:
                    del ring[s]
            self.ho.prune(now)
        self._flush()

    def _shadow(self, rec: dict) -> None:
        r, coin = rec["result"], rec["coin"]
        start = rec["t_ms"] + FILL_S * 1000
        rec["shadow"] = {}
        for key in ("pt", "hl"):
            sh = self.shadows[key]
            if sh["busted"] or start < sh["free_ms"] or (key == "pt" and coin not in PAPERTRADE_COINS):
                continue
            margin = self.cfg["margin_pct"] / 100 * sh["start"]
            if margin > sh["balance"]:
                sh["busted"] = True
                continue
            res = pt_of(r, self.cfg["leverage"], coin) if key == "pt" else hl_result(r, self.cfg["leverage"], coin)
            pnl = margin * res
            sh["balance"] += pnl
            sh["trades"] += 1
            sh["wins"] += pnl > 0
            sh["busts"] += res <= -1.0
            sh["free_ms"] = start + HOLD_S * 1000
            sh["low"] = min(sh["low"], sh["balance"])
            sh["busted"] = sh["balance"] < margin
            rec["shadow"][key] = {"pnl": round(pnl, 4), "res": round(res, 4)}
        self._save()

    def _flush(self) -> None:
        if not self._unsaved:
            return
        if self._db:
            try:
                with transaction(self._db):
                    self._db.executemany("INSERT OR REPLACE INTO bot_fades (id, t_ms, data) VALUES (?, ?, ?)",
                                         [(r["id"], r["t_ms"], json.dumps(r)) for r in self._unsaved])
            except sqlite3.Error as exc:
                log.warning("could not save the informed basket's orders (will retry): %s", exc)
                return
        self._unsaved.clear()

    def _notify(self, msg, coin: str) -> None:
        if self.rt.mode != "live" or not getattr(self.rt, "alerts", None):
            return
        from engine.alerts import Alert
        title, lines = msg
        self.rt.alerts.notify(Alert(f"bot-{time.time_ns()}", coin, "bot", int(time.time() * 1000), "neutral", title, lines))

    # ── settings ───────────────────────────────────────────────────────────
    def _fresh(self) -> dict:
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
        self.cfg.update({k: v for k, v in (saved.get("settings") or {}).items() if k in DEFAULTS})
        for key in ("pt", "hl"):
            self.shadows[key] = self._fresh()
            self.shadows[key].update((saved.get("shadows") or {}).get(key) or {})

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"settings": self.cfg, "shadows": self.shadows}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)

    def save_settings(self, patch: dict) -> dict:
        from api.bot import BotError
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
            self.shadows = {"pt": self._fresh(), "hl": self._fresh()}
        self._save()
        return self.public()

    def reset_shadow(self) -> dict:
        self.shadows = {"pt": self._fresh(), "hl": self._fresh()}
        self._save()
        return self.public()

    # ── what the dashboard reads ───────────────────────────────────────────
    @property
    def shadow(self) -> dict:
        """The balance shown on the basket tab: Papertrade's (BTC and ETH fades only)."""
        return self.shadows["pt"]

    def status(self) -> str:
        if not self.scores:
            return "warming"
        return "live" if self.cfg["alerts"] else "testing"

    def live(self) -> dict:
        now = max(self.now_ms.values(), default=0)
        active = None
        if self.cfg["alerts"]:
            for rec in reversed(list(self.records)[-50:]):
                if rec.get("announced") and now - rec["t_ms"] <= (FILL_S + HOLD_S) * 1000:
                    active = {"id": rec["id"], "coin": rec["coin"], "side": rec["side"], "t_ms": rec["t_ms"],
                              "fill_s": FILL_S, "hold_s": HOLD_S, "kind": "fade", "usd": rec["usd"],
                              "score": rec["score"], "mid": rec.get("mid")}
                    break
        return {"status": self.status(), "alert": active}

    def record(self, since_ms: int | None, fades_only: bool) -> dict:
        rs = [r for r in self.records if r.get("result") and (since_ms is None or r["t_ms"] >= since_ms)
              and (r["fade"] or not fades_only)]
        if not rs:
            return {"n": 0}
        b = [r["result"]["bps"] for r in rs]
        pt = [pt_of(r["result"], self.cfg["leverage"], r["coin"]) for r in rs if r["coin"] in PAPERTRADE_COINS]
        hl = [hl_result(r["result"], self.cfg["leverage"], r["coin"]) for r in rs]
        return {"n": len(rs), "bps": sum(b) / len(b), "win": sum(x > 0 for x in b) / len(b),
                "taker": sum(b) / len(b) - TAKER_RT_BPS, "maker": sum(b) / len(b) - MAKER_RT_BPS,
                "pt": sum(pt) / len(pt) if pt else None, "pt_n": len(pt), "hl": sum(hl) / len(hl),
                "busts": sum(x <= -1.0 for x in hl) / len(hl), **dips(rs)}

    def _row(self, r: dict) -> dict:
        res = r.get("result")
        return {"id": r["id"], "coin": r["coin"], "side": r["side"], "t_ms": r["t_ms"], "wallet": r["wallet"],
                "usd": r["usd"], "score": r["score"], "n": r["n"], "settled": r.get("settled", False),
                "bps": None if not res else round(res["bps"], 2), "dip": None if not res else round(res["adv"], 2),
                "pt": pt_of(res, self.cfg["leverage"], r["coin"]) if res and r["coin"] in PAPERTRADE_COINS else None,
                "hl": hl_result(res, self.cfg["leverage"], r["coin"]) if res else None,
                "move_now": self._move_now(r) if not r.get("settled") else None, "announced": r.get("announced", False)}

    def _move_now(self, r: dict) -> float | None:
        """bps the mid has moved the fade's way since the entry (FILL_S after spotting), live. The feed can skip a
        second, so the entry is the nearest one (as settle() does), else the mid when it was spotted."""
        entry = mid_near(self.ring.get(r["coin"], {}), r["t_ms"] // 1000 + FILL_S) or r.get("mid")
        mid = self._mid.get(r["coin"])
        return r["side"] * (mid / entry - 1) * 1e4 if entry and mid else None

    def public(self) -> dict:
        now = max(self.now_ms.values(), default=0) or int(time.time() * 1000)
        recs = list(self.records)
        day = [r for r in recs if r["t_ms"] >= now - DAY_MS]
        fades = [r for r in recs if r["fade"]]
        open_ = [r for r in fades if not r.get("settled")]
        rec_of = lambda since: {"fade": self.record(since, True), "all": self.record(since, False)}
        lev = self.cfg["leverage"]
        return {
            "status": self.status(), "settings": self.cfg, "limits": LIMITS, "known_coins": list(KNOWN_COINS),
            "fill_s": FILL_S, "hold_s": HOLD_S, "live": self.live(),
            "shadow": {**self.shadow, "margin": self.cfg["margin_pct"] / 100 * self.shadow["start"]},
            "shadows": {k: {**v, "margin": self.cfg["margin_pct"] / 100 * v["start"]} for k, v in self.shadows.items()},
            "hl_lev": {c: min(lev, HL_MAX_LEV.get(c, 10)) for c in KNOWN_COINS},
            "scores": {"state": self.scores_state, "error": self.scores_error, **self.scores_info,
                       "wallets": len(self.scores),
                       "faded_wallets": sum(1 for v in self.scores.values() if v.get("score", 0) <= self.cfg["threshold"])},
            "today": {"orders": len(day), "fades": sum(1 for r in day if r["fade"])},
            "open": [self._row(r) for r in open_][::-1],
            "recent": [self._row(r) for r in fades if r.get("settled")][-30:][::-1],
            "record": {"day": rec_of(now - DAY_MS), "week": rec_of(now - 7 * DAY_MS), "all": rec_of(None)},
            "costs": {"taker": TAKER_RT_BPS, "maker": MAKER_RT_BPS},
            "now_ms": now,
        }


def dips(rs: list[dict]) -> dict:
    """How far trades went against you before closing (the worst bid for a long, ask for a short), in bps:
    the usual one (median) and the deepest."""
    adv = sorted(r["result"]["adv"] for r in rs)
    if not adv:
        return {"dip": None, "dip_max": None}
    m = len(adv) // 2
    return {"dip": adv[m] if len(adv) % 2 else (adv[m - 1] + adv[m]) / 2, "dip_max": adv[-1]}


def hl_result(r: dict, leverage: float, coin: str) -> float:
    """One trade on Hyperliquid as a share of margin: leverage capped at the coin's maximum, limit-order fees,
    out at the bust price if the worst point went that far."""
    lev = min(leverage, HL_MAX_LEV.get(coin, 10))
    if r.get("adv", 0) >= 1e4 / lev:
        return -1.0
    return max(lev * (r["bps"] - MAKER_RT_BPS) / 1e4, -1.0)


def _usd(x: float) -> str:
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def fade_message(rec: dict, cfg: dict) -> tuple[str, tuple[str, ...]]:
    side = "LONG" if rec["side"] > 0 else "SHORT"
    did = "buying" if rec["order_side"] > 0 else "selling"
    px = rec.get("mid")
    return (f"Fade: {rec['coin']} {side}",
            (f"A wallet that's usually wrong is {did} in pieces ({_usd(rec['usd'])} so far; score {rec['score']:+.1f} "
             f"over {rec['n']} orders).",
             f"Take the other side within {FILL_S}s{f' near {px:,.2f}' if px else ''}, close after {HOLD_S // 60} min.",
             "Still a test."))

"""
Basket 3, degen: a shot at doubling a small pot each week. It trades the informed basket's fades on SOL and HYPE (the
fade check's winner: about +1% of margin a trade, 26 a day) but sizes like a gambler: a big share of the pot on every
trade, at Hyperliquid's max leverage. A week ends when the pot reaches 2x or falls to half (or after 7 days), and the
next week starts again from the balance. Paper only, alerts off: a live test.

    trades      every hidden order on your coins from a wallet scoring at or below the informed basket's threshold:
                the other side, in FILL_S after it's spotted, out HOLD_S later (as the informed basket), one open
                trade per coin. The informed basket spots and settles them; this one sizes and books them
    size        pot_pct of the pot when the trade opens (trades still open don't count yet), at each coin's max
                leverage on Hyperliquid, limit-order fees (3 bps a round trip). Liquidated (margin lost) if the worst
                point reaches the maintenance margin, half the margin's distance away
    a week      starts at its first trade; ends at 2x (doubled), at half (halved) or after 7 days (neither). What it
                ended at, minus the balance, is banked
    storage     data/bot_degen.json: settings, this week, open trades, the last 50 trades and the last 52 weeks;
                data/bot_degen_trades.jsonl: every trade ever closed, one line each (the panel's History)
"""
import asyncio
import json
import logging
import time
from itertools import islice
from pathlib import Path

from api.informed import FILL_S, HL_MAX_LEV, HOLD_S, KNOWN_COINS, MAKER_RT_BPS

log = logging.getLogger("analytix.bot")

VERSION = 1
TICK_S = 1.0
UP, DOWN = 2.0, 0.5
WEEK_MS = 7 * 86_400_000
DAY_MS = 86_400_000
DEFAULTS = {"balance": 50.0, "pot_pct": 40.0, "coins": ["SOL", "HYPE"]}
LIMITS = {"balance": (1, 1e7), "pot_pct": (1, 100)}


def result(res: dict | None, coin: str) -> float | None:
    """One trade on Hyperliquid at the coin's max leverage, as a share of margin. None if it couldn't be scored."""
    if not res:
        return None
    lev = HL_MAX_LEV.get(coin, 10)
    if res.get("adv", 0) >= 1e4 / lev / 2:
        return -1.0
    return max(lev * (res["bps"] - MAKER_RT_BPS) / 1e4, -1.0)


class DegenBasket:
    key, name = "degen", "Degen"

    def __init__(self, runtime, path: Path, informed):
        self.rt = runtime
        self.path = Path(path)
        self.inf = informed
        self.cfg = dict(DEFAULTS)
        self.state = self._fresh()
        self._task: asyncio.Task | None = None
        self.hist_path = self.path.with_name(self.path.stem + "_trades.jsonl")
        self._load()
        if not self.hist_path.exists() and self.state["done"]:      # first run with history: keep what's stored
            for row in self.state["done"]:
                self._log(row)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="bot-degen")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._save()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick()
            except Exception:
                log.exception("degen basket tick failed")

    def on_message(self, msg: dict) -> None:
        """Nothing to read: the informed basket watches the feed and spots the orders."""

    # ── every second: open what's new, book what's settled, end the week ───
    def tick(self) -> None:
        st = self.state
        changed = False
        new = []
        for r in reversed(self.inf.records):
            if r["t_ms"] <= st["seen_ms"]:
                break
            new.append(r)
        for r in reversed(new):
            st["seen_ms"] = max(st["seen_ms"], r["t_ms"])
            if self._wanted(r):
                changed |= self._open(r)
        if st["open"]:
            recs = {r["id"]: r for r in islice(reversed(self.inf.records), 2000)}
            for o in list(st["open"]):
                r = recs.get(o["id"])
                if r is not None and r.get("settled"):
                    self._close(o, r)
                    changed = True
        now = self._now()
        if st["week_ms"] and now - st["week_ms"] >= WEEK_MS:
            self._end_week("neither", st["week_ms"] + WEEK_MS)
            changed = True
        if changed:
            self._save()

    def _now(self) -> int:
        return max(self.inf.now_ms.values(), default=0) or int(time.time() * 1000)

    def _wanted(self, r: dict) -> bool:
        s = r.get("score")
        return s is not None and s <= self.inf.cfg["threshold"] and r["coin"] in self.cfg["coins"]

    def _open(self, r: dict) -> bool:
        st, coin = self.state, r["coin"]
        start = r["t_ms"] + FILL_S * 1000
        if start < st["free"].get(coin, 0):
            return False                                   # that coin's last trade is still open
        if not st["week_ms"]:
            st["week_ms"] = start
        st["open"].append({"id": r["id"], "coin": coin, "side": r["side"], "t_ms": r["t_ms"], "usd": r.get("usd"),
                           "margin": round(self.cfg["pot_pct"] / 100 * st["pot"], 4),
                           "lev": HL_MAX_LEV.get(coin, 10)})
        st["free"][coin] = start + HOLD_S * 1000
        return True

    def _close(self, o: dict, r: dict) -> None:
        st = self.state
        res = result(r.get("result"), o["coin"])
        pnl = o["margin"] * res if res is not None else 0.0
        st["pot"] += pnl
        st["open"].remove(o)
        st["trades"] += 1
        st["week_trades"] += 1
        st["wins"] += pnl > 0
        st["busts"] += res is not None and res <= -1.0
        bps = (r.get("result") or {}).get("bps")
        row = {**o, "pnl": round(pnl, 4), "pot": round(st["pot"], 4), "bps": None if bps is None else round(bps, 2),
               "bust": res is not None and res <= -1.0}
        st["done"] = (st["done"] + [row])[-50:]
        self._log(row)
        end = r["t_ms"] + (FILL_S + HOLD_S) * 1000
        if st["pot"] >= UP * st["start"]:
            self._end_week("2x", end)
        elif st["pot"] <= DOWN * st["start"]:
            self._end_week("-50%", end)

    def _end_week(self, how: str, t_ms: int) -> None:
        st = self.state
        st["weeks"] = (st["weeks"] + [{"since": st["week_ms"], "end": t_ms, "result": how, "pot": round(st["pot"], 4),
                                       "trades": st["week_trades"]}])[-52:]
        st["banked"] += st["pot"] - st["start"]
        st["start"] = float(self.cfg["balance"])                             # a balance changed mid-week starts here
        st["pot"], st["week_ms"], st["week_trades"] = st["start"], 0, 0      # the next week starts at its first trade

    # ── settings ───────────────────────────────────────────────────────────
    def _fresh(self) -> dict:
        start, now = float(self.cfg["balance"]), int(time.time() * 1000)
        return {"start": start, "pot": start, "week_ms": 0, "week_trades": 0, "trades": 0, "wins": 0, "busts": 0,
                "banked": 0.0, "weeks": [], "open": [], "done": [], "free": {}, "seen_ms": now, "since_ms": now}

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError):
            log.warning("could not read %s; using defaults", self.path)
            return
        self.cfg.update({k: v for k, v in (saved.get("settings") or {}).items() if k in DEFAULTS})
        self.state = self._fresh()
        if saved.get("v") == VERSION:
            self.state.update(saved.get("state") or {})

    def _log(self, row: dict) -> None:
        """Every closed trade, appended for good (the stored state keeps only the last 50)."""
        try:
            self.hist_path.parent.mkdir(parents=True, exist_ok=True)
            with self.hist_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("could not log a degen trade to %s: %s", self.hist_path, exc)

    def history(self, days: float | None = None) -> dict:
        """Closed trades over the last `days` (all of them when None), newest first."""
        now = self._now()
        since = now - days * DAY_MS if days else 0
        rows, seen = [], set()
        try:
            with self.hist_path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        t = json.loads(line)
                    except ValueError:
                        continue
                    if t.get("t_ms", 0) >= since and t.get("id") not in seen:
                        seen.add(t.get("id"))
                        rows.append(t)
        except FileNotFoundError:
            pass
        rows.sort(key=lambda t: t["t_ms"], reverse=True)
        return {"basket": self.key, "days": days, "since_ms": since, "now_ms": now, "trades": rows,
                "hold_s": HOLD_S}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": VERSION, "settings": self.cfg, "state": self.state}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)

    def save_settings(self, patch: dict) -> dict:
        from api.bot import BotError
        new = dict(self.cfg)
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                raise BotError(f"Unknown setting: {k}.")
            if k == "coins":
                coins = [str(c).upper() for c in (v or [])]
                if not coins or any(c not in KNOWN_COINS for c in coins):
                    raise BotError(f"Coins must be some of {', '.join(KNOWN_COINS)}.")
                new[k] = coins
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise BotError(f"{k.replace('_', ' ')} must be a number.") from None
            lo, hi = LIMITS[k]
            if not lo <= x <= hi:
                raise BotError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
            new[k] = x
        st = self.state
        self.cfg = new                                     # a new share applies from the next trade
        if new["balance"] != st["start"] and not st["week_ms"] and not st["open"]:
            st["start"] = st["pot"] = float(new["balance"])   # between weeks: from now; mid-week: from the next week
        self._save()
        return self.public()

    def reset_shadow(self) -> dict:
        self.state = self._fresh()
        self._save()
        return self.public()

    # ── what the dashboard reads ───────────────────────────────────────────
    @property
    def shadow(self) -> dict:
        """The tab's balance: this week's pot."""
        return {"start": self.state["start"], "balance": self.state["pot"]}

    def status(self) -> str:
        return "testing" if self.inf.scores else "warming"

    def live(self) -> dict:
        return {"status": self.status(), "alert": None}

    def public(self) -> dict:
        st, now = self.state, self._now()
        recs = {r["id"]: r for r in islice(reversed(self.inf.records), 2000)} if st["open"] else {}
        open_ = []
        for o in st["open"]:
            r = recs.get(o["id"])
            move = self.inf._move_now(r) if r is not None and not r.get("settled") else None
            open_.append({**o, "move_now": move, "pnl_now": None if move is None else o["margin"] * o["lev"] * move / 1e4})
        count = {"2x": 0, "-50%": 0, "neither": 0}
        for w in st["weeks"]:
            count[w["result"]] = count.get(w["result"], 0) + 1
        day = [t for t in st["done"] if t["t_ms"] >= now - DAY_MS]
        return {
            "status": self.status(), "settings": self.cfg, "limits": LIMITS, "known_coins": list(KNOWN_COINS),
            "fill_s": FILL_S, "hold_s": HOLD_S, "live": self.live(), "shadow": self.shadow,
            "pot": st["pot"], "start": st["start"], "banked": st["banked"], "trades": st["trades"], "wins": st["wins"],
            "busts": st["busts"], "week_trades": st["week_trades"], "week_ms": st["week_ms"],
            "week_left_s": max(0, (st["week_ms"] + WEEK_MS - now) / 1000) if st["week_ms"] else None,
            "count": count, "weeks": st["weeks"][-12:][::-1], "open": open_[::-1], "recent": st["done"][-30:][::-1],
            "today": {"trades": len(day), "wins": sum(1 for t in day if t["pnl"] > 0)},
            "threshold": self.inf.cfg["threshold"], "hl_lev": {c: HL_MAX_LEV.get(c, 10) for c in KNOWN_COINS},
            "up": UP, "down": DOWN, "now_ms": now,
        }

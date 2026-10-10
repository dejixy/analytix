"""
Alert delivery: settings, the once-a-second check, Telegram and the dashboard feed.

Settings live in data/alerts.json on the machine running Analytix (never in git): which
alerts are on and their minimums, the cooldown, which coins, your price levels, and the
Telegram bot token plus the chats you've approved. The token is write-only through the API:
it's never sent back to a browser.

Telegram: a bot can only message chats that have started it (or groups/channels it was added
to), and Analytix only sends to the ones switched on here. "Find chats" lists everyone who
has messaged the bot recently; each starts switched off. Replays never send to Telegram, so
a demo can't ping your phone with made-up moves.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path

import httpx

from config import ALERTS_FILE
from engine.alerts import DEFAULT_COOLDOWN_MIN, DEFAULT_RULES, KINDS, Alert, AlertEngine, PriceLevel

log = logging.getLogger("analytix.alerts")

TELEGRAM_API = "https://api.telegram.org"
CHECK_EVERY_S = 1.0
RECENT = 100
SEND_GAP_S = 0.05                 # Telegram allows ~30 messages a second overall; stay far below
NUMERIC_LIMITS = {"min_usd": (0, 1e10), "min_usd_per_hour": (0, 1e10), "min_ratio": (1.0, 20.0)}


class AlertError(ValueError):
    pass


class AlertService:
    def __init__(self, runtime, path: Path = ALERTS_FILE):
        self.rt = runtime
        self.path = Path(path)
        self.cfg = self._load()
        self.engine = AlertEngine(self.cfg["rules"], self.cfg["cooldown_min"], self.cfg["coins"])
        self.engine.levels = [PriceLevel(**lv) for lv in self.cfg["price_levels"]]
        self.recent: deque[Alert] = deque(maxlen=RECENT)
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=500)
        self._tasks: list[asyncio.Task] = []
        self.sent = 0
        self.last_error: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(), name="alerts"),
                       asyncio.create_task(self._sender(), name="telegram")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY_S)
            try:
                self.check_once()
            except Exception:                              # one bad check must not stop alerts for good
                log.exception("alert check failed")

    def check_once(self) -> list[Alert]:
        fired: list[Alert] = []
        for coin, pipe in self.rt.pipelines.items():
            fired += self.engine.check(coin, pipe)
        for a in fired:
            self.recent.append(a)
            log.info("alert: %s", a.title)
            if a.kind == "price":
                self._sync_levels()
            if self.telegram_ready:
                try:
                    self._queue.put_nowait(a)
                except asyncio.QueueFull:
                    log.warning("telegram queue full; dropping %s", a.id)
        return fired

    # ── settings ───────────────────────────────────────────────────────────
    def _load(self) -> dict:
        cfg = {"rules": {k: dict(v) for k, v in DEFAULT_RULES.items()}, "cooldown_min": DEFAULT_COOLDOWN_MIN,
               "coins": [], "price_levels": [], "telegram": {"token": "", "bot": "", "chats": []}}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError) as exc:
            log.warning("couldn't read %s (%s); using defaults", self.path, exc)
            return cfg
        for k, v in (saved.get("rules") or {}).items():
            if k in cfg["rules"] and isinstance(v, dict):
                cfg["rules"][k].update(v)
        cfg["cooldown_min"] = float(saved.get("cooldown_min", cfg["cooldown_min"]))
        cfg["coins"] = list(saved.get("coins") or [])
        cfg["price_levels"] = list(saved.get("price_levels") or [])
        cfg["telegram"].update(saved.get("telegram") or {})
        return cfg

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)                                # it holds the bot token
        os.replace(tmp, self.path)

    def _sync_levels(self) -> None:
        self.cfg["price_levels"] = [asdict(lv) for lv in self.engine.levels]
        self._save()

    def public(self) -> dict:
        """Settings for the dashboard: everything except the token itself."""
        tg = self.cfg["telegram"]
        return {
            "rules": self.cfg["rules"], "cooldown_min": self.cfg["cooldown_min"], "coins": self.cfg["coins"],
            "price_levels": self.cfg["price_levels"], "kinds": list(KINDS), "watching": self.rt.coins,
            "telegram": {"connected": bool(tg.get("token")), "bot": tg.get("bot", ""), "chats": tg.get("chats", []),
                         "enabled": self.rt.mode == "live", "sent": self.sent, "last_error": self.last_error},
            "recent": [asdict(a) for a in reversed(self.recent)][:30],
        }

    def update(self, patch: dict) -> dict:
        rules = patch.get("rules") or {}
        for kind, vals in rules.items():
            if kind not in self.cfg["rules"] or not isinstance(vals, dict):
                raise AlertError(f"unknown alert '{kind}'")
            for key, val in vals.items():
                if key == "on":
                    self.cfg["rules"][kind]["on"] = bool(val)
                elif key in NUMERIC_LIMITS and key in self.cfg["rules"][kind]:
                    lo, hi = NUMERIC_LIMITS[key]
                    if not isinstance(val, (int, float)) or not lo <= float(val) <= hi:
                        raise AlertError(f"{kind}.{key} must be between {lo:g} and {hi:g}")
                    self.cfg["rules"][kind][key] = float(val)
                else:
                    raise AlertError(f"unknown setting '{kind}.{key}'")
        if "cooldown_min" in patch:
            cd = patch["cooldown_min"]
            if not isinstance(cd, (int, float)) or not 1 <= cd <= 1440:
                raise AlertError("cooldown must be between 1 and 1440 minutes")
            self.cfg["cooldown_min"] = float(cd)
        if "coins" in patch:
            coins = patch["coins"]
            if not isinstance(coins, list) or any(c not in self.rt.coins for c in coins):
                raise AlertError(f"coins must be some of {self.rt.coins}")
            self.cfg["coins"] = coins
        self.engine.rules = {k: dict(v) for k, v in self.cfg["rules"].items()}
        self.engine.cooldown_ms = int(self.cfg["cooldown_min"] * 60_000)
        self.engine.coins = list(self.cfg["coins"])
        self._save()
        return self.public()

    def add_level(self, coin: str, price: float) -> dict:
        if coin not in self.rt.coins:
            raise AlertError(f"not watching {coin}")
        if not price > 0:
            raise AlertError("price must be above zero")
        self.engine.levels.append(PriceLevel(uuid.uuid4().hex[:8], coin, float(price), int(time.time() * 1000)))
        self._sync_levels()
        return self.public()

    def remove_level(self, level_id: str) -> dict:
        before = len(self.engine.levels)
        self.engine.levels = [lv for lv in self.engine.levels if lv.id != level_id]
        if len(self.engine.levels) == before:
            raise AlertError("no such price alert")
        self._sync_levels()
        return self.public()

    # ── telegram ───────────────────────────────────────────────────────────
    @property
    def telegram_ready(self) -> bool:
        tg = self.cfg["telegram"]
        return self.rt.mode == "live" and bool(tg.get("token")) and any(c.get("on") for c in tg.get("chats", []))

    async def _api(self, method: str, token: str | None = None, **params) -> dict:
        token = token or self.cfg["telegram"].get("token")
        if not token:
            raise AlertError("connect a Telegram bot first")
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(3):
                r = await client.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params)
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code == 429:                      # rate limited: wait as told, then retry
                    await asyncio.sleep(float((data.get("parameters") or {}).get("retry_after", 3)))
                    continue
                if not data.get("ok"):
                    raise AlertError(data.get("description") or f"Telegram said HTTP {r.status_code}")
                return data["result"]
        raise AlertError("Telegram kept rate-limiting; try again in a minute")

    async def set_token(self, token: str) -> dict:
        token = token.strip()
        if ":" not in token:
            raise AlertError("that doesn't look like a bot token (it has a colon in it: 123456:ABC…)")
        me = await self._api("getMe", token=token)
        tg = self.cfg["telegram"]
        if tg.get("token") != token:
            tg["chats"] = []                                   # a different bot: its chats are different
        tg.update(token=token, bot=f"@{me.get('username', '')}")
        self._save()
        return self.public()

    def disconnect(self) -> dict:
        self.cfg["telegram"] = {"token": "", "bot": "", "chats": []}
        self._save()
        return self.public()

    async def discover(self) -> dict:
        """Chats that have messaged the bot (or added it) recently. New ones start switched off."""
        updates = await self._api("getUpdates", limit=100, timeout=0)
        known = {c["id"]: c for c in self.cfg["telegram"].get("chats", [])}
        for u in updates:
            for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
                chat = (u.get(key) or {}).get("chat")
                if not chat or chat["id"] in known:
                    continue
                name = chat.get("title") or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))) \
                    or chat.get("username") or str(chat["id"])
                known[chat["id"]] = {"id": chat["id"], "title": name, "type": chat.get("type", ""), "on": False}
        self.cfg["telegram"]["chats"] = list(known.values())
        self._save()
        return self.public()

    def set_chat(self, chat_id: int, on: bool) -> dict:
        for c in self.cfg["telegram"].get("chats", []):
            if c["id"] == chat_id:
                c["on"] = bool(on)
                self._save()
                return self.public()
        raise AlertError("no such chat: press Find chats first")

    async def test(self) -> dict:
        chats = [c for c in self.cfg["telegram"].get("chats", []) if c.get("on")]
        if not chats:
            raise AlertError("switch on at least one chat first")
        coins = ", ".join(self.rt.coins)
        text = (f"✅ <b>Analytix alerts connected</b>\nWatching {coins}. You'll get the alerts that are switched on "
                f"in the dashboard's Alerts panel.")
        for c in chats:
            await self._api("sendMessage", chat_id=c["id"], text=text, parse_mode="HTML",
                            disable_web_page_preview=True)
        return self.public()

    async def _sender(self) -> None:
        while True:
            alert = await self._queue.get()
            for c in [c for c in self.cfg["telegram"].get("chats", []) if c.get("on")]:
                try:
                    await self._api("sendMessage", chat_id=c["id"], text=alert.telegram_html(), parse_mode="HTML",
                                    disable_web_page_preview=True)
                    self.sent += 1
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                    log.warning("telegram send to %s failed: %s", c.get("title"), exc)
                await asyncio.sleep(SEND_GAP_S)

"""
The bot's baskets: separate strategies in one bot, each with its own balance, settings, record and alerts, so one
can never drag down another's numbers.

    twap       big TWAPs (api/bot.py)
    informed   informed flow: hidden orders from wallets whose trades move price for good. In research: it trades
               nothing until its model passes the same tests as everything else (walk-forward, honest timing, both
               halves)
"""
import json
import logging
import time
from pathlib import Path

from api.bot import KNOWN_COINS, LIMITS, BotError, BotService

log = logging.getLogger("analytix.bot")


class InformedBasket:
    key, name = "informed", "Informed flow"
    DEFAULTS = {"alerts": False, "telegram": False, "coins": ["BTC", "ETH"], "balance": 50.0, "leverage": 200.0,
                "margin_pct": 20.0}

    def __init__(self, runtime, path: Path):
        self.rt = runtime
        self.path = Path(path)
        self.cfg = dict(self.DEFAULTS)
        self.shadow = self._fresh_shadow()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.cfg.update({k: v for k, v in (saved.get("settings") or {}).items() if k in self.DEFAULTS})
            self.shadow.update(saved.get("shadow") or {})
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            log.warning("could not read %s; using defaults", self.path)

    def _fresh_shadow(self) -> dict:
        start = float(self.cfg["balance"])
        return {"start": start, "balance": start, "low": start, "trades": 0, "wins": 0, "busts": 0, "free_ms": 0,
                "busted": False, "since_ms": int(time.time() * 1000)}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"settings": self.cfg, "shadow": self.shadow}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def on_message(self, msg: dict) -> None:
        pass

    def status(self) -> str:
        return "research"

    def live(self) -> dict:
        return {"status": "research", "alert": None}

    def public(self) -> dict:
        return {"status": "research", "settings": self.cfg, "limits": LIMITS, "known_coins": list(KNOWN_COINS),
                "shadow": {**self.shadow, "margin": self.cfg["margin_pct"] / 100 * self.shadow["start"]},
                "now_ms": int(time.time() * 1000)}

    def save_settings(self, patch: dict) -> dict:
        new = dict(self.cfg)
        for k, v in (patch or {}).items():
            if k not in ("balance", "leverage", "margin_pct"):
                raise BotError("While it's in research, only balance, leverage and margin can be set.")
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise BotError(f"{k.replace('_', ' ')} must be a number.") from None
            lo, hi = LIMITS[k]
            if not lo <= x <= hi:
                raise BotError(f"{k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
            new[k] = x
        restart = any(new[k] != self.cfg[k] for k in ("balance", "leverage", "margin_pct"))
        self.cfg = new
        if restart:
            self.shadow = self._fresh_shadow()
        self._save()
        return self.public()

    def reset_shadow(self) -> dict:
        self.shadow = self._fresh_shadow()
        self._save()
        return self.public()


class Bot:
    """All the baskets behind one bot: the feed goes to each, the dashboard reads one at a time."""

    def __init__(self, runtime, path: Path, db_path: Path | None = None):
        path = Path(path)
        self.twap = BotService(runtime, path, db_path)
        self.twap.key, self.twap.name = "twap", "TWAPs"
        self.informed = InformedBasket(runtime, path.with_name(path.stem + "_informed.json"))
        self.baskets = {"twap": self.twap, "informed": self.informed}

    def get(self, key: str | None):
        b = self.baskets.get(key or "twap")
        if b is None:
            raise BotError(f"No basket called {key}. There's {', '.join(self.baskets)}.")
        return b

    async def start(self) -> None:
        for b in self.baskets.values():
            await b.start()

    async def stop(self) -> None:
        for b in self.baskets.values():
            await b.stop()

    def on_message(self, msg: dict) -> None:
        for b in self.baskets.values():
            b.on_message(msg)

    def live(self) -> dict:
        """What every snapshot carries: the first live call across baskets, labelled with its basket."""
        out = {"status": self.twap.status(), "alert": None}
        for key, b in self.baskets.items():
            lv = b.live()
            if lv.get("alert"):
                out = {"status": lv["status"], "alert": {**lv["alert"], "basket": key, "basket_name": b.name}}
                break
        return out

    def public(self, key: str | None = None) -> dict:
        b = self.get(key)
        data = b.public()
        data["basket"] = b.key
        data["baskets"] = [{"key": k, "name": x.name, "status": x.status(), "balance": x.shadow["balance"],
                            "start": x.shadow["start"]} for k, x in self.baskets.items()]
        return data

    def save_settings(self, key: str | None, patch: dict) -> dict:
        self.get(key).save_settings(patch)
        return self.public(key)

    def reset_shadow(self, key: str | None) -> dict:
        self.get(key).reset_shadow()
        return self.public(key)

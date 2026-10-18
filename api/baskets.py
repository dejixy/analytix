"""
The bot's baskets: separate strategies in one bot, each with its own balance, settings, record and alerts, so one
can never drag down another's numbers.

    twap       big TWAPs (api/bot.py)
    informed   informed flow: fading hidden orders from wallets whose pushes reliably reverse (api/informed.py). A
               live test: shadow-traded and scored, alerts off unless you turn them on
"""
import logging
from pathlib import Path

from api.bot import BotError, BotService
from api.informed import InformedBasket

log = logging.getLogger("analytix.bot")


class Bot:
    """All the baskets behind one bot: the feed goes to each, the dashboard reads one at a time."""

    def __init__(self, runtime, path: Path, db_path: Path | None = None):
        path = Path(path)
        self.twap = BotService(runtime, path, db_path)
        self.twap.key, self.twap.name = "twap", "TWAPs"
        self.informed = InformedBasket(runtime, path.with_name(path.stem + "_informed.json"), db_path)
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

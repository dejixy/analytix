"""
The bot's baskets: separate strategies in one bot, each with its own balance, settings, record and alerts, so one
can never drag down another's numbers.

    twap       big TWAPs (api/bot.py)
    informed   informed flow: fading hidden orders from wallets whose pushes reliably reverse (api/informed.py). A
               live test: shadow-traded and scored, alerts off unless you turn them on
    degen      the informed basket's SOL and HYPE fades, sized to double a pot in a week or lose half (api/degen.py)

Only the baskets in BOT_SHOWN appear in the panel. The rest keep scoring, shadow-trading and saving their records
in the background, but silently: they never put a call on the banner or send Telegram, since nothing on screen
could turn them off.
"""
import logging
from pathlib import Path

from api.bot import BotError, BotService
from config import BOT_SHOWN
from api.degen import DegenBasket
from api.informed import InformedBasket

log = logging.getLogger("analytix.bot")


class Bot:
    """All the baskets behind one bot: the feed goes to each, the dashboard reads one at a time."""

    def __init__(self, runtime, path: Path, db_path: Path | None = None, shown: tuple[str, ...] = BOT_SHOWN):
        path = Path(path)
        self.twap = BotService(runtime, path, db_path)
        self.twap.key, self.twap.name = "twap", "TWAPs"
        self.informed = InformedBasket(runtime, path.with_name(path.stem + "_informed.json"), db_path)
        self.degen = DegenBasket(runtime, path.with_name(path.stem + "_degen.json"), self.informed)
        self.baskets = {"twap": self.twap, "informed": self.informed, "degen": self.degen}
        self.shown = [k for k in shown if k in self.baskets] or list(self.baskets)
        for k, b in self.baskets.items():
            b.hidden = k not in self.shown                       # hidden: still runs, never calls out

    def get(self, key: str | None):
        b = self.baskets.get(key or self.shown[0])
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
        out = {"status": self.baskets[self.shown[0]].status(), "alert": None}
        for key in self.shown:
            b = self.baskets[key]
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
                            "start": x.shadow["start"]} for k, x in self.baskets.items() if k in self.shown]
        return data

    def history(self, key: str | None, days: float | None = None) -> dict:
        b = self.get(key)
        if not hasattr(b, "history"):
            raise BotError(f"{b.name} doesn't keep a full trade history; Degen does.")
        return b.history(days)

    def save_settings(self, key: str | None, patch: dict) -> dict:
        self.get(key).save_settings(patch)
        return self.public(key)

    def reset_shadow(self, key: str | None) -> dict:
        self.get(key).reset_shadow()
        return self.public(key)

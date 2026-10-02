"""
Runtime: owns one pipeline per coin, the data source (live socket or replay
file) and the connected browsers. One instance per app.

Live mode subscribes every coin in COINS on a single socket and routes each
message to its coin's pipeline. Replay mode plays one recorded coin.

Live mode also keeps the long-window history: minute bars are saved to SQLite
as they close and reloaded on start, and price candles are backfilled from
Hyperliquid's REST API for the time the app wasn't running. The outlook's
track record is saved the same way, so a 24h hit rate isn't reset by a restart.
"""
import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from config import (
    BACKFILL_ENABLED,
    BAR_HISTORY_S,
    BARS_DB,
    OUTLOOK_DB,
    BROADCAST_INTERVAL_S,
    COIN,
    COINS,
    RECORD_FILE,
    REPLAY_FILE,
    REPLAY_LOOP,
    REPLAY_SPEED,
)
from ingestion.backfill import backfill
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import coin_of
from ingestion.recorder import JsonlRecorder
from ingestion.replay import ReplaySource
from ingestion.synthetic import generate_session
from pipeline import Pipeline
from storage.barStore import BarStore
from storage.trackerStore import TrackerStore

BARS_EVERY_N_BROADCASTS = 10   # the long-window chart changes slowly; send it every ~5s, not twice a second
TRACK_COMMIT_S = 10            # how often the outlook track record is written to disk

log = logging.getLogger("analytix.runtime")


class Runtime:
    def __init__(self, mode: str, coins: list[str] | None = None, default_coin: str = COIN,
                 replay_file: Path = REPLAY_FILE, speed: float = REPLAY_SPEED, loop: bool = REPLAY_LOOP,
                 record_file: str | None = RECORD_FILE, broadcast_interval_s: float = BROADCAST_INTERVAL_S,
                 bars_db: Path | None = BARS_DB, backfill_enabled: bool = BACKFILL_ENABLED,
                 outlook_db: Path | None = OUTLOOK_DB):
        if mode not in ("live", "replay"):
            raise ValueError(f"ANALYTIX_MODE must be 'live' or 'replay', got {mode!r}")
        self.mode = mode
        self.replay_file = Path(replay_file)
        # Live mode watches the configured list; a replay watches whatever coins the recording holds.
        self.coins = list(coins or COINS) if mode == "live" else (_coins_in(self.replay_file) or [default_coin])
        self.default_coin = default_coin if default_coin in self.coins else self.coins[0]
        self.speed = speed
        self.loop = loop
        self.record_file = record_file
        self.broadcast_interval_s = broadcast_interval_s
        self.pipelines: dict[str, Pipeline] = {c: Pipeline(c) for c in self.coins}
        self.status = FeedStatus(mode=mode)
        self.clients: dict[WebSocket, str] = {}      # browser → the coin it's watching
        self._tasks: list[asyncio.Task] = []
        self._recorder: JsonlRecorder | None = None
        # History persistence and backfill are live-only: a replay must never write into the live history.
        self.bars_db = bars_db if mode == "live" else None
        self.outlook_db = outlook_db if mode == "live" else None
        self.backfill_enabled = backfill_enabled and mode == "live"
        self.store: BarStore | None = None
        self.track_store: TrackerStore | None = None
        self._broadcasts = 0

    # ── pipelines ───────────────────────────────────────────────────────────
    def pipeline(self, coin: str | None = None) -> Pipeline:
        return self.pipelines[coin or self.default_coin]

    def on_message(self, msg: dict[str, Any]) -> None:
        pipe = self.pipelines.get(coin_of(msg) or "")
        if pipe:
            pipe.on_message(msg)

    def reset(self) -> None:
        for pipe in self.pipelines.values():
            pipe.reset()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.mode == "replay":
            if not self.replay_file.exists():
                log.info("no replay file at %s — generating the synthetic sample", self.replay_file)
                generate_session(self.replay_file, coin=self.default_coin)
                self.coins = _coins_in(self.replay_file) or [self.default_coin]
                self.pipelines = {c: Pipeline(c) for c in self.coins}
            source = ReplaySource(self.replay_file, self.on_message, self.status,
                                  speed=self.speed, loop=self.loop, on_reset=self.reset)
        else:
            if self.record_file:
                self._recorder = JsonlRecorder(self.record_file)
            self._load_history()
            self._load_track_records()
            source = HyperliquidClient(self.coins, self.on_message, self.status, recorder=self._recorder)
        self._tasks = [asyncio.create_task(source.run(), name="source"),
                       asyncio.create_task(self._broadcast_loop(), name="broadcast")]
        if self.backfill_enabled:
            self._tasks.append(asyncio.create_task(self._backfill(), name="backfill"))
        if self.track_store:
            self._tasks.append(asyncio.create_task(self._commit_loop(), name="track-commit"))
        log.info("analytix %s mode started for %s", self.mode, ", ".join(self.coins))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._recorder:
            self._recorder.close()
        if self.store:
            self.store.close()
        if self.track_store:
            self.track_store.close()

    # ── long-window history ─────────────────────────────────────────────────
    def _load_history(self) -> None:
        if not self.bars_db:
            return
        try:
            self.store = BarStore(self.bars_db)
            since = int(time.time() * 1000) - BAR_HISTORY_S * 1000
            self.store.prune(since - 86_400_000)
            for coin, pipe in self.pipelines.items():
                n = pipe.state.merge_bars(self.store.load(coin, since))
                pipe.state.on_bar = self.store.save
                log.info("loaded %d saved minute bars for %s", n, coin)
        except Exception:
            log.exception("could not open the bar history at %s — long windows will start empty", self.bars_db)
            self.store = None

    def _load_track_records(self) -> None:
        if not self.outlook_db:
            return
        try:
            self.track_store = TrackerStore(self.outlook_db)
            for coin, pipe in self.pipelines.items():
                n = pipe.analyzer.tracker.attach(self.track_store, coin)
                log.info("loaded %d saved outlook samples for %s", n, coin)
            self.track_store.commit()
        except Exception:
            log.exception("could not open the outlook track record at %s — it will start empty", self.outlook_db)
            self.track_store = None

    async def _commit_loop(self) -> None:
        while True:
            await asyncio.sleep(TRACK_COMMIT_S)
            if self.track_store:
                self.track_store.commit()

    async def _backfill(self) -> None:
        for coin, pipe in self.pipelines.items():
            try:
                bars = await backfill(coin, BAR_HISTORY_S)
                added = pipe.state.merge_bars(bars)
                log.info("backfill %s: %d candles merged", coin, added)
            except Exception as exc:  # network down, API change — long windows just warm up live
                log.warning("backfill for %s failed: %s", coin, exc)

    # ── browser push ────────────────────────────────────────────────────────
    def snapshot(self, coin: str | None = None, include_bars: bool = True) -> dict:
        from api.serializers import build_snapshot
        return build_snapshot(self, coin or self.default_coin, include_bars=include_bars)

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(self.broadcast_interval_s)
            if not self.clients:
                continue
            self._broadcasts += 1
            with_bars = self._broadcasts % BARS_EVERY_N_BROADCASTS == 0
            payloads: dict[str, str] = {}   # one snapshot per watched coin, shared by its viewers
            dead = []
            for ws, coin in list(self.clients.items()):
                try:
                    if coin not in payloads:
                        payloads[coin] = json.dumps(self.snapshot(coin, include_bars=with_bars),
                                                    separators=(",", ":"), allow_nan=False)
                    await ws.send_text(payloads[coin])
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.pop(ws, None)

    @property
    def speed_label(self) -> str:
        return "max" if math.isinf(self.speed) else f"{self.speed:g}×"


def _coins_in(path: Path, max_lines: int = 20_000) -> list[str]:
    """Coins present in a recording, in order of first appearance."""
    if not path.exists():
        return []
    seen: dict[str, None] = {}
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            try:
                coin = coin_of(json.loads(line)["msg"])
            except (ValueError, KeyError, TypeError):
                continue
            if coin:
                seen.setdefault(coin)
    return list(seen)

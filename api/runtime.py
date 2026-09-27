"""
Runtime: owns one pipeline per coin, the data source (live socket or replay
file) and the connected browsers. One instance per app.

Live mode subscribes every coin in COINS on a single socket and routes each
message to its coin's pipeline. Replay mode plays one recorded coin.
"""
import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from config import BROADCAST_INTERVAL_S, COIN, COINS, RECORD_FILE, REPLAY_FILE, REPLAY_LOOP, REPLAY_SPEED
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import coin_of
from ingestion.recorder import JsonlRecorder
from ingestion.replay import ReplaySource
from ingestion.synthetic import generate_session
from pipeline import Pipeline

log = logging.getLogger("analytix.runtime")


class Runtime:
    def __init__(self, mode: str, coins: list[str] | None = None, default_coin: str = COIN,
                 replay_file: Path = REPLAY_FILE, speed: float = REPLAY_SPEED, loop: bool = REPLAY_LOOP,
                 record_file: str | None = RECORD_FILE, broadcast_interval_s: float = BROADCAST_INTERVAL_S):
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
            source = HyperliquidClient(self.coins, self.on_message, self.status, recorder=self._recorder)
        self._tasks = [asyncio.create_task(source.run(), name="source"),
                       asyncio.create_task(self._broadcast_loop(), name="broadcast")]
        log.info("analytix %s mode started for %s", self.mode, ", ".join(self.coins))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._recorder:
            self._recorder.close()

    # ── browser push ────────────────────────────────────────────────────────
    def snapshot(self, coin: str | None = None) -> dict:
        from api.serializers import build_snapshot
        return build_snapshot(self, coin or self.default_coin)

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(self.broadcast_interval_s)
            if not self.clients:
                continue
            payloads: dict[str, str] = {}   # one snapshot per watched coin, shared by its viewers
            dead = []
            for ws, coin in list(self.clients.items()):
                try:
                    if coin not in payloads:
                        payloads[coin] = json.dumps(self.snapshot(coin), separators=(",", ":"), allow_nan=False)
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

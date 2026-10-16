"""
Runtime: owns one pipeline per coin, the data source (live socket or replay
file) and the connected browsers. One instance per app.

Live mode subscribes every coin in COINS on a single socket and routes each
message to its coin's pipeline. Replay mode plays one recorded coin.

Live mode also keeps the long-window history: minute bars are saved to SQLite
as they close and reloaded on start, and price candles are backfilled from
Hyperliquid's REST API for the time the app wasn't running.
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
    ALERTS_FILE,
    BOT_FILE,
    BACKFILL_ENABLED,
    BAR_HISTORY_S,
    BARS_DB,
    POSITIONS_DB,
    BROADCAST_INTERVAL_S,
    COIN,
    COINS,
    EXTERNAL_VENUES,
    RECORD,
    RECORD_DIR,
    RECORD_FILE,
    RECORD_MIN_FREE_GB,
    REPLAY_FILE,
    REPLAY_LOOP,
    REPLAY_SPEED,
    WATCH_FILE,
)
from api.alerts import AlertService
from api.bot import BotService
from api.holdclock import HoldClock
from api.liqmap import LiqMapCollector
from api.outcomes import OutcomeService
from api.planner import PlannerService
from api.positions import PositionWatch
from api.scanner import Scanner
from api.speed import Speed
from ingestion.backfill import backfill
from ingestion.external import ExternalFeed
from ingestion.feedStatus import FeedStatus
from ingestion.hyperliquidClient import HyperliquidClient
from ingestion.parsers import coin_of, subscriptions, trade_subscription
from ingestion.recorder import HourlyRecorder, JsonlRecorder
from ingestion.replay import ReplaySource, coins_in
from ingestion.synthetic import generate_session
from pipeline import Pipeline
from storage.barStore import BarStore
from storage.eventStore import KEEP_MS, EventStore

BARS_EVERY_N_BROADCASTS = 10
HEARTBEAT_S = 300   # the long-window chart changes slowly; send it every ~5s, not twice a second
EVENTS_KEPT = 200                 # per coin and kind, loaded back after a restart
EVENTS_SAVE_S = 30
MAX_EXTRA = 3                     # coins opened from More coins with a full dashboard at once
EXTRA_IDLE_S = 600                # an opened coin nobody has viewed for this long is closed

log = logging.getLogger("analytix.runtime")


class Runtime:
    def __init__(self, mode: str, coins: list[str] | None = None, default_coin: str = COIN,
                 replay_file: Path = REPLAY_FILE, speed: float = REPLAY_SPEED, loop: bool = REPLAY_LOOP,
                 record_file: str | None = RECORD_FILE, record: bool = RECORD, broadcast_interval_s: float = BROADCAST_INTERVAL_S,
                 bars_db: Path | None = BARS_DB, backfill_enabled: bool = BACKFILL_ENABLED,
                 alerts_file: Path = ALERTS_FILE, watch_file: Path = WATCH_FILE, bot_file: Path = BOT_FILE,
                 positions_db: Path | None = POSITIONS_DB):
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
        self.record = record
        self.broadcast_interval_s = broadcast_interval_s
        self.pipelines: dict[str, Pipeline] = {c: Pipeline(c) for c in self.coins}
        self.status = FeedStatus(mode=mode)
        self.clients: dict[WebSocket, str] = {}      # browser → the coin it's watching
        self._tasks: list[asyncio.Task] = []
        self._recorder: JsonlRecorder | HourlyRecorder | None = None
        # History persistence and backfill are live-only: a replay must never write into the live history.
        self.bars_db = bars_db if mode == "live" else None
        self.backfill_enabled = backfill_enabled and mode == "live"
        self.store: BarStore | None = None
        self.event_store: EventStore | None = None
        self._broadcasts = 0
        self.planner = PlannerService(self)
        self.alerts = AlertService(self, alerts_file)
        self.positions = PositionWatch(self, watch_file)
        self.perf = Speed()
        for pipe in self.pipelines.values():
            pipe.analyzer.on_run_ms = self.perf.engine.add
        self.scanner = Scanner(self)
        self.scanner.on_top_change = self._resubscribe_aux
        self.aux: HyperliquidClient | None = None       # second socket: scanner trades and coins opened from More coins
        self.extra: dict[str, float] = {}               # coin opened from More coins → when it was last viewed
        self.external: ExternalFeed | None = None       # Binance and Bybit prices, for where moves start
        self.outcomes = OutcomeService(self)            # Hyperliquid's prediction markets (HIP-4), own socket
        self.hold = HoldClock(self, self.bars_db)       # how long very high leverage survives, from the live book
        self.bot = BotService(self, bot_file, self.bars_db)   # big-mover alerts, with a shadow record
        self.liqmap = LiqMapCollector(self, positions_db if mode == "live" else None)   # big traders' liquidation prices
        self._saved_fp: dict[tuple, tuple] = {}

    # ── pipelines ───────────────────────────────────────────────────────────
    def pipeline(self, coin: str | None = None) -> Pipeline:
        return self.pipelines[coin or self.default_coin]

    def on_message(self, msg: dict[str, Any]) -> None:
        coin = coin_of(msg) or ""
        pipe = self.pipelines.get(coin)
        if pipe:
            pipe.on_message(msg)
            if msg.get("channel") in ("trades", "l2Book"):
                try:
                    self.bot.on_message(msg)              # big-mover alerts, the moment a listed wallet trades
                except Exception:
                    log.exception("bot feed check failed")
            if msg.get("channel") == "trades":
                try:
                    self.liqmap.on_message(msg)           # who trades most, for the liquidation map
                except Exception:
                    log.exception("liquidation map feed check failed")

    def reset(self) -> None:
        for pipe in self.pipelines.values():
            pipe.reset()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.mode == "replay":
            if not self.replay_file.exists():
                log.info("no replay file at %s, so generating the synthetic sample", self.replay_file)
                generate_session(self.replay_file, coin=self.default_coin)
                self.coins = _coins_in(self.replay_file) or [self.default_coin]
                self.pipelines = {c: Pipeline(c) for c in self.coins}
            source = ReplaySource(self.replay_file, self.on_message, self.status,
                                  speed=self.speed, loop=self.loop, on_reset=self.reset)
        else:
            if self.record_file:
                self._recorder = JsonlRecorder(self.record_file)
            elif self.record:
                self._recorder = HourlyRecorder(RECORD_DIR, recording_prefix(self.coins), min_free_gb=RECORD_MIN_FREE_GB)
                log.info("recording the live feed to %s", RECORD_DIR)
            self._load_history()
            source = HyperliquidClient(self.coins, self.on_message, self.status, recorder=self._recorder)
            if EXTERNAL_VENUES:
                self.external = ExternalFeed(self.coins, self.on_message, recorder=self._recorder,
                                             venues=EXTERNAL_VENUES)
        self._tasks = [asyncio.create_task(source.run(), name="source"),
                       asyncio.create_task(self._broadcast_loop(), name="broadcast")]
        if self.backfill_enabled:
            self._tasks.append(asyncio.create_task(self._backfill(), name="backfill"))
        if self.mode == "live":
            self._tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))
            self._tasks.append(asyncio.create_task(self.perf.watch(), name="speed"))
            self._tasks.append(asyncio.create_task(self._save_events_loop(), name="events"))
            self.aux = HyperliquidClient([], self._on_aux, FeedStatus(mode="live"))
            self._tasks.append(asyncio.create_task(self.aux.run(), name="aux"))
            self._tasks.append(asyncio.create_task(self._extras_loop(), name="extras"))
            if self.external:
                self._tasks.append(asyncio.create_task(self.external.run(), name="external"))
        await self.scanner.start()
        await self.outcomes.start()
        await self.hold.start()
        await self.bot.start()
        await self.liqmap.start()
        await self.alerts.start()
        await self.positions.start()
        log.info("analytix %s mode started for %s", self.mode, ", ".join(self.coins))

    async def stop(self) -> None:
        await self.scanner.stop()
        await self.outcomes.stop()
        await self.hold.stop()
        await self.bot.stop()
        await self.liqmap.stop()
        await self.positions.stop()
        await self.alerts.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._recorder:
            self._recorder.close()
        self._save_events()
        if self.event_store:
            self.event_store.close()
        if self.store:
            self.store.close()

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
            log.exception("could not open the bar history at %s, so long windows will start empty", self.bars_db)
            self.store = None
        try:
            self.event_store = EventStore(self.bars_db)
            since = int(time.time() * 1000) - KEEP_MS
            self.event_store.prune(since)
            for coin, pipe in self.pipelines.items():
                an = pipe.analyzer
                an.archived_moves = self.event_store.load(coin, "move", since, EVENTS_KEPT)
                an.archived_market = self.event_store.load(coin, "market", since, EVENTS_KEPT)
                log.info("loaded %d saved events for %s", len(an.archived_moves) + len(an.archived_market), coin)
        except Exception:
            log.exception("could not open the saved event log, so it starts empty")
            self.event_store = None

    def _save_events(self) -> None:
        """Write new and changed events to disk (live mode), so a restart doesn't empty the event log."""
        if not self.event_store:
            return
        for coin in list(self.pipelines):
            self._save_coin_events(coin)

    def _save_coin_events(self, coin: str) -> None:
        from api.serializers import market_record, move_record
        pipe = self.pipelines.get(coin)
        if not self.event_store or pipe is None:
            return
        an, rows = pipe.analyzer, []
        for e in an.events.recent(EVENTS_KEPT):              # only what's new or changed since the last save
            fp = (e.peak_ms, e.ended_ms)
            if self._saved_fp.get((coin, "m", e.id)) != fp:
                self._saved_fp[(coin, "m", e.id)] = fp
                rows.append(move_record(e))
        for e in an.market_events():
            fp = (e.title, e.detail)
            if self._saved_fp.get((coin, "x", e.id, e.ts)) != fp:
                self._saved_fp[(coin, "x", e.id, e.ts)] = fp
                rows.append(market_record(e))
        if rows:
            self.event_store.save(coin, rows)

    async def _save_events_loop(self, every_s: float = EVENTS_SAVE_S) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                self._save_events()
            except Exception:
                log.exception("saving the event log failed")

    # ── More coins: dashboards opened on demand ─────────────────────────────
    def open_coin(self, coin: str) -> bool:
        """Make sure `coin` has a live pipeline. Coins outside the configured list are opened on demand (live
        mode, any listed perp), up to MAX_EXTRA at a time, and closed again once nobody has viewed them for a
        while. Returns False when it can't be opened."""
        if coin in self.pipelines:
            if coin in self.extra:
                self.extra[coin] = time.time()
            return True
        if self.mode != "live" or not self.scanner.knows(coin):
            return False
        if len(self.extra) >= MAX_EXTRA:
            viewed = set(self.clients.values())
            idle = [c for c in self.extra if c not in viewed]
            if not idle:
                return False
            self.close_coin(min(idle, key=lambda c: self.extra[c]))
        pipe = Pipeline(coin)
        since = int(time.time() * 1000) - BAR_HISTORY_S * 1000
        if self.store:
            pipe.state.merge_bars(self.store.load(coin, since))
            pipe.state.on_bar = self.store.save
        if self.event_store:
            keep = int(time.time() * 1000) - KEEP_MS
            pipe.analyzer.archived_moves = self.event_store.load(coin, "move", keep, EVENTS_KEPT)
            pipe.analyzer.archived_market = self.event_store.load(coin, "market", keep, EVENTS_KEPT)
        self.scanner.states.pop(coin, None)
        pipe.analyzer.on_run_ms = self.perf.engine.add
        self.pipelines[coin] = pipe
        self.extra[coin] = time.time()
        self._resubscribe_aux()
        if self.backfill_enabled:
            asyncio.get_running_loop().create_task(self._backfill_one(coin))
        log.info("opened %s from More coins", coin)
        return True

    def close_coin(self, coin: str) -> None:
        if coin not in self.extra:
            return
        self._save_coin_events(coin)
        self.extra.pop(coin, None)
        self.pipelines.pop(coin, None)
        self._resubscribe_aux()
        log.info("closed %s (nobody viewing it)", coin)

    def _resubscribe_aux(self) -> None:
        if self.aux is None:
            return
        subs = [trade_subscription(c) for c in self.scanner.trade_coins()]
        subs += [s for c in self.extra for s in subscriptions(c)]
        self.aux.set_subscriptions(subs)

    def _on_aux(self, msg: dict[str, Any]) -> None:
        coin = coin_of(msg) or ""
        if coin in self.extra and coin in self.pipelines:
            self.pipelines[coin].on_message(msg)
        else:
            self.scanner.on_message(msg)

    async def _extras_loop(self, every_s: float = 60) -> None:
        while True:
            await asyncio.sleep(every_s)
            viewed, now = set(self.clients.values()), time.time()
            for coin in list(self.extra):
                if coin in viewed:
                    self.extra[coin] = now
                elif now - self.extra[coin] > EXTRA_IDLE_S:
                    self.close_coin(coin)

    async def _backfill_one(self, coin: str) -> None:
        try:
            bars = await backfill(coin, BAR_HISTORY_S)
            pipe = self.pipelines.get(coin)
            if pipe is not None:
                log.info("backfill %s: %d candles merged", coin, pipe.state.merge_bars(bars))
        except Exception as exc:
            log.warning("backfill for %s failed: %s", coin, exc)

    async def _backfill(self) -> None:
        for coin, pipe in list(self.pipelines.items()):
            try:
                bars = await backfill(coin, BAR_HISTORY_S)
                added = pipe.state.merge_bars(bars)
                log.info("backfill %s: %d candles merged", coin, added)
            except Exception as exc:  # network down, API change: long windows just warm up live
                log.warning("backfill for %s failed: %s", coin, exc)

    # ── browser push ────────────────────────────────────────────────────────
    def snapshot(self, coin: str | None = None, include_bars: bool = True) -> dict:
        from api.serializers import build_snapshot
        return build_snapshot(self, coin or self.default_coin, include_bars=include_bars)

    async def _heartbeat(self, every_s: float = HEARTBEAT_S) -> None:
        """One status line every few minutes, so a server's log shows the feed (and recording) is alive."""
        while True:
            await asyncio.sleep(every_s)
            st = self.status
            line = (f"feed {'connected' if st.connected else 'reconnecting'} · {st.messages:,} messages · "
                    f"{st.reconnects} reconnects · {len(self.clients)} browsers")
            if self.external:
                line += " · " + ", ".join(f"{v} {'up' if s['connected'] else 'down'} ({s['quotes']:,} quotes)"
                                          for v, s in self.external.status().items())
            if self.outcomes.enabled:
                o = self.outcomes
                line += (f" · prediction markets: {len(o.markets)} listed, {len(o._followed)} followed, feed "
                         f"{'up' if o.status.connected else 'down'}")
            if self.liqmap.enabled and self.liqmap.rounds:
                lm = self.liqmap.last
                line += f" · liquidation map: {lm['positions']} positions from {lm['wallets']} wallets last round"
            if isinstance(self._recorder, HourlyRecorder):
                line += (f" · recording this hour {self._recorder.hour_bytes / 1e6:.0f} MB"
                         + (" · PAUSED: disk nearly full" if self._recorder.paused else ""))
            log.info(line)
            log.info("speed: %s", self.perf.line())

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(self.broadcast_interval_s)
            if not self.clients:
                continue
            self._broadcasts += 1
            t = time.perf_counter()
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
            self.perf.push.add((time.perf_counter() - t) * 1000)

    @property
    def speed_label(self) -> str:
        return "max" if math.isinf(self.speed) else f"{self.speed:g}×"


def recording_prefix(coins: list[str]) -> str:
    return "-".join(coins).lower()


def _coins_in(path: Path) -> list[str]:
    return coins_in(path)

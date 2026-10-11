"""
Position watch service: polls the watched wallets, scores every open position, and alerts when risk rises.

Wallets live in data/watch.json on the machine running Analytix. Every POLL_S seconds (live mode only) it
reads each wallet's positions and every perp's mark price and funding; open orders (for stops and
take-profits) every ORDERS_EVERY_S. Each position is scored with the planner's models: 5-minute candles for
the next hour, hourly candles for the next 24 hours, volatility brought up to the moment.

Alerts (the "Position risk" rule in the Alerts panel, threshold in %): one when a position's chance of
liquidation in the next 24 hours rises past the threshold, and a more urgent one when the chance within
the next hour does. Each re-arms once the chance falls back below half the threshold, so a position
hovering at the line doesn't ping every poll.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from config import WATCH_FILE
from engine.alerts import Alert
from engine.planner import candles_from_bars
from engine.positions import Account, Position, PositionRisk, assess, log_distance, parse_account
from ingestion.account import fetch_account, fetch_market, fetch_open_orders, valid_address
from signals.base import fmt_px, fmt_usd

log = logging.getLogger("analytix.positions")

POLL_S = 20
ORDERS_EVERY_S = 60
MAX_WALLETS = 10
REARM = 0.5                       # an alert re-arms once the chance falls below half the threshold


class WatchError(ValueError):
    pass


class PositionWatch:
    def __init__(self, runtime, path: Path = WATCH_FILE):
        self.rt = runtime
        self.path = Path(path)
        self.wallets: list[dict] = self._load()
        self.results: dict[str, dict] = {}          # address → latest scored account (or error)
        self._orders: dict[str, tuple[float, list]] = {}
        self._armed: dict[tuple, bool] = {}
        self._last_p: dict[tuple, float] = {}
        self._task: asyncio.Task | None = None
        self._refresh = asyncio.Event()
        # the three Hyperliquid reads, swappable for tests and demos
        self.fetch_account, self.fetch_orders, self.fetch_market = fetch_account, fetch_open_orders, fetch_market

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live"

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="positions")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            if self.enabled and self.wallets:
                try:
                    await self.poll()
                except Exception:                            # one bad poll must not stop the watch
                    log.exception("position poll failed")
            try:
                await asyncio.wait_for(self._refresh.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass
            self._refresh.clear()

    # ── wallets ────────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("couldn't read %s (%s); starting with no wallets", self.path, exc)
            return []
        return [w for w in saved.get("wallets", []) if valid_address(w.get("address", ""))][:MAX_WALLETS]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"wallets": self.wallets}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, address: str, label: str = "") -> dict:
        address = address.strip()
        if not valid_address(address):
            raise WatchError("That isn't a wallet address. It should start with 0x followed by 40 letters and numbers.")
        if any(w["address"].lower() == address.lower() for w in self.wallets):
            raise WatchError("Already watching that wallet.")
        if len(self.wallets) >= MAX_WALLETS:
            raise WatchError(f"Watching {MAX_WALLETS} wallets already. Remove one first.")
        self.wallets.append({"address": address, "label": label.strip()[:40]})
        self._save()
        self._refresh.set()
        return self.public()

    def remove(self, address: str) -> dict:
        before = len(self.wallets)
        self.wallets = [w for w in self.wallets if w["address"].lower() != address.lower()]
        if len(self.wallets) == before:
            raise WatchError("Not watching that wallet.")
        self.results.pop(address.lower(), None)
        self._save()
        return self.public()

    def public(self) -> dict:
        return {
            "enabled": self.enabled, "poll_s": POLL_S, "max_wallets": MAX_WALLETS,
            "wallets": [{**w, **(self.results.get(w["address"].lower()) or {"account": None, "error": None,
                                                                              "updated_ms": None})}
                        for w in self.wallets],
        }

    # ── polling ────────────────────────────────────────────────────────────
    async def poll(self) -> None:
        try:
            market = await self.fetch_market()
        except Exception as exc:                              # positions still carry their own value ÷ size
            log.warning("market prices unavailable: %s", exc)
            market = {}
        marks = {c: m["mark"] for c, m in market.items() if m.get("mark")}
        for w in list(self.wallets):
            addr = w["address"]
            try:
                state = await self.fetch_account(addr)
                orders = await self._open_orders(addr)
                acct = parse_account(addr, state, orders, marks)
                scored = [await self._score(p, market.get(p.coin, {})) for p in acct.positions]
                self.results[addr.lower()] = {"account": _account_dict(acct, scored), "error": None,
                                              "updated_ms": int(time.time() * 1000)}
                self._alerts(w, acct, scored)
            except Exception as exc:
                log.warning("position watch for %s failed: %s", addr, exc)
                prev = self.results.get(addr.lower()) or {"account": None}
                self.results[addr.lower()] = {**prev, "error": "Couldn't read this wallet from Hyperliquid just now. "
                                                               "Trying again shortly."}

    async def _open_orders(self, addr: str) -> list:
        at, orders = self._orders.get(addr.lower(), (0.0, []))
        if time.time() - at >= ORDERS_EVERY_S:
            try:
                orders = await self.fetch_orders(addr)
                self._orders[addr.lower()] = (time.time(), orders)
            except Exception as exc:                          # keep the last known orders
                log.warning("open orders for %s unavailable: %s", addr, exc)
        return orders

    async def _score(self, pos: Position, mkt: dict) -> tuple[Position, PositionRisk | None, str | None]:
        """The position's odds, or why they couldn't be worked out."""
        planner = self.rt.planner
        try:
            m_short = await planner.model(pos.coin, 300)
            m_day = await planner.model(pos.coin, 3600)
        except Exception as exc:
            log.warning("no risk model for %s: %s", pos.coin, exc)
            return pos, None, f"Couldn't load {pos.coin}'s price history just now, so no odds yet."
        now = int(time.time() * 1000)
        liq_d = log_distance(pos.mark, pos.liq_price, pos.side, against=True)
        stop_d = log_distance(pos.mark, pos.stop, pos.side, against=True)
        target_d = log_distance(pos.mark, pos.target, pos.side, against=False)
        bracket = (stop_d or None, target_d) if (stop_d or target_d) else None
        sim_d = None if liq_d is None else max(liq_d, 1e-6)
        paths_1h = await planner.paths(pos.coin, m_short, 1.0, self._v0(pos, m_short, now), now, pos.side, sim_d)
        paths_24h = await planner.paths(pos.coin, m_day, 24.0, self._v0(pos, m_day, now), now, pos.side,
                                        sim_d, bracket)
        funding = mkt.get("funding")
        pipe = self.rt.pipelines.get(pos.coin)
        if funding is None and pipe is not None and pipe.state.context is not None:
            funding = pipe.state.context.funding
        kind = "fhs" if m_short.kind == "fhs" and m_day.kind == "fhs" else "rough"
        return pos, assess(pos, paths_1h, paths_24h, funding, kind), None

    def _v0(self, pos: Position, model, now: int) -> float:
        pipe = self.rt.pipelines.get(pos.coin)
        recent = None
        if pipe is not None:
            recent = candles_from_bars([b for b in pipe.state.bars if b.timestamp >= model.last_t], model.step_s)
        return model.nowcast(recent, pos.mark, now)

    # ── alerts ─────────────────────────────────────────────────────────────
    def _alerts(self, wallet: dict, acct: Account, scored: list) -> None:
        svc = getattr(self.rt, "alerts", None)
        if svc is None:
            return
        rule = svc.cfg["rules"].get("position") or {}
        if not rule.get("on"):
            return
        threshold = float(rule.get("min_pct", 5.0)) / 100
        open_keys = set()
        for pos, risk, _ in scored:
            if risk is None:
                continue
            for horizon, p in (("24h", risk.p_liq_24h), ("1h", risk.p_liq_1h)):
                key = (acct.address.lower(), pos.coin, pos.side, horizon)
                open_keys.add(key)
                prev = self._last_p.get(key)
                self._last_p[key] = p
                if p >= threshold and self._armed.get(key, True):
                    self._armed[key] = False
                    svc.push(_risk_alert(wallet, acct, pos, risk, horizon, p, prev))
                elif p < threshold * REARM:
                    self._armed[key] = True
        for key in [k for k in self._last_p if k[0] == acct.address.lower() and k not in open_keys]:
            self._last_p.pop(key, None)                       # closed: a new position starts fresh
            self._armed.pop(key, None)


def _side(pos: Position) -> str:
    return "long" if pos.side > 0 else "short"


def _chance(p: float) -> str:
    return "<0.1%" if p < 0.001 else f"{p * 100:.1f}%" if p < 0.1 else f"{p * 100:.0f}%"


def _risk_alert(wallet: dict, acct: Account, pos: Position, risk: PositionRisk, horizon: str, p: float,
                prev: float | None) -> Alert:
    name = wallet.get("label") or f"{acct.address[:6]}…{acct.address[-4:]}"
    if horizon == "1h":
        title = f"{pos.coin} {_side(pos)} is close to liquidation: {_chance(p)} chance in the next hour"
    else:
        title = f"{pos.coin} {_side(pos)}: {_chance(p)} chance of liquidation in the next 24h"
    where = "below" if pos.side > 0 else "above"
    lines = []
    if prev is not None and prev < p:
        lines.append(f"Was {_chance(prev)} at the last check.")
    if pos.liq_price is not None and risk.liq_pct is not None:
        moves = f" That's {risk.liq_moves:.1f}× a normal day's move." if risk.liq_moves is not None else ""
        lines.append(f"Liquidation at {fmt_px(pos.liq_price)}, {abs(risk.liq_pct):.1f}% {where} the price now of "
                     f"{fmt_px(pos.mark)}.{moves}")
    pnl = f"{'+' if pos.pnl >= 0 else '−'}{fmt_usd(abs(pos.pnl))}"
    lines.append(f"{name}: {fmt_usd(pos.value)} at {pos.leverage:g}× {'cross' if pos.cross else 'isolated'}, "
                 f"P&L {pnl}.")
    now = int(time.time() * 1000)
    return Alert(f"pos-{acct.address.lower()}-{pos.coin}-{horizon}-{now}", pos.coin, "position", now,
                 "down" if pos.side > 0 else "up", title, tuple(lines))


def _account_dict(acct: Account, scored: list) -> dict:
    return {
        "value": acct.value, "margin_used": acct.margin_used, "withdrawable": acct.withdrawable,
        "positions": [{**asdict(pos), "risk": asdict(risk) if risk else None, "risk_error": err}
                      for pos, risk, err in scored],
    }


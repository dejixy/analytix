"""
Position watch: a wallet's open positions, read from Hyperliquid, with the planner's odds applied live.

For each position the liquidation price comes from Hyperliquid itself, so it already accounts for cross
margin, the account balance and every other open position. From the price now, the planner's simulation
(the coin's own candles, volatility brought up to the moment) gives:

    the chance price reaches liquidation within the next hour and the next 24 hours
    how far liquidation is, in normal 24-hour moves
    with a stop-loss or take-profit on the book: the chance each one closes the position first within 24h
    funding: what holding it costs (or earns) per day at today's rate

The liquidation price is held fixed over the 24 hours. In cross margin it really moves as the other
positions win or lose and as funding is paid, which the dashboard says next to the odds.
"""
import math
from dataclasses import dataclass

import numpy as np

from engine.planner import Paths


@dataclass(frozen=True, slots=True)
class Position:
    coin: str
    side: int                    # 1 long, −1 short
    size: float                  # coins
    entry: float
    mark: float
    value: float                 # USD at the mark price
    pnl: float                   # unrealised, USD
    roe: float                   # P&L ÷ margin
    liq_price: float | None      # Hyperliquid's own; None when it can't be liquidated
    margin: float
    leverage: float
    cross: bool
    stop: float | None = None    # trigger price of the nearest stop-loss on the book
    target: float | None = None  # trigger price of the nearest take-profit on the book


@dataclass(frozen=True, slots=True)
class Account:
    address: str
    value: float                 # account value (equity), USD
    margin_used: float
    withdrawable: float
    positions: tuple[Position, ...]
    time_ms: int


@dataclass(frozen=True, slots=True)
class PositionRisk:
    liq_pct: float | None        # % move from the price now to liquidation (signed)
    liq_moves: float | None      # liquidation distance in normal 24h moves
    day_move_pct: float          # a normal 24h move, %
    p_liq_1h: float
    p_liq_24h: float
    p_stop_24h: float | None     # chance the stop closes it first within 24h (None: no stop on the book)
    p_target_24h: float | None
    stop_beyond_liq: bool        # the stop sits past liquidation: liquidation would come first
    funding_day: float | None    # USD a day at today's rate; positive = you pay
    model: str                   # "fhs" | "rough"


def _f(x, default: float | None = None) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_account(address: str, state: dict, orders: list[dict] | None = None,
                  marks: dict[str, float] | None = None) -> Account:
    """Hyperliquid's clearinghouseState (+ frontendOpenOrders) → Account. `marks`: coin → mark price, used
    when given (the position's own value ÷ size otherwise)."""
    positions = []
    for ap in state.get("assetPositions") or []:
        p = ap.get("position") or {}
        szi = _f(p.get("szi"), 0.0)
        if not szi:
            continue
        coin = p.get("coin", "?")
        size = abs(szi)
        value = abs(_f(p.get("positionValue"), 0.0))
        mark = (marks or {}).get(coin) or (value / size if size else 0.0)
        lev = p.get("leverage") or {}
        side = 1 if szi > 0 else -1
        liq = _f(p.get("liquidationPx"))
        stop, target = protective_orders(orders or [], coin, side, mark)
        positions.append(Position(
            coin=coin, side=side, size=size, entry=_f(p.get("entryPx"), mark), mark=mark,
            value=size * mark if mark else value, pnl=_f(p.get("unrealizedPnl"), 0.0),
            roe=_f(p.get("returnOnEquity"), 0.0), liq_price=liq if liq and liq > 0 else None,
            margin=_f(p.get("marginUsed"), 0.0), leverage=_f(lev.get("value"), 1.0),
            cross=lev.get("type", "cross") == "cross", stop=stop, target=target))
    ms = state.get("marginSummary") or {}
    return Account(address=address, value=_f(ms.get("accountValue"), 0.0),
                   margin_used=_f(ms.get("totalMarginUsed"), 0.0), withdrawable=_f(state.get("withdrawable"), 0.0),
                   positions=tuple(sorted(positions, key=lambda x: -x.value)), time_ms=int(state.get("time") or 0))


def protective_orders(orders: list[dict], coin: str, side: int, mark: float) -> tuple[float | None, float | None]:
    """The nearest stop-loss and take-profit for a position: trigger orders on the closing side, a stop on
    the losing side of the price now, a take-profit on the winning side."""
    close_side = "A" if side > 0 else "B"
    stops, targets = [], []
    for o in orders:
        if o.get("coin") != coin or o.get("side") != close_side or not o.get("isTrigger"):
            continue
        px = _f(o.get("triggerPx"))
        if not px or not mark:
            continue
        kind = str(o.get("orderType", "")).lower()
        losing = (px < mark) if side > 0 else (px > mark)
        if "stop" in kind and losing:
            stops.append(px)
        elif "take profit" in kind and not losing:
            targets.append(px)
    # the one that would fire first: the closest to the price on each side
    stop = (max(stops) if side > 0 else min(stops)) if stops else None
    target = (min(targets) if side > 0 else max(targets)) if targets else None
    return stop, target


def log_distance(mark: float, level: float | None, side: int, against: bool) -> float | None:
    """How far `level` is from the price now as a positive log move, or None if it's on the wrong side."""
    if level is None or not mark or level <= 0:
        return None
    d = math.log(mark / level) if (side > 0) == against else math.log(level / mark)
    return d if d > 0 else (0.0 if against else None)


def assess(pos: Position, paths_1h: Paths, paths_24h: Paths, funding_hourly: float | None,
           model_kind: str = "fhs") -> PositionRisk:
    """The odds for one position. `paths_24h` must be simulated with liq=(side, distance) and, if the position
    has a stop or take-profit, bracket=(stop, target) distances (engine.planner.simulate)."""
    side = pos.side
    liq_d = log_distance(pos.mark, pos.liq_price, side, against=True)

    def p_liq(paths: Paths) -> float:
        if liq_d is None:
            return 0.0
        if liq_d <= 0:
            return 1.0
        adverse = -paths.low if side > 0 else paths.high
        return float(np.mean(adverse >= liq_d))

    sigma = float(np.std(paths_24h.final))
    stop_d = log_distance(pos.mark, pos.stop, side, against=True)
    p_stop = p_target = None
    beyond = bool(stop_d is not None and liq_d is not None and stop_d >= liq_d)
    if paths_24h.exit_kind is not None:
        if pos.stop is not None:
            p_stop = 0.0 if beyond else float(np.mean(paths_24h.exit_kind == 2))
        if pos.target is not None:
            p_target = float(np.mean(paths_24h.exit_kind == 1))
    return PositionRisk(
        liq_pct=None if pos.liq_price is None or not pos.mark else (pos.liq_price / pos.mark - 1) * 100,
        liq_moves=None if liq_d is None or sigma <= 0 else liq_d / sigma,
        day_move_pct=float(np.expm1(sigma) * 100),
        p_liq_1h=p_liq(paths_1h), p_liq_24h=p_liq(paths_24h),
        p_stop_24h=p_stop, p_target_24h=p_target, stop_beyond_liq=beyond,
        funding_day=None if funding_hourly is None else side * funding_hourly * 24 * pos.value,
        model=model_kind)

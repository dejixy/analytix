"""
Dataclasses → JSON-ready dicts. Enums become their values, floats are rounded,
NaN/inf become null (JSON has no NaN).
"""
import math
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from typing import Any

from config import BOOK_LEVELS_SHOWN, CHART_POINTS, MAX_TICK_WINDOW_S, WINDOWS
from models.explanationModel import Explanation, MoveEvent
from engine.positioning import percentile
from signals.liquidations import is_sweep


def to_jsonable(obj: Any, digits: int = 6) -> Any:
    if obj is None or isinstance(obj, (bool, int, str)) and not isinstance(obj, Enum):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else round(obj, digits)
    if is_dataclass(obj):
        return {f.name: to_jsonable(getattr(obj, f.name), digits) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v, digits) for v in obj]
    return str(obj)


def explanation_dict(ex: Explanation) -> dict:
    d = to_jsonable(ex)
    d["move"]["move_pct"] = round(ex.move.move_pct, 4)
    return d


def plan_dict(plan) -> dict:
    """The planner's result. The fitted model is summarised, not shipped (it holds the residual pool)."""
    m = plan.model
    out = {f.name: to_jsonable(getattr(plan, f.name)) for f in fields(plan) if f.name != "model"}
    out["costs"]["total"] = round(plan.costs.total, 6)
    out["model"] = {
        "kind": m.kind, "step_s": m.step_s, "candles": m.n, "days": round(m.span_days, 1),
        "alpha": round(m.alpha, 3), "beta": round(m.beta, 3),
        "half_life_h": round(math.log(0.5) / math.log(m.alpha + m.beta) * m.step_s / 3600, 1)
        if 0 < m.alpha + m.beta < 1 else None,
    }
    return out


def event_dict(ev: MoveEvent) -> dict:
    return {
        "id": ev.id,
        "window": ev.window,
        "started_ms": ev.started_ms,
        "peak_ms": ev.peak_ms,
        "ended_ms": ev.ended_ms,
        "active": ev.active,
        "explanation": explanation_dict(ev.explanation),
    }


def build_snapshot(runtime, coin: str, trades_limit: int = 40, events_limit: int = 30,
                   include_bars: bool = True) -> dict:
    pipe = runtime.pipeline(coin)
    st, an = pipe.state, pipe.analyzer
    book, c = st.book, st.context

    price = None
    if book and book.mid:
        price = {"mid": book.mid, "best_bid": book.best_bid, "best_ask": book.best_ask,
                 "spread_bps": book.spread_bps}

    context = None
    if c:
        change_24h = (c.mark_price / c.prev_day_price - 1) * 100 if c.prev_day_price else None
        context = {"funding_hourly": c.funding, "funding_apr": c.funding_apr, "open_interest": c.open_interest,
                   "open_interest_usd": c.open_interest_usd, "mark_price": c.mark_price,
                   "oracle_price": c.oracle_price, "premium_bps": c.premium_bps, "day_volume": c.day_volume,
                   "change_24h_pct": change_24h,
                   "funding_pct": percentile(an.positioning.funding_history(), c.funding)}

    book_d = None
    if book:
        summary = st.books.newest
        book_d = {
            "bids": [[l.price, l.size, l.orders] for l in book.bids[:BOOK_LEVELS_SHOWN]],
            "asks": [[l.price, l.size, l.orders] for l in book.asks[:BOOK_LEVELS_SHOWN]],
            "bid_notional": summary.bid_notional if summary else None,
            "ask_notional": summary.ask_notional if summary else None,
            "imbalance": summary.imbalance if summary else None,
        }

    threshold = an.baseline.sweep_threshold if an.baseline else float("inf")
    sweep_keys = {(o.side, o.timestamp) for o in st.orders.window(60, st.now_ms) if is_sweep(o, threshold, st.engine.twaps)} \
        if st.now_ms else set()
    trades = []
    for t in reversed(st.trades):
        trades.append({"ts": t.timestamp, "px": t.price, "sz": t.size, "side": t.side.value,
                       "notional": t.notional, "sweep": (t.side, t.timestamp) in sweep_keys})
        if len(trades) >= trades_limit:
            break

    return to_jsonable({
        "coin": st.coin,
        "coins": runtime.coins,
        "tickers": {c: _ticker(p) for c, p in runtime.pipelines.items()},
        "now_ms": st.now_ms,
        "feed": runtime.status.to_dict(),
        "windows": WINDOWS,
        "price": price,
        "context": context,
        "baseline": an.baseline,
        "explanations": {w: explanation_dict(ex) for w, ex in an.latest.items()},
        "book": book_d,
        "trades": trades,
        "series": [[t, m] for t, m in st.mid_series(MAX_TICK_WINDOW_S, step_ms=_chart_step_ms())],
        "chart_span_s": MAX_TICK_WINDOW_S,
        **({"bar_series": bar_series(st)} if include_bars else {}),
        "events": [event_dict(e) for e in an.events.recent(events_limit)],
        "market_events": sorted(an.market_events(), key=lambda e: -e.ts)[:events_limit],
        "levels": [{"side": t.side, "price": t.price, "absorbed": t.absorbed, "window": t.window,
                    "created_ms": t.created_ms} for t in an.levels.active()],
        "walls": {
            "active": [{"side": w.side, "price": w.price, "notional": w.notional, "age_s": (st.now_ms - w.first_ms) / 1000,
                        "traded": w.traded} for w in an.walls.active(st.now_ms)],
            "stats": an.walls.stats(st.now_ms),
        },
        "engine": {"runs": an.runs, "dropped": st.dropped, "trades_buffered": len(st.trades)},
        "alerts": [asdict(a) for a in list(runtime.alerts.recent)[-10:]] if hasattr(runtime, "alerts") else [],
    })


def _ticker(pipe) -> dict:
    """Mid and 1m move for the coin switcher."""
    ex = pipe.analyzer.latest.get("1m")
    return {"mid": pipe.state.mid, "move_1m_pct": ex.move.move_pct if ex else None,
            "significance": ex.move.significance.value if ex else None}


def _chart_step_ms() -> int:
    """Whole seconds per chart point: 1s for a 15m chart, 4s for a 60m one."""
    return max(1, math.ceil(MAX_TICK_WINDOW_S / CHART_POINTS)) * 1000


def bar_series(st, recent_s: int = 86_400, recent_step_s: int = 120, old_step_s: int = 900) -> list[list[float]]:
    """
    Close prices for the long-window chart: 2-minute points for the last 24h,
    15-minute points before that — about 1,300 points for a full week.
    """
    if not len(st.bars):
        return []
    cutoff = (st.now_ms or st.bars.newest.end_ms) - recent_s * 1000
    points: dict[int, float] = {}
    for b in st.bars:
        step = (recent_step_s if b.end_ms >= cutoff else old_step_s) * 1000
        points[b.end_ms // step * step] = b.close
    out = [[t, p] for t, p in sorted(points.items())]
    if st.mid is not None and st.now_ms:
        out.append([st.now_ms, st.mid])
    return out

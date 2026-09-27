"""
Dataclasses → JSON-ready dicts. Enums become their values, floats are rounded,
NaN/inf become null (JSON has no NaN).
"""
import math
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from config import BOOK_LEVELS_SHOWN, MAX_WINDOW_S, WINDOWS
from models.explanationModel import Explanation, MoveEvent
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


def build_snapshot(runtime, coin: str, trades_limit: int = 40, events_limit: int = 30) -> dict:
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
                   "change_24h_pct": change_24h}

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
    sweep_keys = {(o.side, o.timestamp) for o in st.orders.window(60, st.now_ms) if is_sweep(o, threshold)} \
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
        "series": [[t, m] for t, m in st.mid_series(MAX_WINDOW_S)],
        "events": [event_dict(e) for e in an.events.recent(events_limit)],
        "engine": {"runs": an.runs, "dropped": st.dropped, "trades_buffered": len(st.trades)},
    })


def _ticker(pipe) -> dict:
    """Mid and 1m move for the coin switcher."""
    ex = pipe.analyzer.latest.get("1m")
    return {"mid": pipe.state.mid, "move_1m_pct": ex.move.move_pct if ex else None,
            "significance": ex.move.significance.value if ex else None}

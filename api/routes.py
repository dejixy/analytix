from fastapi import APIRouter, HTTPException, Query, Request

from api.planner import PlanError
from api.serializers import event_dict, explanation_dict, plan_dict
from config import WINDOWS

router = APIRouter(prefix="/api")


def _rt(request: Request):
    return request.app.state.runtime


def _pipe(request: Request, coin: str | None):
    rt = _rt(request)
    if coin is not None and coin not in rt.pipelines:
        raise HTTPException(404, f"Not watching '{coin}'. Watching {rt.coins} — set ANALYTIX_COINS to change.")
    return rt.pipeline(coin)


@router.get("/health")
def health(request: Request):
    rt = _rt(request)
    return {
        "status": "ok",
        "coins": {c: {"now_ms": p.state.now_ms, "engine_runs": p.analyzer.runs, "trades_buffered": len(p.state.trades)}
                  for c, p in rt.pipelines.items()},
        "feed": rt.status.to_dict(),
    }


@router.get("/config")
def config(request: Request):
    rt = _rt(request)
    return {"coins": rt.coins, "default_coin": rt.default_coin, "mode": rt.mode, "windows": WINDOWS,
            "replay_speed": rt.speed_label}


@router.get("/snapshot")
def snapshot(request: Request, coin: str | None = None):
    _pipe(request, coin)
    return _rt(request).snapshot(coin)


@router.get("/explain/{window}")
def explain(window: str, request: Request, coin: str | None = None):
    if window not in WINDOWS:
        raise HTTPException(404, f"Unknown window '{window}'. Choose from {list(WINDOWS)}")
    ex = _pipe(request, coin).analyzer.latest.get(window)
    if ex is None:
        raise HTTPException(503, "Not enough data yet — the engine is warming up.")
    return explanation_dict(ex)


@router.get("/explain_at")
async def explain_at(request: Request, t: int, window: str = "1m", coin: str | None = None):
    """Explain the move in the window ending at moment t (exchange ms). Async on purpose: it reads the
    same buffers the feed writes to, so it runs on the event loop rather than in a worker thread."""
    if window not in WINDOWS:
        raise HTTPException(404, f"Unknown window '{window}'. Choose from {list(WINDOWS)}")
    found = _pipe(request, coin).analyzer.explain_at(window, t)
    if found is None:
        raise HTTPException(404, "That moment is outside the data in memory.")
    used, ex = found
    note = "" if used == window else f"{window} isn't in memory that far back, so this is the {used} up to that moment."
    return {"window": used, "requested": window, "at_ms": ex.end_ms, "note": note, "explanation": explanation_dict(ex)}


@router.get("/plan")
async def plan(request: Request, side: str = "long", margin: float = Query(1_000, gt=0, le=1e8),
               leverage: float = Query(5, ge=1, le=100), hours: float = Query(24, ge=0.25, le=168),
               stop: float | None = Query(None, gt=0, lt=100), target: float | None = Query(None, gt=0, le=1000),
               coin: str | None = None):
    """What to expect from a position before taking it: liquidation odds, path, exit spread, costs."""
    if side not in ("long", "short"):
        raise HTTPException(400, "side must be 'long' or 'short'")
    pipe = _pipe(request, coin)
    try:
        p = await _rt(request).planner.plan(pipe.coin, 1 if side == "long" else -1, margin, leverage, hours,
                                            stop, target)
    except PlanError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return plan_dict(p)


@router.get("/events")
def events(request: Request, coin: str | None = None, window: str | None = None,
           limit: int = Query(50, ge=1, le=500)):
    if window is not None and window not in WINDOWS:
        raise HTTPException(404, f"Unknown window '{window}'")
    return [event_dict(e) for e in _pipe(request, coin).analyzer.events.recent(limit, window)]

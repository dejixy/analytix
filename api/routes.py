from fastapi import APIRouter, HTTPException, Query, Request

from api.serializers import event_dict, explanation_dict
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


@router.get("/events")
def events(request: Request, coin: str | None = None, window: str | None = None,
           limit: int = Query(50, ge=1, le=500)):
    if window is not None and window not in WINDOWS:
        raise HTTPException(404, f"Unknown window '{window}'")
    return [event_dict(e) for e in _pipe(request, coin).analyzer.events.recent(limit, window)]

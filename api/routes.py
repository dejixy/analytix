from fastapi import APIRouter, HTTPException, Query, Request

from pydantic import BaseModel

from api.alerts import AlertError
from api.planner import PlanError
from api.positions import WatchError
from api.serializers import event_dict, explanation_dict, plan_dict
from config import WINDOWS

router = APIRouter(prefix="/api")


def _rt(request: Request):
    return request.app.state.runtime


def _pipe(request: Request, coin: str | None):
    rt = _rt(request)
    if coin is not None and coin not in rt.pipelines:
        raise HTTPException(404, f"Not watching '{coin}'. Watching {rt.coins}. Set ANALYTIX_COINS to change that.")
    return rt.pipeline(coin)


@router.get("/health")
def health(request: Request):
    rt = _rt(request)
    return {
        "status": "ok",
        "coins": {c: {"now_ms": p.state.now_ms, "engine_runs": p.analyzer.runs, "trades_buffered": len(p.state.trades)}
                  for c, p in rt.pipelines.items()},
        "other_exchanges": rt.external.status() if rt.external else None,
        "speed_since_last_report": rt.perf.report(reset=False),
        "prediction_markets": {"listed": len(rt.outcomes.markets), **rt.outcomes.feed_status(),
                               "sources": rt.outcomes.intel.public_status()}
        if rt.outcomes.enabled else None,
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
        raise HTTPException(503, "Not enough data yet. The engine is warming up.")
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
               account: float | None = Query(None, gt=0, le=1e9), coin: str | None = None):
    """What to expect from a position before taking it: liquidation odds, path, exit spread, costs.
    `account` switches to cross margin with that account balance (≥ margin)."""
    if side not in ("long", "short"):
        raise HTTPException(400, "side must be 'long' or 'short'")
    pipe = _pipe(request, coin)
    try:
        p, pending = await _rt(request).planner.plan(pipe.coin, 1 if side == "long" else -1, margin, leverage,
                                                     hours, stop, target, account)
    except PlanError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {**plan_dict(p), "calibration_pending": pending}


@router.get("/events")
def events(request: Request, coin: str | None = None, window: str | None = None,
           limit: int = Query(50, ge=1, le=500)):
    if window is not None and window not in WINDOWS:
        raise HTTPException(404, f"Unknown window '{window}'")
    return [event_dict(e) for e in _pipe(request, coin).analyzer.events.recent(limit, window)]


# ── alerts ──────────────────────────────────────────────────────────────────
class LevelIn(BaseModel):
    coin: str
    price: float


class TokenIn(BaseModel):
    token: str


class ChatIn(BaseModel):
    on: bool


def _alerts(request: Request):
    return _rt(request).alerts


async def _guard(fn, *args):
    try:
        out = fn(*args)
        return await out if hasattr(out, "__await__") else out
    except AlertError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/alerts")
def alerts(request: Request):
    """Alert settings (never the bot token), approved chats, and the latest alerts."""
    return _alerts(request).public()


@router.put("/alerts")
async def alerts_update(request: Request, patch: dict):
    """Change rules ({"rules": {"twap": {"on": true, "min_usd_per_hour": 2e6}}}), "cooldown_min" or "coins"."""
    return await _guard(_alerts(request).update, patch)


@router.post("/alerts/levels")
async def alerts_add_level(request: Request, body: LevelIn):
    return await _guard(_alerts(request).add_level, body.coin, body.price)


@router.delete("/alerts/levels/{level_id}")
async def alerts_remove_level(request: Request, level_id: str):
    return await _guard(_alerts(request).remove_level, level_id)


@router.post("/alerts/telegram/token")
async def alerts_token(request: Request, body: TokenIn):
    """Connect a bot (checked with Telegram's getMe). Stored on this machine only; never sent back."""
    return await _guard(_alerts(request).set_token, body.token)


@router.delete("/alerts/telegram")
async def alerts_disconnect(request: Request):
    return await _guard(_alerts(request).disconnect)


@router.post("/alerts/telegram/discover")
async def alerts_discover(request: Request):
    """List chats that have messaged the bot recently; new ones start switched off."""
    return await _guard(_alerts(request).discover)


@router.put("/alerts/telegram/chats/{chat_id}")
async def alerts_chat(request: Request, chat_id: int, body: ChatIn):
    return await _guard(_alerts(request).set_chat, chat_id, body.on)


@router.post("/alerts/telegram/test")
async def alerts_test(request: Request):
    return await _guard(_alerts(request).test)


# ── position watch ──────────────────────────────────────────────────────────
class WalletIn(BaseModel):
    address: str
    label: str = ""


def _watch(request: Request):
    return _rt(request).positions


@router.get("/positions")
def positions(request: Request):
    """Watched wallets with their open positions and each position's odds (refreshed every 20s, live mode)."""
    return _watch(request).public()


@router.post("/positions/wallets")
def positions_add(request: Request, body: WalletIn):
    try:
        return _watch(request).add(body.address, body.label)
    except WatchError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/positions/wallets/{address}")
def positions_remove(request: Request, address: str):
    try:
        return _watch(request).remove(address)
    except WatchError as exc:
        raise HTTPException(404, str(exc)) from exc


# ── scanner ─────────────────────────────────────────────────────────────────
@router.get("/scanner")
def scanner(request: Request):
    """Every Hyperliquid perp, most unusual first, with the reasons (live mode)."""
    return _rt(request).scanner.public()


# ── prediction markets (HIP-4 outcomes) ─────────────────────────────────────
@router.get("/outcomes")
async def outcomes(request: Request):
    """Every prediction market on Hyperliquid (Outcome, trade.xyz and any other venue), with live odds. Async on
    purpose: it reads what the feed writes, so it runs on the event loop rather than in a worker thread."""
    return _rt(request).outcomes.public()


@router.get("/outcomes/questions/{question_id}")
async def outcome_question(request: Request, question_id: int, range: str = "12h"):
    """One event (a question: several outcomes, exactly one ends Yes): every outcome's odds, the add-up check,
    and the five likeliest outcomes' odds over time on one chart."""
    svc = _rt(request).outcomes
    if not svc.enabled:
        raise HTTPException(404, "Prediction markets are read live, so they only show in live mode.")
    d = await svc.question_detail(question_id, range)
    if d is None:
        raise HTTPException(404, "That event isn't listed any more. It may have settled.")
    return d


@router.get("/outcomes/{outcome_id}")
async def outcome(request: Request, outcome_id: int, range: str = "12h"):
    """One market's page: odds history, book, trades, who is trading, fair odds for price markets."""
    svc = _rt(request).outcomes
    if not svc.enabled:
        raise HTTPException(404, "Prediction markets are read live, so they only show in live mode.")
    d = await svc.detail(outcome_id, range)
    if d is None:
        raise HTTPException(404, "That market isn't listed any more. It may have settled.")
    return d


# ── hold clock (very high leverage) ─────────────────────────────────────────
@router.get("/hold")
async def hold(request: Request, coin: str | None = None, side: str = "long",
               leverage: float = Query(800, ge=2, le=2000), target_pct: float | None = Query(None, gt=0, le=10_000),
               entry: float | None = Query(None, gt=0), bust: float | None = Query(None, gt=0),
               buffer_bps: float = Query(0.0, ge=0, le=100)):
    """How long a very high-leverage position survives, from the last 24 hours of the real best bid and ask:
    survival odds by hold time, fill drift, the target-vs-bust race and the tape right now."""
    rt = _rt(request)
    if side not in ("long", "short"):
        raise HTTPException(400, "side must be 'long' or 'short'")
    if not rt.hold.enabled:
        raise HTTPException(404, "The hold clock reads the live book, so it only runs in live mode.")
    coin = coin or rt.default_coin
    if coin not in rt.coins:
        raise HTTPException(404, f"The hold clock covers {rt.coins}.")
    if entry and bust and (bust >= entry) == (side == "long"):
        raise HTTPException(400, "For a long the bust price is below the entry; for a short, above it.")
    return await rt.hold.read(coin, 1 if side == "long" else -1, leverage, target_pct, entry, bust, buffer_bps)


# ── trading bot (shadow mode) ───────────────────────────────────────────────
def _bot_call(fn, *args):
    from api.bot import BotError
    try:
        return fn(*args)
    except BotError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/bot")
def bot(request: Request):
    """The bot's rules, each one's track record with market and limit orders, what's open and the latest trades.
    Shadow mode: real Hyperliquid prices and costs, no orders sent."""
    return _rt(request).bot.public()


@router.post("/bot/rules")
def bot_save_rule(request: Request, rule: dict):
    """Add a rule, or change one (include its id). New trading settings start a fresh track record."""
    return _bot_call(_rt(request).bot.save_rule, rule)


@router.delete("/bot/rules/{rule_id}")
def bot_delete_rule(request: Request, rule_id: str):
    return _bot_call(_rt(request).bot.delete_rule, rule_id)


@router.post("/bot/rules/{rule_id}/reset")
def bot_reset_rule(request: Request, rule_id: str):
    return _bot_call(_rt(request).bot.reset_rule, rule_id)

"""
The trading bot's brain, in shadow mode: turns a timeframe card into trades by rules you set, and fills them the
way Hyperliquid itself would, fees and all. Nothing here places an order; it shows whether a rule survives real
costs before any money is involved.

    Entry       every row you picked on the card (e.g. Flow and Book on 1m) leans the same way, each at least
                `min_strength` strong. The bot is flat, past its cooldown, under its trades-per-hour cap and not
                stopped for the day.
    Two styles  every rule is run twice, side by side, so you can see which survives:
                  market   in and out with market orders: fills at the best ask (buy) or bid (sell), walking the
                           book for size, 0.045% fee per side
                  limit    joins the best bid (long) or ask (short) with a limit order, filled only when a trade
                           prints through that price within `limit_wait_s`, 0.015% fee; the take-profit rests as
                           a limit too. Stops, time exits and flips are market orders either way.
    Exits       take-profit and stop-loss in bps of price, checked on every book update (wicks count: a long's
                stop fills at the best bid the moment it reaches the stop, which can be past it), a time limit,
                an optional exit when the rows all lean the other way, and Hyperliquid's own liquidation
                (isolated margin; maintenance is half the initial margin at the coin's max leverage).
    Costs       fees on each side, the spread (market orders pay it), slippage for size, funding while held.
"""
import math
import time
from dataclasses import asdict, dataclass, field, fields

from models.signalModel import Direction

TAKER_FEE = 0.00045          # Hyperliquid perps, base tier, per side
MAKER_FEE = 0.00015
LATENCY_MS = 500             # a market order lands this long after the signal (our view of the book + the round trip)
STYLES = ("market", "limit", "maker_exit")
EXIT_WAIT_MS = 3000          # maker_exit: a closing limit order waits this long at the touch, then crosses
MIN_TRADES_FOR_VERDICT = 30


@dataclass
class Rule:
    id: str
    name: str = ""
    coin: str = "BTC"
    timeframe: str = "1m"
    signals: tuple = ("flow", "book")
    min_strength: float = 0.3        # each chosen row's bar must reach this (0 to 1)
    sides: str = "both"              # "both" | "long" | "short"
    leverage: float = 20.0
    margin_usd: float = 100.0
    take_profit_bps: float = 15.0
    stop_loss_bps: float = 10.0
    max_hold_s: int = 300
    exit_on_flip: bool = True
    cooldown_s: int = 30
    max_trades_per_hour: int = 20
    daily_loss_usd: float = 50.0     # per style: stop for the rest of the UTC day after losing this much
    limit_wait_s: int = 10
    telegram: str = "hourly"         # "off" | "hourly" | "trades"
    on: bool = True
    taker_fee: float = TAKER_FEE     # your own rates: volume tier and HYPE staking discount
    maker_fee: float = MAKER_FEE

    @property
    def notional(self) -> float:
        return self.margin_usd * self.leverage

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        names = {f.name for f in fields(cls)}
        r = cls(**{k: v for k, v in d.items() if k in names})
        r.signals = tuple(r.signals)
        return r

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = list(self.signals)
        return d


def card_signal(rows, wanted, min_strength: float) -> int:
    """+1 when every wanted row leans up at least `min_strength` strong, -1 when all lean down, 0 otherwise
    (a row missing, partial, neutral, too weak, or the rows disagreeing)."""
    got = {m.key: m for m in rows or ()}
    lean = None
    for k in wanted:
        m = got.get(k)
        if m is None or m.partial or m.lean is Direction.NEUTRAL or m.bar < min_strength:
            return 0
        if lean is None:
            lean = m.lean
        elif m.lean is not lean:
            return 0
    if lean is None:
        return 0
    return 1 if lean is Direction.UP else -1


def walk(levels, usd: float) -> float | None:
    """Average fill price for a market order of `usd` notional, walking book levels (best first)."""
    left, cost, qty = usd, 0.0, 0.0
    for lv in levels or ():
        take = min(left, lv.price * lv.size)
        cost += take
        qty += take / lv.price
        left -= take
        if left <= 1e-9:
            break
    return cost / qty if qty else None


def liq_distance(leverage: float, max_leverage: float | None) -> float:
    """How far price can go against an isolated position before Hyperliquid liquidates it: equity falls to the
    maintenance margin, which is half the initial margin at the coin's max leverage."""
    mm = 1 / (2 * max_leverage) if max_leverage else 0.0
    return max(0.0, 1 / leverage - mm)


@dataclass
class Position:
    side: int
    style: str
    entry_ms: int
    entry_px: float
    notional: float
    fees: float
    tp_px: float
    sl_px: float
    liq_px: float
    funding: float = 0.0
    best: float = 0.0               # most favourable move seen, bps
    worst: float = 0.0              # most adverse move seen, bps
    last_funding_ms: int = 0


@dataclass
class Pending:                       # a limit entry waiting to fill
    side: int
    price: float
    placed_ms: int


@dataclass
class Sim:
    """One rule run in one style."""
    rule: Rule
    style: str
    max_leverage: float | None = None
    pos: Position | None = None
    pending: Pending | None = None
    market_due: tuple | None = None          # (side, due_ms): a market entry waiting out the latency
    last_exit_ms: int = -10**15            # no exit yet: no cooldown
    entries: list = field(default_factory=list)      # entry times, last hour
    day: str = ""
    day_net: float = 0.0
    stopped_for_day: bool = False
    missed: int = 0                          # limit entries that never filled
    closing: tuple | None = None             # maker_exit: (price, placed ms, reason) of a resting closing order

    # ── entries ───────────────────────────────────────────────────────────
    def can_enter(self, now_ms: int, side: int) -> bool:
        r = self.rule
        self._roll_day(now_ms)
        if self.pos or self.pending or self.market_due or self.stopped_for_day or not r.on:
            return False
        if (r.sides == "long" and side < 0) or (r.sides == "short" and side > 0):
            return False
        if now_ms - self.last_exit_ms < r.cooldown_s * 1000:
            return False
        self.entries = [t for t in self.entries if now_ms - t < 3_600_000]
        return len(self.entries) < r.max_trades_per_hour

    def signal(self, now_ms: int, side: int, best_bid: float, best_ask: float) -> None:
        if side == 0 or not self.can_enter(now_ms, side):
            return
        self.entries.append(now_ms)
        if self.style in ("market", "maker_exit"):
            self.market_due = (side, now_ms + LATENCY_MS)
        else:
            self.pending = Pending(side, best_bid if side > 0 else best_ask, now_ms)

    def _open(self, side: int, px: float, now_ms: int, fee_rate: float) -> None:
        r = self.rule
        tp = px * (1 + side * r.take_profit_bps / 1e4)
        sl = px * (1 - side * r.stop_loss_bps / 1e4)
        liq = px * (1 - side * liq_distance(r.leverage, self.max_leverage))
        self.pos = Position(side, self.style, now_ms, px, r.notional, r.notional * fee_rate, tp, sl, liq,
                            last_funding_ms=now_ms)

    # ── the market moving ────────────────────────────────────────────────
    def on_book(self, ts: int, best_bid: float, best_ask: float, levels=None) -> dict | None:
        """One book update. Fills a due market entry; checks the open position's stop, liquidation and (market
        style) take-profit. Returns the closed trade, if any."""
        if self.market_due and ts >= self.market_due[1]:
            side = self.market_due[0]
            self.market_due = None
            px = None
            if levels is not None:
                px = walk(levels[0] if side > 0 else levels[1], self.rule.notional)
            self._open(side, px or (best_ask if side > 0 else best_bid), ts, self.rule.taker_fee)
            return None
        if self.pending and side_crossed(self.pending, best_bid, best_ask):
            self._fill_pending(ts)
            return None
        p = self.pos
        if not p:
            return None
        exit_px = best_bid if p.side > 0 else best_ask             # where a market close fills
        mid = (best_bid + best_ask) / 2
        move = p.side * (mid / p.entry_px - 1) * 1e4
        p.best, p.worst = max(p.best, move), min(p.worst, move)
        r = self.rule
        if p.side * (mid - p.liq_px) <= 0:
            return self._close(ts, p.liq_px, "liquidated", 0.0)
        if p.side * (exit_px - p.sl_px) <= 0:
            return self._close(ts, exit_px, "stop", r.taker_fee)
        if self.closing:                                  # our closing limit: the other side reached it
            px, placed, why = self.closing
            if (p.side > 0 and best_bid >= px) or (p.side < 0 and best_ask <= px):
                return self._close(ts, px, why, r.maker_fee)
            if ts - placed >= EXIT_WAIT_MS:
                return self._close(ts, exit_px, why, r.taker_fee)
            return None
        if self.style == "market" and p.side * (exit_px - p.tp_px) >= 0:
            return self._close(ts, exit_px, "target", r.taker_fee)
        if self.style != "market" and p.side * (exit_px - p.tp_px) > 0:        # the book moved through our resting take-profit
            return self._close(ts, p.tp_px, "target", r.maker_fee)
        return None

    def on_trade(self, ts: int, price: float) -> dict | None:
        """One trade print: fills resting limit orders it trades through (strictly: a print at our price might
        not have reached us in the queue)."""
        if self.pending:
            pe = self.pending
            if (pe.side > 0 and price < pe.price) or (pe.side < 0 and price > pe.price):
                self._fill_pending(ts)
            return None
        p = self.pos
        if p and self.closing:
            px, _, why = self.closing
            if (p.side > 0 and price > px) or (p.side < 0 and price < px):
                return self._close(ts, px, why, self.rule.maker_fee)
            return None
        if p and self.style != "market" and ((p.side > 0 and price > p.tp_px) or (p.side < 0 and price < p.tp_px)):
            return self._close(ts, p.tp_px, "target", self.rule.maker_fee)
        return None

    def _fill_pending(self, ts: int) -> None:
        pe = self.pending
        self.pending = None
        self._open(pe.side, pe.price, ts, self.rule.maker_fee)

    def on_tick(self, now_ms: int, side_now: int, best_bid: float, best_ask: float, funding_rate: float = 0.0) -> dict | None:
        """Once a second: funding, the limit entry timing out, the time limit and the flip exit."""
        if self.pending and now_ms - self.pending.placed_ms > self.rule.limit_wait_s * 1000:
            self.pending = None
            self.missed += 1
        p = self.pos
        if not p:
            return None
        if funding_rate:
            hours = (now_ms - p.last_funding_ms) / 3_600_000
            p.funding += p.side * funding_rate * p.notional * hours        # positive rate: longs pay
            p.last_funding_ms = now_ms
        exit_px = best_bid if p.side > 0 else best_ask
        if self.closing:
            if now_ms - self.closing[1] >= EXIT_WAIT_MS:              # nobody took our closing limit: cross
                return self._close(now_ms, exit_px, self.closing[2], self.rule.taker_fee)
            return None
        why = "time" if now_ms - p.entry_ms >= self.rule.max_hold_s * 1000 else \
            "flip" if self.rule.exit_on_flip and side_now == -p.side else None
        if why is None:
            return None
        if self.style == "maker_exit":                    # close with a limit at our side of the touch first
            self.closing = (best_ask if p.side > 0 else best_bid, now_ms, why)
            return None
        return self._close(now_ms, exit_px, why, self.rule.taker_fee)

    def _close(self, ts: int, px: float, reason: str, fee_rate: float) -> dict:
        p = self.pos
        self.pos = None
        self.closing = None
        self.last_exit_ms = ts
        r = self.rule
        gross = p.side * (px / p.entry_px - 1) * p.notional
        fees = p.fees + p.notional * (px / p.entry_px) * fee_rate
        net = gross - fees - p.funding
        if reason == "liquidated":                       # isolated: you lose the margin, never more
            net = -r.margin_usd
        self._roll_day(ts)
        self.day_net += net
        if self.day_net <= -r.daily_loss_usd:
            self.stopped_for_day = True
        return {"rule_id": r.id, "style": self.style, "coin": r.coin, "side": p.side, "entry_ms": p.entry_ms,
                "exit_ms": ts, "entry_px": p.entry_px, "exit_px": px, "reason": reason, "notional": p.notional,
                "margin": r.margin_usd, "gross_usd": gross, "fees_usd": fees, "funding_usd": p.funding, "net_usd": net,
                "net_pct": net / r.margin_usd * 100, "best_bps": p.best, "worst_bps": p.worst}

    def _roll_day(self, ts: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        if day != self.day:
            self.day, self.day_net, self.stopped_for_day = day, 0.0, False

    def drop(self) -> None:
        """Forget anything open (a replay jumped back in time, or the rule changed)."""
        self.pos = self.pending = self.market_due = self.closing = None

    def open_view(self, mid: float | None) -> dict | None:
        p = self.pos
        if not p:
            if self.pending:
                return {"style": self.style, "side": self.pending.side, "waiting": True, "price": self.pending.price,
                        "since_ms": self.pending.placed_ms}
            return None
        out = {"style": self.style, "side": p.side, "entry_px": p.entry_px, "entry_ms": p.entry_ms, "tp_px": p.tp_px,
               "sl_px": p.sl_px, "liq_px": p.liq_px}
        if mid:
            gross = p.side * (mid / p.entry_px - 1) * p.notional
            out["pnl_usd"] = gross - p.fees - p.funding
            out["pnl_pct"] = out["pnl_usd"] / self.rule.margin_usd * 100
        return out


def side_crossed(pe: Pending, best_bid: float, best_ask: float) -> bool:
    """The book moved through a resting limit order: the other side now trades at or beyond it."""
    return (pe.side > 0 and best_ask <= pe.price) or (pe.side < 0 and best_bid >= pe.price)


def stats(trades: list[dict], missed: int = 0) -> dict:
    """The track record of one rule in one style."""
    n = len(trades)
    out = {"trades": n, "missed": missed, "wins": 0, "win_rate": None, "net_usd": 0.0, "gross_usd": 0.0,
           "fees_usd": 0.0, "avg_net_pct": None, "t_stat": None, "max_drawdown_usd": 0.0, "avg_hold_s": None,
           "by_reason": {}, "verdict": "No trades yet."}
    if not n:
        return out
    nets = [t["net_usd"] for t in trades]
    pcts = [t["net_pct"] for t in trades]
    out["wins"] = sum(1 for x in nets if x > 0)
    out["win_rate"] = out["wins"] / n
    out["net_usd"], out["gross_usd"] = sum(nets), sum(t["gross_usd"] for t in trades)
    out["fees_usd"] = sum(t["fees_usd"] for t in trades)
    out["avg_net_pct"] = sum(pcts) / n
    out["avg_hold_s"] = sum((t["exit_ms"] - t["entry_ms"]) / 1000 for t in trades) / n
    for t in trades:
        out["by_reason"][t["reason"]] = out["by_reason"].get(t["reason"], 0) + 1
    if n >= 10:                                   # too few trades and the t-stat means nothing
        mean = sum(pcts) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in pcts) / (n - 1))
        out["t_stat"] = mean / (sd / math.sqrt(n)) if sd > 0 else None
    peak = run = 0.0
    for x in nets:
        run += x
        peak = max(peak, run)
        out["max_drawdown_usd"] = max(out["max_drawdown_usd"], peak - run)
    out["verdict"] = verdict(out)
    return out


def verdict(s: dict) -> str:
    n, t = s["trades"], s["t_stat"]
    if n < MIN_TRADES_FOR_VERDICT:
        return f"Too early to judge: {n} of {MIN_TRADES_FOR_VERDICT} trades."
    if s["net_usd"] <= 0:
        if s["gross_usd"] > 0:
            return "Wins before fees but loses after them: the costs eat the edge."
        return "Losing, even before fees."
    if t is not None and t >= 2:
        return "Profitable after costs, and steady enough that luck is an unlikely explanation."
    return "Up after costs so far, but not yet clearly more than luck."


def break_even_win_rate(rule: Rule, style: str) -> float | None:
    """The share of trades that must hit the target (the rest hitting the stop) to break even after fees."""
    t, m = rule.taker_fee, rule.maker_fee
    fee_in = m if style == "limit" else t
    fee_tp = t if style == "market" else m
    win = rule.take_profit_bps / 1e4 - fee_in - fee_tp
    loss = rule.stop_loss_bps / 1e4 + fee_in + t
    if win <= 0:
        return None
    return loss / (win + loss)

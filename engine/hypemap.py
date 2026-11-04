"""
HYPE holder map: who holds HYPE, grouped by what they've done (not by guessed motive), and the moment a group
switches. Everything comes from Hyperliquid's public info API (scripts/hypeProbe.py checks which calls answer and
in what shape; scripts/hypeMap.py crawls and reports). Pure functions here, no network.

    holdings    a wallet's HYPE: spot balance (spotClearinghouseState), staked (delegatorSummary: delegated plus
                undelegated staking balance) and leaving staking (pendingWithdrawal: 7 days from asking to being
                spendable, so it is the earliest public warning that a holder may sell)
    groups      Assistance Fund (only buys), not circulating (tokenDetails' non-circulating balances: reserves),
                labelled (a labels file: exchanges, market makers, team, by hand), then by behaviour: stakers (most
                of their HYPE staked), traders (seen trading on Hyperliquid in our recordings), holders (the rest)
    switching   staking flows by day from each wallet's history: HYPE staked, withdrawals started (the warning) and
                withdrawals finished (now spendable), and what unlocks over the next 7 days
    the test    does a day with unusually many withdrawals started come before weak HYPE? Decided before running:
                a spike day is in the top 10% of the 30 days before it (walk-forward, 14 days minimum); outcomes
                are HYPE's moves over the next 1, 3 and 7 days, and over days 7 to 9 (when those coins can be sold);
                a warning passes if the 7-day move after spikes is below the move after other days with t <= -2,
                and below it in both halves
"""
from collections import defaultdict
from dataclasses import dataclass, field

import math

DAY_MS = 86_400_000
UNSTAKE_DAYS = 7
ASSISTANCE_FUND = "0xfefefefefefefefefefefefefefefefefefefefe"   # widely reported; the probe checks it holds HYPE
SPIKE_Q = 0.90
TRAIL_DAYS = 30
MIN_TRAIL = 14
TIERS = ((1_000_000, "1M+"), (100_000, "100k–1M"), (10_000, "10k–100k"), (1_000, "1k–10k"), (0, "under 1k"))


def num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def spot_hype(state: dict | None) -> float:
    """HYPE in a spotClearinghouseState reply ({"balances": [{"coin", "total", "hold", ...}]})."""
    for b in (state or {}).get("balances") or []:
        if b.get("coin") == "HYPE" or b.get("token") == 150:
            return num(b.get("total"))
    return 0.0


def stake_of(summary: dict | None) -> dict:
    """delegatorSummary: delegated to validators, staked but not delegated, and on its way out."""
    s = summary or {}
    return {"delegated": num(s.get("delegated")), "undelegated": num(s.get("undelegated")),
            "pending": num(s.get("totalPendingWithdrawal")), "n_pending": int(num(s.get("nPendingWithdrawals")))}


def events_of(history: list | None, ledger: list | None = None) -> list[tuple[int, str, float]]:
    """A wallet's staking events, (time ms, kind, HYPE), from delegatorHistory and/or the non-funding ledger.
    Kinds: stake (spot into staking), unstake_start (a 7-day withdrawal asked for), unstake_done (it arrived in
    spot), delegate, undelegate. Shapes the probe hasn't confirmed are read both ways; anything unknown is skipped."""
    out = []
    for row in history or []:
        t, delta = int(num(row.get("time"))), row.get("delta") or {}
        if "cDeposit" in delta:
            out.append((t, "stake", num(delta["cDeposit"].get("amount"))))
        elif "withdrawal" in delta:
            w = delta["withdrawal"]
            phase = str(w.get("phase", "initiated")).lower()
            out.append((t, "unstake_done" if phase.startswith("final") else "unstake_start", num(w.get("amount"))))
        elif "delegate" in delta:
            d = delta["delegate"]
            out.append((t, "undelegate" if d.get("isUndelegate") else "delegate", num(d.get("amount"))))
    seen = {(t, k) for t, k, _ in out}
    for row in ledger or []:
        t, delta = int(num(row.get("time"))), row.get("delta") or {}
        if delta.get("type") == "cStakingTransfer" and str(delta.get("token", "HYPE")).upper() in ("HYPE", "150"):
            kind = "stake" if delta.get("isDeposit") else "unstake_done"
            if (t, kind) not in seen:
                out.append((t, kind, num(delta.get("amount"))))
    return sorted(out)


@dataclass
class Wallet:
    address: str
    spot: float = 0.0
    delegated: float = 0.0
    undelegated: float = 0.0
    pending: float = 0.0
    traded_usd: float = 0.0                    # taker and maker dollars on HYPE in our recordings
    label: str | None = None
    not_circulating: bool = False
    events: list = field(default_factory=list)

    @property
    def staked(self) -> float:
        return self.delegated + self.undelegated

    @property
    def total(self) -> float:
        return self.spot + self.staked + self.pending


def group_of(w: Wallet) -> str:
    if w.address.lower() == ASSISTANCE_FUND:
        return "Assistance Fund"
    if w.not_circulating:
        return "Not circulating"
    if w.label:
        return f"Labelled: {w.label}"
    if w.total and w.staked / w.total >= 0.5:
        return "Stakers"
    if w.traded_usd >= 10_000:
        return "Traders"
    return "Holders"


def tier_of(x: float) -> str:
    return next(name for lo, name in TIERS if x >= lo)


def groups(wallets: list[Wallet], supply: float | None = None) -> list[dict]:
    """Per group: wallets, HYPE, share of the supply (or of what was found), staked and leaving shares."""
    agg = defaultdict(lambda: {"wallets": 0, "hype": 0.0, "staked": 0.0, "pending": 0.0, "spot": 0.0})
    for w in wallets:
        if w.total <= 0:
            continue
        g = agg[group_of(w)]
        g["wallets"] += 1
        g["hype"] += w.total
        g["staked"] += w.staked
        g["pending"] += w.pending
        g["spot"] += w.spot
    base = supply or sum(g["hype"] for g in agg.values()) or 1.0
    return sorted(({"group": k, **v, "share": v["hype"] / base} for k, v in agg.items()), key=lambda r: -r["hype"])


def concentration(wallets: list[Wallet], supply: float | None = None, tops=(10, 100, 1000)) -> dict:
    """Share of the supply (or of what was found) in the biggest wallets, the Assistance Fund and reserves aside."""
    xs = sorted((w.total for w in wallets if group_of(w) not in ("Assistance Fund", "Not circulating")), reverse=True)
    base = supply or sum(xs) or 1.0
    return {n: sum(xs[:n]) / base for n in tops}


def daily_flows(wallets: list[Wallet]) -> dict[int, dict[str, float]]:
    """Every wallet's staking events summed by UTC day: {day start ms: {kind: HYPE}}."""
    out = defaultdict(lambda: defaultdict(float))
    for w in wallets:
        for t, kind, amt in w.events:
            out[t // DAY_MS * DAY_MS][kind] += amt
    return {d: dict(v) for d, v in sorted(out.items())}


def unlocks(wallets: list[Wallet], now_ms: int) -> list[tuple[int, float]]:
    """HYPE becoming spendable over the next 7 days: each withdrawal started in the last 7 days and not finished,
    7 days after it started. When the history doesn't cover a wallet's pending amount, the rest shows as "unknown
    date" (day None)."""
    by_day = defaultdict(float)
    for w in wallets:
        if w.pending <= 0:
            continue
        starts = [(t, a) for t, k, a in w.events if k == "unstake_start" and now_ms - t < UNSTAKE_DAYS * DAY_MS]
        left = w.pending
        for t, a in sorted(starts, reverse=True):                # newest first: the pending total is the newest ones
            take = min(a, left)
            if take <= 0:
                break
            by_day[(t + UNSTAKE_DAYS * DAY_MS) // DAY_MS * DAY_MS] += take
            left -= take
        if left > 1e-9:
            by_day[None] += left
    return sorted(by_day.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))


def _t(diff: list[float], base: list[float]) -> float:
    """Welch's t for the difference of two means."""
    if len(diff) < 3 or len(base) < 3:
        return float("nan")
    m1, m2 = sum(diff) / len(diff), sum(base) / len(base)
    v1 = sum((x - m1) ** 2 for x in diff) / (len(diff) - 1)
    v2 = sum((x - m2) ** 2 for x in base) / (len(base) - 1)
    se = math.sqrt(v1 / len(diff) + v2 / len(base))
    return (m1 - m2) / se if se else float("nan")


def spike_test(flows: dict[int, dict[str, float]], closes: dict[int, float], kind: str = "unstake_start") -> dict:
    """The pre-registered test (see the top). `closes`: HYPE's daily close by UTC day start. Returns per horizon the
    mean move (%) after spike days and after other days, Welch's t, and the halves."""
    days = sorted(d for d in closes if d >= min(flows, default=d))
    if not days:
        return {"n_days": 0}
    vol = {d: flows.get(d, {}).get(kind, 0.0) for d in days}
    spikes, rest = [], []
    for i, d in enumerate(days):
        past = [vol[x] for x in days[max(0, i - TRAIL_DAYS):i]]
        if len(past) < MIN_TRAIL:
            continue
        cut = sorted(past)[min(len(past) - 1, int(len(past) * SPIKE_Q))]
        (spikes if vol[d] > cut and vol[d] > 0 else rest).append(d)

    def move(d, a, b):
        p0, p1 = closes.get(d + a * DAY_MS), closes.get(d + b * DAY_MS)
        return (p1 / p0 - 1) * 100 if p0 and p1 else None

    out = {"n_days": len(spikes) + len(rest), "n_spikes": len(spikes), "spike_days": spikes}
    for name, (a, b) in {"1d": (0, 1), "3d": (0, 3), "7d": (0, 7), "days 7-9": (7, 9)}.items():
        s = [m for m in (move(d, a, b) for d in spikes) if m is not None]
        r = [m for m in (move(d, a, b) for d in rest) if m is not None]
        out[name] = {"spike": sum(s) / len(s) if s else float("nan"), "other": sum(r) / len(r) if r else float("nan"),
                     "t": _t(s, r), "n": len(s)}
    tested = sorted(spikes + rest)
    if tested:
        mid = tested[len(tested) // 2]
        halves = []
        for part in (lambda d: d < mid, lambda d: d >= mid):
            s = [m for m in (move(d, 0, 7) for d in spikes if part(d)) if m is not None]
            r = [m for m in (move(d, 0, 7) for d in rest if part(d)) if m is not None]
            halves.append((sum(s) / len(s) if s else float("nan")) - (sum(r) / len(r) if r else float("nan")))
        out["halves_7d"] = halves
    h = out.get("7d", {})
    out["passes"] = bool(h and h["spike"] < h["other"] and h["t"] <= -2
                         and all(x < 0 for x in out.get("halves_7d", [float("nan")])))
    return out

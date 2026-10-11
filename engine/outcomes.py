"""
Prediction markets on Hyperliquid (HIP-4 "outcome" markets): Hyperliquid's raw listing turned into readable
markets, plus the reads Analytix adds on top. Pure functions with no network calls; api/outcomes.py feeds them.

How the markets work
  - Each market (an "outcome") has a Yes side and a No side. Yes pays $1 if it happens, No pays $1 if it
    doesn't. So Yes at 0.62 reads as "62% likely", and a contract costs 62 cents.
  - Yes and No share one order book: buying Yes at 0.62 is the same as selling No at 0.38. Analytix reads the
    Yes side and mirrors it for No.
  - Coins are "#<10 * outcome + side>": #112140 is outcome 11214's Yes side, #112141 its No side.
  - A question groups outcomes where exactly one ends Yes (a match: home win, away win, draw, or "none of
    these" if it's called off). Their Yes prices should add up to about 100%.
  - `venue` says who listed the market: "out" is Outcome (outcome.xyz), "txyz" is trade.xyz.

What Analytix adds
  - fair odds for price markets ("ETH above 2,538.8 at 18:30"), from the perp's live price and how much it has
    been moving lately
  - the odds add-up check for questions: when Yes on every outcome costs under $1, the difference is locked in
  - who is trading: the wallets behind the volume, and pairs of wallets trading only with each other
  - a plain line on why the odds moved
"""
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

VENUES = {"out": "Outcome", "txyz": "trade.xyz"}
PRICE_TEMPLATES = {"binaryPrice", "priceBinary"}    # Yes if the price is at or above the line at a set time
TOUCH_TEMPLATES = {"priceTouch"}                    # Yes if the price reaches the line before a set time
LABEL_KEYS = ("participant", "candidate", "company", "nominee", "team", "player", "option", "label", "name")
DETAIL_KEYS = ("threshold", "line", "spread", "total", "marketCap", "value", "targetPrice")
QUIET_KEYS = {"resolutionDeadline", "officialSource", "priceDescription", "seconds", "time", "expiry", "countedPlay",
              "season", "sport", "scheduledStart", "stage", "contestType", "class", "period", "shortNameA", "shortNameB",
              "shortName", "participantA", "participantB", "competition", "awardingBody", "field"}
WIDE_SPREAD = 0.10        # wider than 10 points, the mid says little: odds come from the last trade instead
MIN_LOCKED_EDGE = 0.005   # half a cent per set before the add-up check calls it out
STEADY_POINTS = 2.0       # an odds change smaller than this many points reads as "steady"


# ── the listing ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Market:
    outcome: int
    template: str                  # "binaryPrice", "sportsContestParticipant2", "fallback", ...
    venue: str                     # "out", "txyz", ...
    fields: dict
    sides: tuple[str, str]         # usually ("Yes", "No")
    kind: str                      # "price" | "touch" | "sports" | "other"
    title: str                     # readable on its own: "ETH above 2,538.8 at 11 Oct 18:30 UTC"
    label: str                     # short, inside its question: "AEK Athens", "Draw"
    underlying: str | None = None  # the perp it settles on: "ETH", "xyz:SILVER"
    threshold: float | None = None
    expiry_ms: int | None = None
    question: int | None = None
    settled: bool = False
    sub: str = ""                  # context under the title: "NFL · starts 12 Oct 17:00 UTC"

    @property
    def yes_coin(self) -> str:
        return f"#{10 * self.outcome}"

    @property
    def no_coin(self) -> str:
        return f"#{10 * self.outcome + 1}"

    @property
    def venue_name(self) -> str:
        return VENUES.get(self.venue, self.venue or "Hyperliquid")


@dataclass(slots=True)
class Question:
    question: int
    template: str
    fields: dict
    title: str
    sub: str                        # competition, stage, kick-off
    outcomes: tuple[int, ...]       # the named outcomes, then the fallback ("none of these")
    fallback: int | None
    settled: tuple[int, ...] = ()
    expiry_ms: int | None = None
    starts_ms: int | None = None


def parse_fields(desc: str | None) -> dict[str, str]:
    """"perp:ETH|threshold:2538.8|time:20261011-1830" → {"perp": "ETH", "threshold": "2538.8", ...}. Only the
    first colon splits, so "perp:xyz:SILVER" keeps "xyz:SILVER"."""
    out = {}
    for part in str(desc or "").split("|"):
        k, sep, v = part.partition(":")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def parse_time(s: str | None) -> int | None:
    """"20261011-1830" (UTC) → ms."""
    for fmt in ("%Y%m%d-%H%M", "%Y%m%d"):
        try:
            return int(datetime.strptime(str(s), fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except (TypeError, ValueError):
            continue
    return None


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def template_of(name: str | None, fields: dict) -> str:
    n = str(name or "")
    if n.startswith("template:"):
        return n[len("template:"):]
    if n.startswith("template "):                     # the fallback outcomes are named "template fallback"
        return n[len("template "):].strip()
    return fields.get("class") or ""                  # older listings: name "Recurring", class:priceBinary


def humanize(key: str) -> str:
    """"sportsContestWinner" → "Sports contest winner"."""
    out = "".join(" " + c.lower() if c.isupper() else c for c in str(key)).strip()
    return out[:1].upper() + out[1:]


def asset_name(perp: str | None) -> str:
    return (perp or "").split(":")[-1]


def fmt_line(x: float | None) -> str:
    if x is None:
        return "-"
    if x >= 1000:
        return f"{x:,.1f}".rstrip("0").rstrip(".") if x % 1 else f"{x:,.0f}"
    return f"{x:,.6g}"


def fmt_when(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%-d %b %H:%M UTC")


def fill(text: str, f: dict) -> str:
    """Side names can be templates: "{shortNameA}" → "Pittsburgh Steelers" (the full name when the listing has it,
    else the short one); any other "{key}" → that field."""
    def sub(m):
        key = m.group(1)
        if key.startswith("shortName") and f.get("participant" + key[len("shortName"):]):
            return f["participant" + key[len("shortName"):]]
        return f.get(key) or key
    return re.sub(r"\{(\w+)\}", sub, text)


def _side_name(spec: dict, default: str, f: dict | None = None) -> str:
    n = str((spec or {}).get("name") or default)
    n = n[len("template:"):] if n.startswith("template:") else n
    return fill(n, f or {})


def _details(f: dict) -> list[str]:
    return [f"{humanize(k).lower()} {f[k]}" for k in DETAIL_KEYS if f.get(k)]


def matchup(f: dict) -> str | None:
    a, b = f.get("participantA"), f.get("participantB")
    return f"{a} vs {b}" if a and b else None


def context_line(f: dict) -> str:
    """"NFL · Week 6 · starts 12 Oct 17:00 UTC"""
    parts = [x for x in (f.get("competition"), f.get("stage"), f.get("awardingBody")) if x]
    starts = parse_time(f.get("scheduledStart"))
    if starts:
        parts.append(f"starts {fmt_when(starts)}")
    return " · ".join(parts)


def generic_label(template: str, f: dict) -> str:
    main = next((f[k] for k in LABEL_KEYS if f.get(k)), None) or matchup(f)
    extra = _details(f)
    if main:
        return main + (f" ({', '.join(extra)})" if extra else "")
    rest = [f"{humanize(k)} {v}" for k, v in f.items() if k not in QUIET_KEYS and v]
    return " · ".join(rest) if rest else (humanize(template) or "Market")


def market_from(raw: dict) -> Market | None:
    oid = raw.get("outcome")
    if not isinstance(oid, int):
        return None
    f = parse_fields(raw.get("description"))
    template = template_of(raw.get("name"), f)
    specs = raw.get("sideSpecs") or [{}, {}]
    sides = (_side_name(specs[0] if specs else {}, "Yes", f), _side_name(specs[1] if len(specs) > 1 else {}, "No", f))
    venue = str(raw.get("venue") or "")
    if template in PRICE_TEMPLATES | TOUCH_TEMPLATES:
        under = f.get("perp") or f.get("underlying") or f.get("spot")
        line = _num(f.get("threshold") or f.get("targetPrice"))
        expiry = parse_time(f.get("time") or f.get("expiry"))
        if under and line is not None and expiry is not None:
            touch = template in TOUCH_TEMPLATES
            a, when = asset_name(under), fmt_when(expiry)
            title = f"{a} touches {fmt_line(line)} by {when}" if touch else f"{a} above {fmt_line(line)} at {when}"
            return Market(oid, template, venue, f, sides, "touch" if touch else "price", title, title,
                          under, line, expiry, sub=f.get("priceDescription", ""))
    if template == "fallback":
        label = "None of these"
    elif template.startswith("sportsContestDraw"):
        label = "Draw"
    elif matchup(f):
        extra = _details(f)
        label = matchup(f) + (f" ({', '.join(extra)})" if extra else "")
    elif raw.get("name") and not str(raw.get("name")).startswith("template"):
        label = str(raw["name"])
    else:
        label = generic_label(template, f)
    kind = "sports" if template.startswith("sports") else "other"
    expiry = parse_time(f.get("resolutionDeadline") or f.get("time") or f.get("expiry"))
    return Market(oid, template, venue, f, sides, kind, label, label, expiry_ms=expiry, sub=context_line(f))


def question_title(template: str, f: dict) -> tuple[str, str]:
    """(title, sub) for a question: "RB Leipzig vs PSV Eindhoven", "UEFA Champions League · Matchday 2"."""
    a, b = f.get("participantA"), f.get("participantB")
    if a and b:
        title = f"{a} vs {b}"
    elif f.get("award"):
        title = " ".join(x for x in (f["award"], f.get("period")) if x)
    elif f.get("competition"):
        title = " ".join(x for x in (f["competition"], f.get("season")) if x)
        if "winner" in template.lower():
            title += " winner"
    else:
        main = next((f[k] for k in LABEL_KEYS if f.get(k)), None)
        title = main or humanize(template) or "Question"
    sub = [x for x in (f.get("competition") if a and b else None, f.get("stage"), f.get("awardingBody")) if x]
    starts = parse_time(f.get("scheduledStart"))
    if starts:
        sub.append(f"starts {fmt_when(starts)}")
    return title, " · ".join(sub)


def build_markets(meta: dict) -> tuple[dict[int, Market], dict[int, Question]]:
    """outcomeMeta → ({outcome id: Market}, {question id: Question}). Outcomes inside a question get the
    question's name in their title ("RB Leipzig vs PSV Eindhoven: Draw") and its deadline when they have none."""
    markets = {m.outcome: m for m in (market_from(o) for o in meta.get("outcomes") or [] if isinstance(o, dict)) if m}
    questions: dict[int, Question] = {}
    for q in meta.get("questions") or []:
        if not isinstance(q, dict) or not isinstance(q.get("question"), int):
            continue
        f = parse_fields(q.get("description"))
        template = template_of(q.get("name"), f)
        title, sub = question_title(template, f)
        named = [o for o in q.get("namedOutcomes") or [] if isinstance(o, int)]
        fb = q.get("fallbackOutcome") if isinstance(q.get("fallbackOutcome"), int) else None
        ids = tuple(o for o in named + ([fb] if fb is not None else []) if o in markets)
        settled = tuple(o for o in q.get("settledNamedOutcomes") or [] if isinstance(o, int))
        expiry = parse_time(f.get("resolutionDeadline"))
        questions[q["question"]] = Question(q["question"], template, f, title, sub, ids, fb, settled, expiry,
                                            parse_time(f.get("scheduledStart")))
        for oid in ids:
            m = markets[oid]
            m.question = q["question"]
            m.title = f"{title}: {m.label}"
            m.sub = sub
            m.settled = oid in settled
            if m.expiry_ms is None:
                m.expiry_ms = expiry
            if m.kind == "other" and template.startswith("sports"):
                m.kind = "sports"
    return markets, questions


# ── prices and odds ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Book:
    """The Yes side's book, best first: [(price, contracts)]."""
    t: int
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


def parse_levels(levels) -> list[tuple[float, float]]:
    out = []
    for lv in levels or []:
        p, s = _num(lv.get("px")), _num(lv.get("sz"))
        if p is not None and s is not None:
            out.append((p, s))
    return out


def odds(bid: float | None, ask: float | None, last: float | None) -> float | None:
    """The market's odds for Yes (0..1): the mid when the spread is tight, otherwise the last trade (a 0.01 /
    0.99 book says nothing), and the mid as a last resort."""
    if bid is not None and ask is not None and ask - bid <= WIDE_SPREAD:
        return (bid + ask) / 2
    if last is not None:
        return last
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return None


def push_cost(levels: list[tuple[float, float]], points: float = 5.0, up: bool = True) -> float | None:
    """Dollars of Yes buying (up) or selling (down) it takes to move the price `points` points from the best
    level. None when the book doesn't reach that far."""
    if not levels:
        return None
    start, usd = levels[0][0], 0.0
    limit = start + points / 100 if up else start - points / 100
    for p, s in levels:
        if (up and p > limit) or (not up and p < limit):
            return usd
        usd += p * s
    return None


# ── fair odds for price markets ─────────────────────────────────────────────
def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def sigma_per_sqrt_s(closes: list[float], step_s: float) -> float | None:
    """How much the price moves: the standard deviation of log returns, per square-root second."""
    xs = [c for c in closes if c and c > 0]
    if len(xs) < 30:
        return None
    r = [math.log(b / a) for a, b in zip(xs, xs[1:])]
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var / step_s) if var > 0 else None


def fair_above(spot: float | None, line: float | None, sigma_s: float | None, tau_s: float) -> float | None:
    """Chance the price is at or above `line` in tau_s seconds, if it keeps moving the way it has been (a random
    walk in log price with no drift)."""
    if not spot or not line or spot <= 0 or line <= 0:
        return None
    if tau_s <= 0:
        return 1.0 if spot >= line else 0.0
    if not sigma_s:
        return None
    sd = sigma_s * math.sqrt(tau_s)
    return norm_cdf((math.log(spot / line) - 0.5 * sd * sd) / sd)


def fair_touch(spot: float | None, line: float | None, sigma_s: float | None, tau_s: float) -> float | None:
    """Chance the price reaches `line` within tau_s seconds (from either side), same random walk."""
    if not spot or not line or spot <= 0 or line <= 0 or tau_s <= 0 or not sigma_s:
        return None
    d = abs(math.log(line / spot))
    if d == 0:
        return 1.0
    return min(1.0, 2 * norm_cdf(-d / (sigma_s * math.sqrt(tau_s))))


def fair_odds(m: Market, spot: float | None, sigma_s: float | None, now_ms: int) -> float | None:
    if m.expiry_ms is None:
        return None
    tau = (m.expiry_ms - now_ms) / 1000
    if m.kind == "price":
        return fair_above(spot, m.threshold, sigma_s, tau)
    if m.kind == "touch":
        return fair_touch(spot, m.threshold, sigma_s, tau)
    return None


def fair_series(m: Market, prices: list[tuple[int, float]], sigma_s: float | None) -> list[tuple[int, float]]:
    """Fair odds at each past moment, from the perp's price then (for the chart). Price markets only: a touch
    market's history would also need to know whether the line had been reached."""
    if m.kind != "price" or m.expiry_ms is None or not sigma_s:
        return []
    out = []
    for t, px in prices:
        if t < m.expiry_ms:
            p = fair_above(px, m.threshold, sigma_s, (m.expiry_ms - t) / 1000)
            if p is not None:
                out.append((t, p))
    return out


def trade_edge(fair: float | None, bid: float | None, ask: float | None) -> float | None:
    """How far the fair odds sit outside the prices you can trade, in points: positive when fair is above the ask
    (buy Yes), negative when it's below the bid (sell Yes, i.e. buy No), 0 inside the spread. Never measured
    against a last trade or mark, which on a thin market can be days old."""
    if fair is None or (bid is None and ask is None):
        return None
    if ask is not None and fair > ask:
        return (fair - ask) * 100
    if bid is not None and fair < bid:
        return (fair - bid) * 100
    return 0.0


def ladder_check(rows: list[dict]) -> list[dict]:
    """Price markets on one asset closing at the same moment: "above" a higher line can never be likelier than
    "above" a lower one. When the higher line's Yes sells (bid) for more than the lower line's Yes costs (ask),
    buying Yes on the lower and No on the higher pays at least $1 whatever happens, for less than $1.
    rows: {"id", "kind", "threshold", "expiry_ms", "bid", "bid_sz", "ask", "ask_sz"}."""
    found = []
    by_close: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("kind") == "price" and r.get("threshold") is not None and r.get("expiry_ms"):
            by_close[r["expiry_ms"]].append(r)
    for group in by_close.values():
        group.sort(key=lambda r: r["threshold"])
        for i, lo in enumerate(group):
            for hi in group[i + 1:]:
                if lo.get("ask") is None or hi.get("bid") is None or hi["threshold"] <= lo["threshold"]:
                    continue
                locked = hi["bid"] - lo["ask"]
                if locked >= MIN_LOCKED_EDGE:
                    pairs = min(lo.get("ask_sz") or 0, hi.get("bid_sz") or 0)
                    a, b = fmt_line(lo["threshold"]), fmt_line(hi["threshold"])
                    found.append({"low": lo["id"], "high": hi["id"], "locked": locked, "pairs": pairs, "note": (
                        f"Above {b} sells for {hi['bid'] * 100:.1f}¢ but above {a} costs {lo['ask'] * 100:.1f}¢. Price "
                        f"can't end above {b} without ending above {a}, so Yes on {a} plus No on {b} locks in "
                        f"{locked * 100:.1f}¢ a pair ({pairs:,.0f} pairs at these prices, before fees).")})
    return sorted(found, key=lambda f: -f["locked"])


# ── questions: do the odds add up? ──────────────────────────────────────────
@dataclass(slots=True)
class SumCheck:
    n: int
    mid_sum: float | None           # the Yes odds added up (1.0 = 100%)
    ask_sum: float | None           # what one Yes on every outcome costs
    bid_sum: float | None           # what one Yes on every outcome sells for
    locked: float | None            # dollars locked in per set, when there is an edge
    sets: float | None              # how many sets the best prices have room for
    way: str | None                 # "yes" (buy Yes on all) | "no" (buy No on all)
    note: str


def sum_check(quotes: list[tuple[float | None, float, float | None, float, float | None]]) -> SumCheck:
    """quotes: one (bid, bid size, ask, ask size, odds) per outcome of a question. Exactly one outcome ends
    Yes, so one Yes on each pays exactly $1 and one No on each pays exactly n - 1 dollars."""
    n = len(quotes)
    mids = [q[4] for q in quotes if q[4] is not None]
    mid_sum = sum(mids) if mids else None
    asks_ok = n > 1 and all(q[2] is not None for q in quotes)
    bids_ok = n > 1 and all(q[0] is not None for q in quotes)
    ask_sum = sum(q[2] for q in quotes) if asks_ok else None
    bid_sum = sum(q[0] for q in quotes) if bids_ok else None
    if ask_sum is not None and 1 - ask_sum >= MIN_LOCKED_EDGE:
        sets = min(q[3] for q in quotes)
        edge = 1 - ask_sum
        note = (f"Yes on all {n} costs {ask_sum * 100:.1f}¢ and exactly one pays $1, so {edge * 100:.1f}¢ is locked "
                f"in per set ({sets:,.0f} sets at these prices, before fees).")
        return SumCheck(n, mid_sum, ask_sum, bid_sum, edge, sets, "yes", note)
    if bid_sum is not None and bid_sum - 1 >= MIN_LOCKED_EDGE:
        sets = min(q[1] for q in quotes)
        edge = bid_sum - 1
        note = (f"No on all {n} costs ${n - bid_sum:.3f} and pays ${n - 1}, so {edge * 100:.1f}¢ is locked in per set "
                f"({sets:,.0f} sets at these prices, before fees).")
        return SumCheck(n, mid_sum, ask_sum, bid_sum, edge, sets, "no", note)
    if mid_sum is None:
        note = "No outcome has a price yet."
    elif len(mids) == n:
        note = f"The odds add up to {mid_sum * 100:.0f}%."
    else:
        note = f"{len(mids)} of {n} outcomes have a price; those add up to {mid_sum * 100:.0f}%."
    return SumCheck(n, mid_sum, ask_sum, bid_sum, None, None, None, note)


# ── who is trading ──────────────────────────────────────────────────────────
def short_wallet(w: str | None) -> str:
    return f"{w[:6]}…{w[-4:]}" if w and len(w) > 12 else (w or "?")


def who(trades: list[dict]) -> dict:
    """trades on the Yes side: {"px", "sz", "side" ("B" taker bought Yes / "A" taker sold), "buyer", "seller"}.
    Selling Yes is the same as backing No. Returns the busiest wallets and plain notes on anything odd."""
    vol = sum(t["sz"] for t in trades)
    out = {"trades": len(trades), "contracts": vol, "wallets": [], "notes": []}
    if not trades or vol <= 0:
        return out
    rows: dict[str, dict] = defaultdict(lambda: {"yes": 0.0, "no": 0.0, "usd": 0.0, "trades": 0, "taker": 0.0})
    pairs: dict[frozenset, dict] = defaultdict(lambda: {"sz": 0.0, "dirs": set()})
    self_sz = 0.0
    for t in trades:
        b, s, sz, px = t.get("buyer"), t.get("seller"), t["sz"], t["px"]
        taker = b if t.get("side") == "B" else s
        if b and s and b == s:
            self_sz += sz
        for w, yes in ((b, True), (s, False)):
            if not w:
                continue
            r = rows[w]
            r["yes" if yes else "no"] += sz
            r["usd"] += sz * (px if yes else 1 - px)
            r["trades"] += 1
            if w == taker:
                r["taker"] += sz
        if b and s and b != s:
            p = pairs[frozenset((b, s))]
            p["sz"] += sz
            p["dirs"].add(b)
    total_wallet_sz = 2 * vol
    ranked = sorted(rows.items(), key=lambda kv: -(kv[1]["yes"] + kv[1]["no"]))
    out["wallets"] = [{"wallet": w, "short": short_wallet(w), "yes": r["yes"], "no": r["no"], "net": r["yes"] - r["no"],
                       "usd": r["usd"], "trades": r["trades"], "share": (r["yes"] + r["no"]) / total_wallet_sz}
                      for w, r in ranked[:8]]
    out["count"] = len(rows)
    notes = out["notes"]
    if pairs:
        (pk, top) = max(pairs.items(), key=lambda kv: kv[1]["sz"])
        share = top["sz"] / vol
        if share >= 0.5 and len(top["dirs"]) == 2:
            a, b = sorted(pk)
            notes.append(f"{share * 100:.0f}% of the trading was two wallets ({short_wallet(a)} and {short_wallet(b)}) "
                         f"buying from and selling to each other. That volume doesn't show real interest.")
    if self_sz / vol >= 0.2:
        notes.append(f"{self_sz / vol * 100:.0f}% of the trading was a wallet trading with itself.")
    buyers = sorted(((w, r["yes"]) for w, r in rows.items()), key=lambda x: -x[1])
    sellers = sorted(((w, r["no"]) for w, r in rows.items()), key=lambda x: -x[1])
    for (w, sz), side in ((buyers[0], "Yes"), (sellers[0], "No")):
        if len(rows) >= 3 and sz / vol >= 0.6:
            notes.append(f"One wallet ({short_wallet(w)}) did {sz / vol * 100:.0f}% of the {side} buying.")
    if not notes:
        notes.append(f"{len(rows)} wallets traded; no single wallet or pair stands out.")
    return out


# ── why the odds moved ──────────────────────────────────────────────────────
def flow_of(trades: list[dict], since_ms: int) -> dict:
    """Taker flow since a moment: Yes contracts bought vs sold by whoever crossed the spread."""
    bought = sold = 0.0
    takers: dict[str, float] = defaultdict(float)
    for t in trades:
        if t["t"] < since_ms:
            continue
        if t.get("side") == "B":
            bought += t["sz"]
            w = t.get("buyer")
        else:
            sold += t["sz"]
            w = t.get("seller")
        if w:
            takers[w] += t["sz"]
    top = max(takers.items(), key=lambda kv: kv[1]) if takers else (None, 0.0)
    total = bought + sold
    return {"bought": bought, "sold": sold, "net": bought - sold, "total": total,
            "top": top[0], "top_share": top[1] / total if total else 0.0}


def why_line(m: Market, now: float | None, then: float | None, span: str, flow: dict | None = None,
             fair_now: float | None = None, fair_then: float | None = None,
             under_now: float | None = None, under_then: float | None = None) -> str | None:
    """One plain sentence on how the odds moved over `span` ("the last hour") and what moved them."""
    if now is None or then is None:
        return None
    pts = (now - then) * 100
    if abs(pts) < STEADY_POINTS:
        return f"Odds steady around {now * 100:.0f}% over {span}."
    move = f"Odds {'rose' if pts > 0 else 'fell'} {abs(pts):.0f} points ({then * 100:.0f}% to {now * 100:.0f}%) over {span}"
    if m.kind in ("price", "touch") and under_now and under_then:
        a = asset_name(m.underlying)
        chg = (under_now / under_then - 1) * 100
        where = ("" if m.threshold is None else
                 f" (now {'above' if under_now >= m.threshold else 'below'} {fmt_line(m.threshold)})")
        if fair_now is not None and fair_then is not None:
            fpts = (fair_now - fair_then) * 100
            if fpts * pts > 0 and abs(fpts) >= 0.5 * abs(pts):
                return f"{move}, in line with {a} moving {chg:+.2f}%{where}."
            if abs(fpts) < 0.5 * abs(pts):
                return (f"{move}, while the fair odds from {a}'s price barely changed ({fair_then * 100:.0f}% to "
                        f"{fair_now * 100:.0f}%): the move came from trading here{_flow_tail(flow, pts)}.")
    tail = _flow_tail(flow, pts)
    if flow and flow["total"] > 0:
        return f"{move}{tail}."
    return f"{move} with little trading: quotes moved, not trades."


def _flow_tail(flow: dict | None, pts: float) -> str:
    if not flow or flow["total"] <= 0:
        return ""
    side = "Yes" if flow["net"] >= 0 else "No"
    tail = f", on {abs(flow['net']):,.0f} contracts of net {side} buying"
    if flow["top"] and flow["top_share"] >= 0.6:
        tail += f", mostly from one wallet ({short_wallet(flow['top'])})"
    if (flow["net"] >= 0) != (pts >= 0):
        tail += " (against the move: resting orders were pulled or moved)"
    return tail

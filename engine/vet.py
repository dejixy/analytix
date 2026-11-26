"""
The combo builder's Vet: a SportyBet ticket, loaded from its booking code, checked game by game against the board.

Each game on the ticket gets the board's chance (Pinnacle's, or a model's) and its edge at SportyBet's price, then one of:
  keep      nothing clearly better found
  swap      another game, at least as long a price (within 3%), that is 5+ points more likely: the ticket keeps its odds
  safer     another bet on the same game, 5+ points more likely, at 80%+ of its price and returning about as much on
            average (within 2 points)
  drop      SportyBet pays well under its chance (edge under -5%): the ticket's average return rises without it
and the ones that can't be judged: started (kicked off or not "Not start" on SportyBet), off (SportyBet has suspended the
bet), unpriced (the game is on the board but not this bet) and unknown (the game isn't on the board: no sharp price).
Every option shows what the ticket becomes (total odds and chance). The fixes are taken least likely game first, each
replacement game used once, and the result is the vetted ticket, with SportyBet's ids for a booking code.

It can't say which game will go wrong: it finds the games that carry more risk than they pay for, and likelier ones
at the same price.
"""
import math

from engine import betting as B
from engine import combos as C

GAIN = 0.05            # a swap or a safer bet: at least this much more likely (points of chance)
SWAP_ODDS = 0.97       # a swap's price: at least 97% of the bet it replaces
SAFER_EV = 0.02        # a safer bet may return up to 2 points less on average
SAFER_ODDS = 0.80      # and keeps at least 80% of the price: an adjustment, not a different ticket
EDGE_BAD = -0.05       # SportyBet's price this far under fair: dropped
START_SLACK_MS = 10 * 60_000


def sig(event, market, specifier, outcome) -> tuple:
    return (str(event or ""), str(market or ""), str(specifier or "").replace(" ", ""), str(outcome or ""))


def index(board: list[dict]) -> tuple[dict, dict]:
    """The board's SportyBet bets by SportyBet's own ids ({sig: (bet, SportyBet odds)}) and its games by SportyBet's
    event id ({event: match_id})."""
    by_sig, by_event = {}, {}
    for x in board:
        for o in x.get("others") or []:
            s = o.get("sel") or {}
            if o.get("book") == "sportybet" and s.get("event"):
                by_sig[sig(s["event"], s.get("market"), s.get("specifier"), s.get("outcome"))] = (x, o["odds"])
                by_event[str(s["event"])] = x["match_id"]
    return by_sig, by_event


def _ev(x: dict) -> float:
    """What a bet returns on average for each 1 staked (a refund counts as 1 back)."""
    return x["odds"] * x["p_win"] + x.get("p_void", 0.0)


def _ticket(legs: list[dict]) -> dict:
    """A ticket's total odds, and its chance and edge over the games we can price."""
    priced = [x for x in legs if x.get("p") is not None]
    return {"odds": math.prod(x["odds"] for x in legs) if legs else None, "n": len(legs), "priced": len(priced),
            "p": math.prod(x["p"] for x in priced) if priced else None,
            "edge": math.prod(_ev(x) for x in priced) - 1 if priced else None,
            "least": min((x["p"] for x in priced), default=None)}


def vet(ticket: list[dict], board: list[dict], pool: list[dict], now_ms: int) -> dict:
    """ticket: SportyBet's selections as the collector read them ({event, market, specifier, outcome, odds, active,
    home, away, t_ms, league, market_desc, desc, status}); pool: the SportyBet bets a slip may use (combos.legs)."""
    by_sig, by_event = index(board)
    legs = []
    for i, t in enumerate(ticket):
        got = by_sig.get(sig(t.get("event"), t.get("market"), t.get("specifier"), t.get("outcome")))
        x = got[0] if got else None
        odds = float(t.get("odds") or (got[1] if got else 0) or 0)
        leg = {"i": i, "home": t.get("home") or (x or {}).get("home", ""), "away": t.get("away") or (x or {}).get("away", ""),
               "t_ms": int(t.get("t_ms") or (x or {}).get("t_ms") or 0), "league": t.get("league") or (x or {}).get("league_name", ""),
               "label": " ".join(s for s in (t.get("market_desc"), t.get("desc")) if s), "odds": odds,
               "sel": {"event": str(t.get("event") or ""), "market": str(t.get("market") or ""),
                       "specifier": str(t.get("specifier") or ""), "outcome": str(t.get("outcome") or "")},
               "match_id": (x or {}).get("match_id") or by_event.get(str(t.get("event") or "")), "p": None}
        if x:
            pv = x.get("p_void") or 0.0
            leg.update(market=x["market"], home=x["home"], away=x["away"], p=x["p"] + pv, p_win=x["p"], p_void=pv,
                       chance_raw=x["p"] + pv, model=x.get("model"), sharp_src=x.get("sharp_src"),
                       edge=odds * x["p"] + pv - 1 if odds > 1 else None, fair_odds=(1 - pv) / x["p"] if x["p"] else None)
        started = leg["t_ms"] and leg["t_ms"] <= now_ms + START_SLACK_MS or (t.get("status") not in (None, "", "Not start"))
        leg["state"] = ("started" if started else "off" if t.get("active") in (0, "0", False) else
                        "unknown" if leg["match_id"] is None else "unpriced" if leg["p"] is None else "priced")
        legs.append(leg)

    in_ticket = {x["match_id"] for x in legs if x["match_id"] is not None}
    games: dict = {}
    for y in pool:
        games.setdefault(y["match_id"], []).append(y)
    live = [x for x in legs if x["state"] != "started"]
    until = max([B.day_end(now_ms)] + [x["t_ms"] for x in live])
    taken: set = set()
    base = _ticket(legs)

    def after(leg: dict, new: dict | None) -> dict:
        """The ticket with this game's bet replaced by `new` (None: dropped)."""
        rest = [x for x in legs if x is not leg]
        return _ticket(rest + ([new] if new else []))

    def as_leg(y: dict) -> dict:
        pv = y.get("p_void") or 0.0
        return {"market": y["market"], "home": y["home"], "away": y["away"], "t_ms": y["t_ms"], "league": y.get("league_name") or y.get("league", ""),
                "odds": y["odds"], "p": y["p"] + pv, "p_win": y["p"], "p_void": pv, "chance_raw": y["p"] + pv, "edge": y["edge"],
                "model": y.get("model"), "sharp_src": y.get("sharp_src"), "match_id": y["match_id"], "sel": y.get("sel"),
                "fair_odds": (1 - pv) / y["p"] if y["p"] else None, "key": y["key"]}

    for leg in sorted(live, key=lambda x: (x["p"] is None, x["p"] or 0)):         # least likely first
        opts, why = [], []
        c, o = leg["p"], leg["odds"]
        ev_old = C.ev({"odds": o, "p": leg["p_win"], "p_void": leg.get("p_void", 0.0)}) if c is not None else None
        if leg["state"] in ("priced", "unpriced", "off") and leg["match_id"] in games:
            same = [y for y in games[leg["match_id"]] if not (leg.get("market") and y["market"] == leg["market"])
                    and (c is None or (C.chance(y) >= c + GAIN and C.ev(y) >= ev_old - SAFER_EV and y["odds"] >= o * SAFER_ODDS))
                    and (c is not None or y["odds"] >= o * SWAP_ODDS)]
            if same:
                y = max(same, key=lambda y: (C.chance(y), C.ev(y)))
                opts.append({"kind": "safer", "bet": as_leg(y), **after(leg, as_leg(y))})
        swaps = [y for g, ys in games.items() if g not in in_ticket and g not in taken for y in ys
                 if y["t_ms"] <= until and y["odds"] >= o * SWAP_ODDS
                 and (c is None or (C.chance(y) >= c + GAIN and C.ev(y) >= ev_old - SAFER_EV))]
        if swaps:
            y = max(swaps, key=lambda y: (C.chance(y), C.ev(y)))
            opts.append({"kind": "swap", "bet": as_leg(y), **after(leg, as_leg(y))})
        opts.append({"kind": "drop", "bet": None, **after(leg, None)})
        bad = leg.get("edge") is not None and leg["edge"] < EDGE_BAD
        if bad:
            why.append("SportyBet pays well under its chance")
        if leg["state"] == "off":
            why.append("suspended on SportyBet")
        swap = next((k for k, x in enumerate(opts) if x["kind"] == "swap"), None)
        safer = next((k for k, x in enumerate(opts) if x["kind"] == "safer"), None)
        if leg["state"] == "priced":
            fix = swap if swap is not None else safer if safer is not None else len(opts) - 1 if bad else None
        elif leg["state"] == "off":
            fix = swap if swap is not None else safer if safer is not None else len(opts) - 1
        else:
            fix = None                                         # unpriced or unknown: its risk isn't known, so it stays
        if fix is not None and opts[fix]["kind"] == "swap":
            taken.add(opts[fix]["bet"]["match_id"])
        leg.update(options=opts, fix=fix, why=why)
        leg["verdict"] = opts[fix]["kind"] if fix is not None else {"priced": "keep"}.get(leg["state"], leg["state"])
    for leg in legs:
        if leg["state"] == "started":
            leg.update(options=[], fix=None, why=[], verdict="started")
    priced = [x for x in legs if x["state"] == "priced"]
    if priced:
        min(priced, key=lambda x: x["p"])["weakest"] = True

    vetted = []
    for leg in legs:
        if leg["fix"] is None:
            vetted.append(leg)
        elif leg["options"][leg["fix"]]["bet"]:
            vetted.append({**leg["options"][leg["fix"]]["bet"], "was": leg["i"]})
    v = _ticket(vetted)
    v.update(changes=sum(1 for x in legs if x["fix"] is not None), coded=all((x.get("sel") or {}).get("event") for x in vetted),
             legs=[{k: x.get(k) for k in ("market", "home", "away", "t_ms", "odds", "p", "sel", "label", "league")} for x in vetted])
    strip = ("p_win", "chance_raw")
    return {"legs": [{k: v_ for k, v_ in x.items() if k not in strip} | {"options": [
                {**o, "bet": {k: v_ for k, v_ in o["bet"].items() if k not in strip} if o["bet"] else None} for o in x["options"]]}
                     for x in sorted(legs, key=lambda x: x["t_ms"])],
            "ticket": base, "vetted": v, "last_ms": max((x["t_ms"] for x in legs), default=None)}

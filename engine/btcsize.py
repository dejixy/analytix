"""
BTC estimate v2, after the direction test failed: how BIG the next day's move will be, and what follows the most
extreme ETF-flow days and Strategy's biggest buys. Pure functions; scripts/btcMoves.py gathers the data (the same
as the BTC estimate's) and prints both.

Every rule here was written down before the first run (4 Nov 2026). Changing one after seeing the results makes it
a new idea that needs new days.

    size      realised volatility of BTC's next SIZE_H hours from the BTC estimate's entry (14:00 UTC the day after
              each US flow day): the square root of the summed squared hourly moves, in % (about one standard
              deviation of the day's move). Forecast as its log, walk-forward, from three sets of terms:
                own past   log realised vol of the last 1, 7 and 30 days (the standard "HAR" forecast), the share of
                           the window falling on a weekend, and BTC's own last-day move (falls bring bigger moves)
                options    Deribit's DVOL (the market's own 30-day forecast), as a daily %
                flows      how unusual the day's ETF flow was (size and sign), the 5-day flow's size, treasury
                           buying, and the size of Strategy's premium change
              Pass: adding the flows lowers the out-of-sample squared error of the best model without them, t >= 2
              on the paired difference, and in both halves.
    events    ETF: a US flow day bigger than 95% of the 120 US days before it (inflow) or smaller than 95% of them
              (outflow); extreme days within EVENT_GAP US days of the last one on the same side are one event.
              Strategy: a filing whose purchase in dollars beats 80% of the 26 filings before it (about half a year;
              at least 10), since its buys were far bigger in 2024-25 than now.
              BTC's move from the entry over 1, 3, 5 and 10 days, minus the average move over all US flow days.
              Pass (pre-registered, 3 days): |t| >= 2, the same sign in both halves, and bigger than a 9 bps round trip.
"""
import math
from datetime import datetime, timezone

import numpy as np

from engine import btcmodel as M

HOUR, DAY = M.HOUR, M.DAY
SIZE_H = 24
MIN_COVER = 0.8                     # a window needs 80% of its hourly prices
OWN = ("rv1", "rv7", "rv30", "weekend", "mom")
OPTIONS = ("iv",)
FLOWS = ("etf_abs", "etf", "etf5_abs", "dat", "prem_abs")
BOOK = ("taker_abs", "book_abs")
NAMES = {"rv1": "Realised vol, last day", "rv7": "Realised vol, last 7 days", "rv30": "Realised vol, last 30 days",
         "weekend": "Weekend share of window", "mom": "BTC's own last 24 h", "iv": "Options (DVOL)",
         "etf_abs": "ETF flow size, that day", "etf": "ETF flow, that day", "etf5_abs": "ETF flow size, 5 days",
         "dat": "Treasury buying, 7 days", "prem_abs": "Strategy premium change size", "taker_abs": "Taker flow size",
         "book_abs": "Book lean size"}
HORIZONS = (24, 72, 120, 240)
PRIMARY_H = 72
EVENT_WINDOW, EVENT_MIN, EVENT_Q, EVENT_GAP = 120, 60, 0.05, 5
DAT_Q, DAT_MIN, DAT_WINDOW = 0.8, 10, 26
COST_PCT = 0.09
REFIT_EVERY = 5


class Hours:
    """BTC's hourly closes on a regular grid, for realised volatility over any span of whole hours."""

    def __init__(self, btc: list[tuple[int, float]]):
        self.t0 = btc[0][0] // HOUR * HOUR
        n = (btc[-1][0] // HOUR * HOUR - self.t0) // HOUR + 1
        c = np.full(n, np.nan)
        for t, p in btc:
            if p and p > 0:
                c[(t // HOUR * HOUR - self.t0) // HOUR] = p
        r = np.diff(np.log(c))                          # r[j]: the move ending at grid hour j + 1
        ok = ~np.isnan(r)
        self.sq = np.concatenate([[0.0], np.cumsum(np.where(ok, r * r, 0.0))])
        self.cnt = np.concatenate([[0], np.cumsum(ok)])
        self.n = len(r)

    def rv(self, t0: int, t1: int) -> float | None:
        """Realised volatility in % over (t0, t1], scaled up for missing hours; None if under 80% are there."""
        a, b = (t0 - self.t0) // HOUR, (t1 - self.t0) // HOUR
        if a < 0 or b > self.n or b <= a:
            return None
        k = int(self.cnt[b] - self.cnt[a])
        if k < MIN_COVER * (b - a):
            return None
        return math.sqrt(float(self.sq[b] - self.sq[a]) * (b - a) / k) * 100


def weekend_share(t: int, hours: int = SIZE_H) -> float:
    """The share of the hours after t that fall on a Saturday or Sunday (UTC)."""
    return sum(1 for h in range(hours)
               if datetime.fromtimestamp((t + h * HOUR) / 1000, tz=timezone.utc).weekday() >= 5) / hours


def _log(v):
    return math.log(v) if v and v > 0 else None


def size_rows(rows: list[dict], btc: list[tuple[int, float]], dvol: list[tuple[int, float]] | None = None,
              horizon_h: int = SIZE_H) -> list[dict]:
    """The BTC estimate's rows (engine/btcmodel.build_rows) with the size terms and target: x {term: value or None},
    y = log realised vol of the next `horizon_h` hours (None until it's over), rv the same in %."""
    hrs = Hours(btc)
    iv = M.Series(dvol or [])
    out = []
    for r in rows:
        t, z = r["t"], r["z"]
        rv1, rv7, rv30 = hrs.rv(t - DAY, t), hrs.rv(t - 7 * DAY, t), hrs.rv(t - 30 * DAY, t)
        nxt = hrs.rv(t, t + horizon_h * HOUR)
        dv = iv.at(t, 24 * HOUR) if dvol else None
        ab = lambda k: abs(z[k]) if z.get(k) is not None else None
        out.append({"day": r["day"], "t": t, "move": r["move"], "rv": nxt, "y": _log(nxt), "rv30": rv30 / math.sqrt(30) if rv30 else None,
                    "x": {"rv1": _log(rv1), "rv7": _log(rv7 / math.sqrt(7) if rv7 else None),
                          "rv30": _log(rv30 / math.sqrt(30) if rv30 else None), "weekend": weekend_share(t, horizon_h),
                          "mom": z.get("mom"), "iv": _log(dv / math.sqrt(365) if dv else None),
                          "etf_abs": ab("etf"), "etf": z.get("etf"), "etf5_abs": ab("etf5"), "dat": z.get("dat"),
                          "prem_abs": ab("prem"), "taker_abs": ab("taker"), "book_abs": ab("book")}})
    return out


def complete(rows: list[dict], terms) -> list[dict]:
    """Rows whose move is over and every term is known."""
    return [r for r in rows if r["y"] is not None and all(r["x"].get(k) is not None for k in terms)]


def fit(X: np.ndarray, y: np.ndarray, lam: float = 1e-3):
    """Ridge on standardised columns (a whisker of shrinkage, so collinear terms don't blow up). (mu, sd, a, beta)."""
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    beta = np.linalg.solve(Z.T @ Z + lam * len(y) * np.eye(X.shape[1]), Z.T @ (y - y.mean()))
    return mu, sd, float(y.mean()), beta


def predict(f, X: np.ndarray) -> np.ndarray:
    mu, sd, a, beta = f
    return a + ((X - mu) / sd) @ beta


def _X(rows, terms):
    return np.array([[r["x"][k] for k in terms] for r in rows], float)


def walk(rows: list[dict], terms, min_train: int, horizon_h: int = SIZE_H, refit: int = REFIT_EVERY,
         lam: float = 1e-3) -> list[dict]:
    """Walk-forward forecasts: each row from a fit on the rows whose window had finished by then. With no terms,
    the forecast is the training average (the no-skill benchmark). [{row, est}]."""
    out, f, since = [], None, refit
    for i, r in enumerate(rows):
        train = [q for q in rows[:i] if q["t"] + horizon_h * HOUR <= r["t"]]
        if len(train) < min_train:
            continue
        y = np.array([q["y"] for q in train])
        if not terms:
            out.append({"row": r, "est": float(y.mean())})
            continue
        if f is None or since >= refit:
            f, since = fit(_X(train, terms), y, lam), 0
        since += 1
        out.append({"row": r, "est": float(predict(f, _X([r], terms))[0])})
    return out


def size_scores(preds: list[dict], naive: list[dict], base: list[dict] | None = None) -> dict:
    """How a size forecast did: correlation with what happened, R² against the training average, and (with `base`,
    the same rows forecast another way) the paired squared-error gain with its t. Every list is in the same rows."""
    if len(preds) < 20:
        return {"n": len(preds)}
    y = np.array([p["row"]["y"] for p in preds])
    e = np.array([p["est"] for p in preds])
    nv = np.array([p["est"] for p in naive])
    out = {"n": len(preds), "corr": float(np.corrcoef(e, y)[0, 1]) if e.std() > 0 else 0.0,
           "r2": float(1 - ((y - e) ** 2).sum() / ((y - nv) ** 2).sum()), "mse": float(((y - e) ** 2).mean())}
    if base is not None:
        b = np.array([p["est"] for p in base])
        d = (y - b) ** 2 - (y - e) ** 2
        out["gain"] = float(d.mean())
        out["t_gain"] = float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))) if d.std() > 0 else 0.0
    return out


def size_halves(preds, naive, base=None) -> tuple[dict, dict]:
    m = len(preds) // 2
    return (size_scores(preds[:m], naive[:m], base[:m] if base else None),
            size_scores(preds[m:], naive[m:], base[m:] if base else None))


def quintiles(preds: list[dict]) -> list[dict]:
    """Rows sorted into fifths by the forecast: average forecast, average realised vol and average |move| (both %)."""
    if len(preds) < 25:
        return []
    order = sorted(preds, key=lambda p: p["est"])
    out = []
    for k in range(5):
        part = order[k * len(order) // 5:(k + 1) * len(order) // 5]
        out.append({"n": len(part), "forecast": float(np.mean([math.exp(p["est"]) for p in part])),
                    "rv": float(np.mean([p["row"]["rv"] for p in part])),
                    "move": float(np.mean([abs(p["row"]["move"]) for p in part if p["row"]["move"] is not None]))})
    return out


def coverage(preds: list[dict]) -> float | None:
    """The share of days BTC's move stayed within ± the forecast (about 68% if the forecast is a fair 1-sd)."""
    got = [abs(p["row"]["move"]) <= math.exp(p["est"]) for p in preds if p["row"]["move"] is not None]
    return float(np.mean(got)) if got else None


def size_now(rows: list[dict], terms, lam: float = 1e-3) -> dict | None:
    """The forecast for the newest row: fit on every finished row; the forecast in %, its 30-day normal, where it sits
    among the last year's forecasts, and each term's push (in log points)."""
    done = complete(rows, terms)
    last = next((r for r in reversed(rows) if all(r["x"].get(k) is not None for k in terms)), None)
    if len(done) < 60 or last is None:
        return None
    f = fit(_X(done, terms), np.array([r["y"] for r in done]), lam)
    est = float(predict(f, _X([last], terms))[0])
    past = predict(f, _X(done[-250:], terms))
    mu, sd, _a, beta = f
    zx = (_X([last], terms)[0] - mu) / sd
    return {"day": last["day"], "t": last["t"], "est": math.exp(est), "normal": last.get("rv30"),
            "rank": float(np.mean(past <= est)), "parts": {k: float(b * v) for k, b, v in zip(terms, beta, zx)},
            "n": len(done), "finished": last["y"] is not None, "actual": last["rv"]}


# ── events ───────────────────────────────────────────────────────────────────
def flow_events(rows: list[dict], window: int = EVENT_WINDOW, min_n: int = EVENT_MIN, q: float = EVENT_Q,
                gap: int = EVENT_GAP) -> list[dict]:
    """Extreme ETF-flow days: [{row, side (+1 inflow, -1 outflow), pct (share of the window below or above)}]."""
    flows = [r["raw"]["etf"] for r in rows]
    last = {1: -10 ** 9, -1: -10 ** 9}
    out = []
    for i, r in enumerate(rows):
        x = flows[i]
        past = [v for v in flows[max(0, i - window):i] if v is not None]
        if x is None or len(past) < min_n:
            continue
        below = sum(1 for v in past if v < x) / len(past)
        above = sum(1 for v in past if v > x) / len(past)
        side = 1 if below >= 1 - q else -1 if above >= 1 - q else 0
        if not side:
            continue
        if i - last[side] > gap:
            out.append({"row": r, "side": side, "pct": below if side == 1 else above, "flow": x})
        last[side] = i                                  # a run of extreme days stays one event
    return out


def dat_events(dat: list[dict], btc: list[tuple[int, float]], q: float = DAT_Q, min_n: int = DAT_MIN,
               window: int = DAT_WINDOW, entry_h: int = M.ENTRY_H) -> list[dict]:
    """Strategy's biggest buys: filings whose dollar purchase beats `q` of the `window` buying filings before it.
    Entry at `entry_h` UTC on the filing day (8-Ks come out before the US open). [{t, day, usd, side=+1}]."""
    px = M.Series(btc)
    buys = []
    for x in sorted(dat, key=lambda x: x["filed"]):
        t = M.ms_of(x["filed"], entry_h)
        p = px.at(t)
        if p and x.get("bought"):
            buys.append((t, x["filed"], x["bought"] * p))
    out = []
    for i, (t, day, usd) in enumerate(buys):
        past = [u for _, _, u in buys[max(0, i - window):i]]
        if len(past) >= min_n and sum(1 for u in past if u < usd) / len(past) >= q:
            out.append({"t": t, "day": day, "usd": usd, "side": 1})
    return out


def forward(t: int, px: M.Series, horizons=HORIZONS) -> dict:
    """BTC's move in % from t over each horizon (hours); a horizon not yet over is left out."""
    p0 = px.at(t)
    out = {}
    for h in horizons:
        p1 = px.at(t + h * HOUR, 2 * HOUR)
        if p0 and p1:
            out[h] = math.log(p1 / p0) * 100
    return out


def baseline(rows: list[dict], px: M.Series, horizons=HORIZONS) -> dict:
    """Over every US flow day: the average move and the average size of the move, per horizon."""
    fw = [forward(r["t"], px, horizons) for r in rows]
    return {h: {"mean": float(np.mean(v)), "abs": float(np.mean(np.abs(v))), "n": len(v)}
            for h in horizons if (v := [f[h] for f in fw if h in f])}


def _t(x):
    return float(np.mean(x) / (np.std(x, ddof=1) / math.sqrt(len(x)))) if len(x) > 2 and np.std(x) > 0 else 0.0


def event_test(times: list[int], px: M.Series, base: dict, horizons=HORIZONS) -> dict:
    """For events at `times`: per horizon the count, the average move above the all-days average, its t, the share
    above it, each half's average, and the size of the moves against an ordinary day's."""
    fw = [forward(t, px, horizons) for t in times]
    out = {}
    for h in horizons:
        if h not in base:
            continue
        ex = [f[h] - base[h]["mean"] for f in fw if h in f]
        raw = [f[h] for f in fw if h in f]
        if len(ex) < 4:
            out[h] = {"n": len(ex)}
            continue
        m = len(ex) // 2
        out[h] = {"n": len(ex), "excess": float(np.mean(ex)), "t": _t(ex), "above": float(np.mean(np.array(ex) > 0)),
                  "first": float(np.mean(ex[:m])), "second": float(np.mean(ex[m:])),
                  "size": float(np.mean(np.abs(raw)) / base[h]["abs"]) if base[h]["abs"] else None}
    return out


def passes(res: dict, h: int = PRIMARY_H) -> bool:
    s = res.get(h, {})
    if s.get("n", 0) < 8:
        return False
    same = np.sign(s["first"]) == np.sign(s["excess"]) == np.sign(s["second"])
    return abs(s["t"]) >= 2 and bool(same) and abs(s["excess"]) > COST_PCT

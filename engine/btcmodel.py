"""
The BTC estimate: one equation for BTC's move over the next day, from slow money (US spot ETF flows, treasury-company
buying, Strategy's share price against BTC) and the order book (taker flow and book imbalance from our recordings),
fitted walk-forward and checked on days it never saw. Pure functions; scripts/btcEstimate.py gathers the data.

    timing      a US flow day D is published that evening or the next morning, so every row is measured at
                ENTRY_H UTC on D + 1 (before the next US open) and the move is from there, HORIZON_H hours on
    terms       each a z-score against the 60 rows before it (no look-ahead), so a coefficient reads as "% of move
                per unusual day":
                  etf     that day's net ETF flow
                  etf5    the last 5 days' net flow
                  dat     BTC bought by treasury companies, announced in the 7 days before (in dollars)
                  prem    the 5-day change of Strategy's share price against BTC (its premium falling or rising)
                  mom     BTC's own move over the 24 hours before (ETF flows often just follow price; this keeps
                          them honest)
                  taker   net taker dollars on Hyperliquid over the 24 hours before   (from our recordings, so only
                  book    the bid/ask depth lean within 25 bps, averaged               since 9 Oct 2026; 0 before)
    fit         ridge regression (shrinks every coefficient toward zero; more so with less data), refitted as days
                pass using only rows whose move had finished before the day being estimated
    held back   the last HOLD_FRAC of rows are never used while tuning; --final shows them once, at the end
"""
import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone

import numpy as np

HOUR = 3_600_000
DAY = 24 * HOUR
ENTRY_H = 14                # UTC hour the next day's row is measured at
HORIZON_H = 24
Z_WINDOW = 60
Z_MIN = 20
TERMS = ("etf", "etf5", "dat", "prem", "mom", "taker", "book")
NAMES = {"etf": "ETF flow, that day", "etf5": "ETF flow, 5 days", "dat": "Treasury buying, 7 days",
         "prem": "Strategy vs BTC, 5 days", "mom": "BTC's own last 24 h", "taker": "Taker flow, 24 h",
         "book": "Book lean, 24 h"}
HOLD_FRAC = 0.2
MIN_TRAIN = 120
REFIT_EVERY = 5


def ms_of(day: str, hour: int = 0) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class Series:
    """A sorted (time ms, value) series with "last value at or before t"."""

    def __init__(self, rows):
        rows = sorted(rows)
        self.t = [r[0] for r in rows]
        self.v = [r[1] for r in rows]

    def at(self, t: int, max_age: int = 6 * HOUR):
        i = bisect_right(self.t, t) - 1
        if i < 0 or t - self.t[i] > max_age:
            return None
        return self.v[i]

    def window(self, t0: int, t1: int) -> list:
        return self.v[bisect_right(self.t, t0):bisect_right(self.t, t1)]


def zscore(raw: list[float | None], window: int = Z_WINDOW, min_n: int = Z_MIN) -> list[float | None]:
    """Each value against the `window` values before it (not itself): (x − mean) / sd; None until min_n."""
    out = []
    for i, x in enumerate(raw):
        past = [v for v in raw[max(0, i - window):i] if v is not None]
        if x is None or len(past) < min_n:
            out.append(None)
            continue
        m = sum(past) / len(past)
        sd = math.sqrt(sum((v - m) ** 2 for v in past) / (len(past) - 1)) or None
        out.append((x - m) / sd if sd else 0.0)
    return out


def build_rows(etf: list[dict], dat: list[dict], btc: list[tuple[int, float]], mstr: list[tuple[int, float]],
               book: dict[int, dict] | None = None, horizon_h: int = HORIZON_H, entry_h: int = ENTRY_H) -> list[dict]:
    """One row per ETF flow day: when it's measured, the raw terms, their z-scores and the move that followed
    (None while it hasn't happened yet)."""
    px = Series(btc)
    ms = Series(mstr)
    flows = [(x["date"], x["total"]) for x in sorted(etf, key=lambda x: x["date"])]
    buys = sorted((ms_of(x["filed"], 13) + 30 * 60_000, x["bought"]) for x in dat)   # 8-Ks are filed before 9:30 ET
    btimes = [b[0] for b in buys]
    hours = sorted(book or {})
    rows = []
    for i, (day, flow) in enumerate(flows):
        t = ms_of(day, entry_h) + DAY
        p0 = px.at(t)
        if p0 is None:
            continue
        p1 = px.at(t + horizon_h * HOUR, max_age=2 * HOUR)
        p_back = px.at(t - DAY)
        bought = sum(b for tb, b in buys[bisect_left(btimes, t - 7 * DAY + 1):bisect_right(btimes, t)])
        m_now = ms.at(t, max_age=4 * DAY)
        m_then = ms.at(t - 7 * DAY, max_age=4 * DAY)             # about 5 trading days
        b_now, b_then = px.at(t - 18 * HOUR, 12 * HOUR), px.at(t - 7 * DAY - 18 * HOUR, 12 * HOUR)
        prem = (math.log(m_now / b_now) - math.log(m_then / b_then)) if m_now and m_then and b_now and b_then else None
        lo = bisect_left(hours, t - DAY)
        hi = bisect_left(hours, t)
        window = [book[h] for h in hours[lo:hi]]
        rows.append({
            "day": day, "t": t,
            "raw": {"etf": flow, "etf5": sum(f for _, f in flows[max(0, i - 4):i + 1]),
                    "dat": bought * p0 / 1e6, "prem": prem,
                    "mom": math.log(p0 / p_back) * 100 if p_back else None,
                    "taker": sum(w["taker"] for w in window) / 1e6 if len(window) >= 18 else None,
                    "book": sum(w["imb"] for w in window) / len(window) if len(window) >= 18 else None},
            "move": math.log(p1 / p0) * 100 if p1 else None, "p0": p0})
    for k in TERMS:
        for r, z in zip(rows, zscore([r["raw"][k] for r in rows], min_n=Z_MIN if k not in ("taker", "book") else 10)):
            r.setdefault("z", {})[k] = z
    return rows


def _x(rows, terms):
    return np.array([[r["z"].get(k) if r["z"].get(k) is not None else 0.0 for k in terms] for r in rows], float)


def ridge(X: np.ndarray, y: np.ndarray, lam: float) -> tuple[float, np.ndarray]:
    """Intercept (not shrunk) and coefficients."""
    xm, ym = X.mean(0), y.mean()
    Xc, yc = X - xm, y - ym
    beta = np.linalg.solve(Xc.T @ Xc + lam * len(y) * np.eye(X.shape[1]), Xc.T @ yc)
    return float(ym - xm @ beta), beta


def usable(rows: list[dict], terms) -> list[dict]:
    """Rows with a finished move and every non-recording term known (the recording terms count as 0 before 9 Oct)."""
    need = [k for k in terms if k not in ("taker", "book")]
    return [r for r in rows if r["move"] is not None and all(r["z"].get(k) is not None for k in need)]


def walk_forward(rows: list[dict], terms=TERMS, lam: float = 0.1, min_train: int = MIN_TRAIN,
                 refit: int = REFIT_EVERY, horizon_h: int = HORIZON_H) -> list[dict]:
    """Estimates for each row from a fit on the rows whose move had finished by then. [{row, est}]."""
    out, fit, since = [], None, refit
    for i, r in enumerate(rows):
        train = [q for q in rows[:i] if q["t"] + horizon_h * HOUR <= r["t"]]
        if len(train) < min_train:
            continue
        if fit is None or since >= refit:
            fit = ridge(_x(train, terms), np.array([q["move"] for q in train]), lam)
            since = 0
        since += 1
        a, b = fit
        out.append({"row": r, "est": a + float(_x([r], terms)[0] @ b)})
    return out


def scores(preds: list[dict]) -> dict:
    """How the estimates did: correlation with what happened, the share with the right sign, R² against always
    saying zero, and the average move in the estimate's direction (bps)."""
    if len(preds) < 10:
        return {"n": len(preds)}
    e = np.array([p["est"] for p in preds])
    y = np.array([p["row"]["move"] for p in preds])
    ic = float(np.corrcoef(e, y)[0, 1]) if e.std() > 0 and y.std() > 0 else 0.0
    return {"n": len(preds), "ic": ic, "t_ic": ic * math.sqrt(max(len(preds) - 2, 1)) / math.sqrt(max(1e-12, 1 - ic * ic)),
            "hit": float(np.mean(np.sign(e) == np.sign(y))), "r2": float(1 - ((y - e) ** 2).sum() / (y ** 2).sum()),
            "follow_bps": float(np.mean(np.sign(e) * y) * 100), "resid_sd": float(np.std(y - e, ddof=1))}


def halves(preds: list[dict]) -> tuple[dict, dict]:
    m = len(preds) // 2
    return scores(preds[:m]), scores(preds[m:])


def split(rows: list[dict], final: bool = False, frac: float = HOLD_FRAC) -> tuple[list[dict], list[dict]]:
    """(rows to tune on, rows held back). With final, the held-back rows are estimated too (from fits that only saw
    earlier rows), but they are only ever looked at once."""
    cut = int(len(rows) * (1 - frac))
    return (rows, rows[cut:]) if final else (rows[:cut], [])


def univariate(rows: list[dict]) -> dict:
    """Each term's correlation with the move that followed, on every usable row (a first look, not a test)."""
    out = {}
    for k in TERMS:
        pairs = [(r["z"][k], r["move"]) for r in rows if r["z"].get(k) is not None and r["move"] is not None]
        if len(pairs) >= 15:
            a = np.array(pairs)
            out[k] = {"n": len(pairs), "r": float(np.corrcoef(a[:, 0], a[:, 1])[0, 1])}
    return out


def reactive(etf: list[dict], btc: list[tuple[int, float]]) -> dict:
    """Do ETF flows follow BTC? Correlation of a day's flow with BTC's move that US day (14:30 to 21:00 UTC), the day
    before, and the next day."""
    px = Series(btc)
    out = {}
    for name, (a, b) in {"day before": (-DAY - 10 * HOUR, -DAY + 7 * HOUR), "same day": (-10 * HOUR, 7 * HOUR),
                         "next day": (DAY - 10 * HOUR, DAY + 7 * HOUR)}.items():
        pairs = []
        for x in etf:
            t = ms_of(x["date"], 0) + 24 * HOUR - 3 * HOUR        # 21:00 UTC that day
            p0, p1 = px.at(t + a, 2 * HOUR), px.at(t + b, 2 * HOUR)
            if p0 and p1:
                pairs.append((x["total"], math.log(p1 / p0)))
        if len(pairs) > 20:
            arr = np.array(pairs)
            out[name] = float(np.corrcoef(arr[:, 0], arr[:, 1])[0, 1])
    return out


def equation(a: float, b: np.ndarray, terms) -> str:
    parts = [f"{a:+.3f}"] + [f"{c:+.3f}·{k}" for k, c in zip(terms, b)]
    return f"move next {HORIZON_H}h (%) = " + " ".join(parts)


def estimate_now(rows: list[dict], terms=TERMS, lam: float = 0.1, resid_sd: float | None = None) -> dict | None:
    """The live estimate: fit on every finished row, apply it to the newest row (measured at its time), with each
    term's part, a 68% band from the out-of-sample misses, and the chance the move is up."""
    done = usable(rows, terms)
    if len(done) < 30 or not rows:
        return None
    a, b = ridge(_x(done, terms), np.array([r["move"] for r in done]), lam)
    last = rows[-1]
    x = _x([last], terms)[0]
    est = a + float(x @ b)
    sd = resid_sd or float(np.std(np.array([r["move"] for r in done]) - (a + _x(done, terms) @ b), ddof=1))
    up = 0.5 * (1 + math.erf(est / (sd * math.sqrt(2)))) if sd else 0.5
    return {"day": last["day"], "t": last["t"], "est": est, "band": sd, "p_up": up, "intercept": a,
            "coef": dict(zip(terms, map(float, b))), "parts": {k: float(c * v) for k, c, v in zip(terms, b, x)},
            "z": {k: last["z"].get(k) for k in terms}, "raw": last["raw"], "equation": equation(a, b, terms), "n": len(done)}

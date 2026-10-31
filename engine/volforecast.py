"""
How big will the next 30 minutes be? A forecast of the move's size per coin (not its direction), from how busy the
market has been, how far it has moved, the hour of day and scheduled releases. It's the "trade only when the move
can beat the cost" filter: fees are fixed, so a signal is only worth taking when the market is moving enough.

    minutes     the last print each minute (carried forward) and the taker orders that minute (fills merged by
                block, taker and side: one sweep through five resting orders is one order)
    inputs      at the end of each minute: average move per minute and orders per minute over the last
                5, 30, 120 and 1,440 minutes, the hour's usual move size (from the fitting days only), and whether
                a High-impact US release is due in the next 30 minutes (engine/calendar.py)
    target      |move| over the next 30 minutes, bps
    model       least squares on logs, fitted only on minutes before the split; scaled so its average matches the
                fitting days. Regimes: quiet / middle / loud at the fitting days' thirds
    at(t)       forecast and regime from the last minute that had fully ended before t: no look-ahead
"""
import math
from dataclasses import dataclass

import numpy as np

from engine.calendar import upcoming
from storage import tickStore

MINUTE = 60_000
HORIZON = 30
LOOKBACKS = (5, 30, 120, 1440)
STALE_MIN = 5
REGIMES = ("quiet", "middle", "loud")


@dataclass
class Forecast:
    coin: str
    m0: int                       # first minute (ms // MINUTE)
    pred: np.ndarray              # forecast |move| over the next 30 min, bps, per minute (nan where unknown)
    cuts: tuple[float, float]     # the fitting days' thirds
    quality: dict                 # how it did on the minutes after the split
    price: np.ndarray | None = None   # the minute's last print (carried forward), for studies built on top
    ok: np.ndarray | None = None      # a real print within STALE_MIN minutes

    def at(self, t_ms: float) -> tuple[float, str | None]:
        i = int(t_ms // MINUTE) - 1 - self.m0
        if i < 0 or i >= len(self.pred) or not np.isfinite(self.pred[i]):
            return math.nan, None
        p = float(self.pred[i])
        return p, REGIMES[int(p > self.cuts[0]) + int(p > self.cuts[1])]


def minutes(coin: str, start_ms: float, end_ms: float, store=tickStore.STORE_DIR) -> dict:
    """Per-minute price and taker-order count for one coin."""
    z = tickStore.load(coin, ["trades"], start_ms, end_ms, store,
                       only=["trade_px", "trade_side", "trade_buyer", "trade_seller"])
    t = z["trade_t"]
    if not len(t):
        return {}
    mi = t // MINUTE
    m0 = int(mi[0])
    n = int(mi[-1]) - m0 + 1
    idx = mi - m0
    last = np.full(n, np.nan)
    last[idx] = z["trade_px"]                                   # time order: the minute's last print wins
    seen = np.where(np.isfinite(last), np.arange(n), -1)
    np.maximum.accumulate(seen, out=seen)
    ok = (seen >= 0) & (np.arange(n) - seen <= STALE_MIN)
    price = np.where(seen >= 0, last[np.maximum(seen, 0)], np.nan)
    side = z["trade_side"].astype(np.int64)
    taker = np.where(side > 0, z["trade_buyer"], z["trade_seller"]).astype(np.int64)
    order = np.lexsort((taker, side, t))
    ts, ss, ks = t[order], side[order], taker[order]
    new = np.r_[True, (np.diff(ts) != 0) | (np.diff(ss) != 0) | (np.diff(ks) != 0)]
    orders = np.bincount((ts[new] // MINUTE - m0).astype(np.int64), minlength=n).astype(float)
    return {"m0": m0, "price": price, "ok": ok, "orders": orders}


def _avg(x: np.ndarray, k: int) -> np.ndarray:
    c = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    out[k - 1:] = (c[k:] - c[:-k]) / k
    return out


def fit(coin: str, start_ms: float, split_ms: float, end_ms: float, store=tickStore.STORE_DIR) -> Forecast | None:
    g = minutes(coin, start_ms, end_ms, store)
    if not g:
        return None
    m0, price, ok, orders = g["m0"], g["price"], g["ok"], g["orders"]
    n = len(price)
    lp = np.log(price)
    step = np.abs(np.r_[np.nan, np.diff(lp)]) * 1e4
    step[~ok] = np.nan
    stepz = np.nan_to_num(step)
    y = np.full(n, np.nan)
    y[:-HORIZON] = np.abs(lp[HORIZON:] - lp[:-HORIZON]) * 1e4
    y[~ok | ~np.r_[ok[HORIZON:], np.zeros(HORIZON, bool)]] = np.nan
    t_end = (np.arange(n) + m0 + 1) * MINUTE                    # the moment each minute ends
    hour = (t_end // 3_600_000) % 24
    train = (t_end <= split_ms - HORIZON * MINUTE) & np.isfinite(y)
    hour_mean = np.array([np.nanmean(y[train & (hour == h)]) if (train & (hour == h)).any() else np.nan
                          for h in range(24)])
    cols = [np.ones(n)]
    for k in LOOKBACKS:
        cols.append(np.log1p(_avg(stepz, k)))
        cols.append(np.log1p(_avg(orders, k)))
    cols.append(np.log1p(hour_mean[hour]))
    cols.append(upcoming(t_end, HORIZON * MINUTE).astype(float))
    X = np.column_stack(cols)
    good = np.all(np.isfinite(X), axis=1)
    fit_rows = train & good
    if fit_rows.sum() < 1000:
        return None
    beta = np.linalg.lstsq(X[fit_rows], np.log1p(y[fit_rows]), rcond=None)[0]
    pred = np.full(n, np.nan)
    pred[good] = np.expm1(X[good] @ beta)
    pred *= np.nanmean(y[fit_rows]) / np.nanmean(pred[fit_rows])  # same average as the fitting days
    cuts = tuple(np.nanpercentile(pred[fit_rows], [100 / 3, 200 / 3]))
    test = (t_end > split_ms) & np.isfinite(y) & good
    naive = np.full(n, np.nan)
    naive[HORIZON:] = np.abs(lp[HORIZON:] - lp[:-HORIZON]) * 1e4  # "same as the last 30 minutes"
    quality = {"test_minutes": int(test.sum())}
    if test.sum() > 100:
        ly = np.log1p(y[test])
        quality["corr"] = float(np.corrcoef(np.log1p(pred[test]), ly)[0, 1])
        nv = test & np.isfinite(naive)
        quality["naive_corr"] = float(np.corrcoef(np.log1p(naive[nv]), np.log1p(y[nv]))[0, 1])
        reg = (pred[test] > cuts[0]).astype(int) + (pred[test] > cuts[1]).astype(int)
        quality["by_regime"] = {REGIMES[r]: (float(np.mean(y[test][reg == r])) if (reg == r).any() else None,
                                             float((reg == r).mean())) for r in range(3)}
    return Forecast(coin, m0, pred, cuts, quality, price, ok)

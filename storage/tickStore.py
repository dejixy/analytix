"""
Fast columns for studies: the recordings, one compressed numpy file per coin per day.

Studies used to re-read weeks of gzipped JSON every run (minutes). The store is built once (scripts/buildStore.py)
and loads in seconds. Layout:

    <STORE_DIR>/<coin>/<YYYYMMDD>.npz        one UTC day (by the recording's hour), every column below

    trades   trade_t, trade_recv (ms), trade_px, trade_sz, trade_side (+1 the taker bought, -1 sold), trade_tid,
             trade_engine (fills the exchange's engine made itself: all-zero hash), trade_buyer, trade_seller
             (indices into `wallets`, -1 when missing)
    book     book_t, book_recv, bid_px, bid_sz, bid_n, ask_px, ask_sz, ask_n   (rows x BOOK_LEVELS; NaN / 0 padded)
    bbo      bbo_t, bbo_recv, bbo_bid, bbo_bid_sz, bbo_bid_n, bbo_ask, bbo_ask_sz, bbo_ask_n: the best bid and offer,
             sent whenever the top changes (~every 0.1 s; recorded from 28 Oct 2026, empty before)
    ext      ext_t (the other exchange's own time), ext_recv, ext_venue (VENUES index), ext_bid, ext_ask
    xtrade   another exchange's trades (Lighter, from 29 Oct 2026): xtrade_t, xtrade_recv, xtrade_venue, xtrade_px,
             xtrade_sz, xtrade_side (+1 the taker bought), xtrade_liq
    ctx      ctx_recv, ctx_funding, ctx_oi, ctx_mark, ctx_oracle, ctx_mid, ctx_premium, ctx_volume
    wallets  that day's addresses (lowercase); `sources` the hourly files it was built from (name:size)

Times are exchange milliseconds except *_recv (this server's clock on arrival) and ctx, which has no exchange time.
"""
import calendar
import gzip
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np

from config import STORE_DIR

BOOK_LEVELS = 20
VENUES = ("binance", "bybit", "lighter")                     # append only: the index is what's stored
KINDS = {
    "trades": ("trade_t", "trade_recv", "trade_px", "trade_sz", "trade_side", "trade_tid", "trade_engine",
               "trade_buyer", "trade_seller"),
    "book": ("book_t", "book_recv", "bid_px", "bid_sz", "bid_n", "ask_px", "ask_sz", "ask_n"),
    "bbo": ("bbo_t", "bbo_recv", "bbo_bid", "bbo_bid_sz", "bbo_bid_n", "bbo_ask", "bbo_ask_sz", "bbo_ask_n"),
    "ext": ("ext_t", "ext_recv", "ext_venue", "ext_bid", "ext_ask"),
    "xtrade": ("xtrade_t", "xtrade_recv", "xtrade_venue", "xtrade_px", "xtrade_sz", "xtrade_side", "xtrade_liq"),
    "ctx": ("ctx_recv", "ctx_funding", "ctx_oi", "ctx_mark", "ctx_oracle", "ctx_mid", "ctx_premium", "ctx_volume"),
}
TIME_KEY = {"trades": "trade_t", "book": "book_t", "bbo": "bbo_t", "ext": "ext_t", "xtrade": "xtrade_t",
            "ctx": "ctx_recv"}
HOUR_RE = re.compile(r"_(\d{8})_(\d{2})00(?:_\d+)?\.jsonl\.gz$")


# ----------------------------------------------------------------------------------------------- building

def day_of(path: Path) -> str | None:
    """The UTC day of a finished hourly recording (eth-btc-sol-hype_20261027_1300.jsonl.gz -> 20261027)."""
    m = HOUR_RE.search(path.name)
    return m.group(1) if m else None


def hours_by_day(folders: Iterable[str | Path]) -> dict[str, list[Path]]:
    """Finished (gzipped) hourly recordings grouped by day. The hour still being written (.jsonl) is left out."""
    days: dict[str, list[Path]] = defaultdict(list)
    for folder in map(Path, folders):
        for p in sorted(folder.glob("*.jsonl.gz")) if folder.is_dir() else [folder]:
            d = day_of(p)
            if d:
                days[d].append(p)
    return dict(days)


def sources_of(paths: list[Path]) -> list[str]:
    return sorted(f"{p.name}:{p.stat().st_size}" for p in paths)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


class _Coin:
    """Rows for one coin while a day is being read."""

    def __init__(self):
        self.trades: list[tuple] = []
        self.book_t: list[int] = []
        self.book_recv: list[int] = []
        self.levels: list[tuple] = []
        self.bbo: list[tuple] = []
        self.ext: list[tuple] = []
        self.xtrade: list[tuple] = []
        self.ctx: list[tuple] = []


def _wallet_id(addr, table: dict[str, int]) -> int:
    if not isinstance(addr, str) or not addr:
        return -1
    addr = addr.lower()
    i = table.get(addr)
    if i is None:
        i = table[addr] = len(table)
    return i


def _side(levels: list, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    px = np.full(n, np.nan)
    sz = np.zeros(n)
    cnt = np.zeros(n, dtype=np.int32)
    for i, lv in enumerate(levels[:n]):
        px[i], sz[i], cnt[i] = _f(lv.get("px")), _f(lv.get("sz")), int(lv.get("n") or 0)
    return px, sz, cnt


def read_day(paths: list[Path]) -> dict[str, dict[str, np.ndarray]]:
    """Every coin's columns from one day's hourly recordings."""
    coins: dict[str, _Coin] = defaultdict(_Coin)
    wallets: dict[str, dict[str, int]] = defaultdict(dict)
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    recv, msg = int(rec["recv_ms"]), rec["msg"]
                    ch, d = msg.get("channel"), msg.get("data")
                    if ch == "trades" and d:
                        for x in d:
                            c = x["coin"]
                            users = x.get("users") or [None, None]
                            table = wallets[c]
                            h = str(x.get("hash", ""))
                            coins[c].trades.append((
                                int(x["time"]), recv, _f(x["px"]), _f(x["sz"]), 1 if x["side"] == "B" else -1,
                                int(x.get("tid", 0)), bool(h) and set(h[2:] if h.startswith("0x") else h) <= {"0"},
                                _wallet_id(users[0], table), _wallet_id(users[1] if len(users) > 1 else None, table)))
                    elif ch == "l2Book" and d:
                        bids, asks = d["levels"]
                        c = coins[d["coin"]]
                        c.book_t.append(int(d["time"]))
                        c.book_recv.append(recv)
                        c.levels.append((*_side(bids, BOOK_LEVELS), *_side(asks, BOOK_LEVELS)))
                    elif ch == "bbo" and d:
                        bid, ask = (d.get("bbo") or [None, None])[:2]
                        bid, ask = bid or {}, ask or {}                      # a side with no orders is null
                        coins[d["coin"]].bbo.append((
                            int(d["time"]), recv, _f(bid.get("px")), _f(bid.get("sz", 0)), int(bid.get("n") or 0),
                            _f(ask.get("px")), _f(ask.get("sz", 0)), int(ask.get("n") or 0)))
                    elif ch == "ext" and d:
                        coins[d["coin"]].ext.append((int(d["ts"]), recv, VENUES.index(d["venue"]),
                                                     _f(d["bid"]), _f(d["ask"])))
                    elif ch == "ext_trade" and d:
                        coins[d["coin"]].xtrade.append((int(d["ts"]), recv, VENUES.index(d["venue"]), _f(d["px"]),
                                                        _f(d["sz"]), 1 if d.get("side", 0) > 0 else -1,
                                                        bool(d.get("liq"))))
                    elif ch == "activeAssetCtx" and d and "ctx" in d:
                        x = d["ctx"]
                        coins[d["coin"]].ctx.append((recv, _f(x.get("funding")), _f(x.get("openInterest")),
                                                     _f(x.get("markPx")), _f(x.get("oraclePx")), _f(x.get("midPx")),
                                                     _f(x.get("premium")), _f(x.get("dayNtlVlm"))))
                except (KeyError, ValueError, TypeError, IndexError, json.JSONDecodeError):
                    continue
    out = {}
    for name, c in coins.items():
        cols: dict[str, np.ndarray] = {}
        t = np.array(c.trades, dtype=object).reshape(-1, 9)
        for i, (k, dt) in enumerate(zip(KINDS["trades"], ("i8", "i8", "f8", "f8", "i1", "i8", "?", "i4", "i4"))):
            cols[k] = t[:, i].astype(dt)
        cols["book_t"] = np.array(c.book_t, dtype="i8")
        cols["book_recv"] = np.array(c.book_recv, dtype="i8")
        for i, k in enumerate(KINDS["book"][2:]):
            dt = "i4" if k.endswith("_n") else "f8"
            cols[k] = (np.stack([lv[i] for lv in c.levels]).astype(dt) if c.levels
                       else np.zeros((0, BOOK_LEVELS), dtype=dt))
        b = np.array(c.bbo, dtype=object).reshape(-1, 8)
        for i, (k, dt) in enumerate(zip(KINDS["bbo"], ("i8", "i8", "f8", "f8", "i4", "f8", "f8", "i4"))):
            cols[k] = b[:, i].astype(dt)
        e = np.array(c.ext, dtype=object).reshape(-1, 5)
        for i, (k, dt) in enumerate(zip(KINDS["ext"], ("i8", "i8", "i1", "f8", "f8"))):
            cols[k] = e[:, i].astype(dt)
        xt = np.array(c.xtrade, dtype=object).reshape(-1, 7)
        for i, (k, dt) in enumerate(zip(KINDS["xtrade"], ("i8", "i8", "i1", "f8", "f8", "i1", "?"))):
            cols[k] = xt[:, i].astype(dt)
        x = np.array(c.ctx, dtype=float).reshape(-1, 8)
        for i, k in enumerate(KINDS["ctx"]):
            cols[k] = x[:, i].astype("i8") if k == "ctx_recv" else x[:, i]
        for kind, keys in KINDS.items():                     # each kind in time order
            order = np.argsort(cols[TIME_KEY[kind]], kind="stable")
            for k in keys:
                cols[k] = cols[k][order]
        table = wallets[name]
        cols["wallets"] = np.array(sorted(table, key=table.get), dtype="<U42")
        out[name] = cols
    return out


def write_day(store: Path, day: str, paths: list[Path], coins: Iterable[str] | None = None) -> list[str]:
    """Build one day for every coin in it (or just `coins`). Returns the coins written."""
    data = read_day(paths)
    src = np.array(sources_of(paths))
    written = []
    for coin, cols in data.items():
        if coins is not None and coin not in coins:
            continue
        folder = store / coin
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".{day}.tmp.npz"
        np.savez_compressed(tmp, sources=src, **cols)
        tmp.replace(folder / f"{day}.npz")                   # never leave a half-written day behind
        written.append(coin)
    return written


def built_sources(store: Path, day: str) -> list[str] | None:
    """The source list a built day was made from (any coin's file; they're written together)."""
    for f in sorted(store.glob(f"*/{day}.npz")):
        try:
            with np.load(f) as z:
                return sorted(z["sources"].tolist())
        except Exception:
            return None
    return None


# ----------------------------------------------------------------------------------------------- loading

def days(coin: str, store: Path = STORE_DIR) -> list[str]:
    return sorted(p.stem for p in (Path(store) / coin).glob("*.npz") if not p.name.startswith("."))


def coins(store: Path = STORE_DIR) -> list[str]:
    return sorted(p.name for p in Path(store).iterdir() if p.is_dir()) if Path(store).is_dir() else []


def load(coin: str, kinds: Iterable[str] = ("book",), start_ms: float | None = None, end_ms: float | None = None,
         store: Path = STORE_DIR, only: Iterable[str] | None = None) -> dict[str, np.ndarray]:
    """One coin's columns for `kinds` across days, in time order, within [start_ms, end_ms).

    `only` limits the columns read (each kind's time column always comes too), which keeps memory down for long
    ranges. Wallet indices from different days are remapped onto one combined `wallets` table."""
    kinds = list(kinds)
    want = [k for kind in kinds for k in KINDS[kind]]
    if only is not None:
        keep_cols = set(only) | {TIME_KEY[kind] for kind in kinds}
        want = [k for k in want if k in keep_cols]
    lo_day = _day_str(start_ms - 86_400_000) if start_ms is not None else None   # a day's file can hold a few
    hi_day = _day_str(end_ms + 86_400_000) if end_ms is not None else None       # rows from either neighbour
    parts: dict[str, list[np.ndarray]] = defaultdict(list)
    wallet_tables: list[np.ndarray] = []
    for day in days(coin, store):
        if (lo_day and day < lo_day) or (hi_day and day > hi_day):
            continue
        with np.load(Path(store) / coin / f"{day}.npz") as z:
            for k in want:
                parts[k].append(z[k] if k in z.files else _empty(k))     # a day built before that column existed
            if "trades" in kinds and "trade_buyer" in want:
                parts["_wallet_offset"].append(np.array([sum(len(w) for w in wallet_tables)]))
                wallet_tables.append(z["wallets"])
    out = {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in parts.items() if not k.startswith("_")}
    for k in want:
        out.setdefault(k, np.zeros((0, BOOK_LEVELS)) if k.endswith(("_px", "_sz", "_n")) and k[:3] in ("bid", "ask")
                       else np.zeros(0))
    if "trades" in kinds and "trade_buyer" in want:
        allw = np.concatenate(wallet_tables) if wallet_tables else np.zeros(0, dtype="<U42")
        uniq, inv = np.unique(allw, return_inverse=True)
        for side in ("trade_buyer", "trade_seller"):
            if side not in want:
                continue
            fixed = []
            for (off,), idx in zip(parts["_wallet_offset"], parts[side]):
                g = np.full(len(idx), -1, dtype=np.int64)
                ok = idx >= 0
                g[ok] = inv[off + idx[ok]]
                fixed.append(g)
            out[side] = np.concatenate(fixed) if fixed else np.zeros(0, dtype=np.int64)
        out["wallets"] = uniq
    for kind in kinds:
        tk = TIME_KEY[kind]
        t = out[tk]
        keep = np.ones(len(t), dtype=bool)
        if start_ms is not None:
            keep &= t >= start_ms
        if end_ms is not None:
            keep &= t < end_ms
        order = np.argsort(t[keep], kind="stable")
        for k in KINDS[kind]:
            if k in out:
                out[k] = out[k][keep][order]
    return out


def _empty(k: str) -> np.ndarray:
    """No rows of column k (days built before bbo and Lighter were recorded have none)."""
    if k[:4] in ("bid_", "ask_"):
        return np.zeros((0, BOOK_LEVELS), dtype="i4" if k.endswith("_n") else "f8")
    if k.endswith(("_t", "_recv", "_tid")):
        return np.zeros(0, dtype="i8")
    if k.endswith(("_venue", "_side")) and not k.startswith("trade"):
        return np.zeros(0, dtype="i1")
    if k.endswith("_n"):
        return np.zeros(0, dtype="i4")
    if k.endswith("_liq"):
        return np.zeros(0, dtype=bool)
    return np.zeros(0, dtype="f8")


def day_end_ms(day: str) -> int:
    """Midnight UTC after a stored day ("20261027" -> 28 Oct 00:00 in ms)."""
    return (int(calendar.timegm(time.strptime(day, "%Y%m%d"))) + 86_400) * 1000


def _day_str(ms: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ms / 1000))

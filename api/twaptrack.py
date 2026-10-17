"""
Big-TWAP tracker, silent (edge idea 3): the TWAP study found that big Hyperliquid TWAPs ($20k+ slices every 30s)
push price their way over the next 5-30 minutes, but the evidence is one week and the group was picked after the
fact. This spots them live and scores riding each one, without calling any trades, so the record can confirm or
kill it.

    a slice   one wallet's market orders on one coin and side within 2s of each other
    a run     slices 25-35s apart (Hyperliquid's TWAP beat); spotted at the 4th, if the slices' median is $20k+
    scored    in at the mid 6s after it's spotted, out after 5, 15 and 30 minutes, and when the beat stops (35s after
              the last slice, at most an hour), from the bot's per-second book (engine/bigmovers.settle)
"""
from collections import deque
from statistics import median

from engine.bigmovers import settle

SLICE_MS = 2000
BEAT_MS = (25_000, 35_000)
SPOT_AT = 4
MIN_SLICE_USD = 20_000
LAG_S = 6
HOLDS_S = (300, 900, 1800)
END_AFTER_MS = 35_000
END_CAP_S = 3600
KEEP = 2000


class TwapTracker:
    def __init__(self, min_slice_usd: float = MIN_SLICE_USD):
        self.min_slice_usd = min_slice_usd
        self.keys: dict[tuple, dict] = {}            # (wallet, coin, side) → {"last": slice, "run": [slices], "rec": record}
        self.records: deque = deque(maxlen=KEEP)
        self._pending: list[dict] = []
        self.unsaved: list[dict] = []

    def on_fill(self, wallet: str, coin: str, side: int, t_ms: int, usd: float) -> None:
        st = self.keys.setdefault((wallet, coin, side), {"last": None, "run": [], "rec": None})
        last = st["last"]
        if last is not None and 0 <= t_ms - last[0] <= SLICE_MS:
            last[1] += usd                               # the same market order taking another level
        else:
            sl = [t_ms, usd]
            if last is not None and BEAT_MS[0] <= t_ms - last[0] <= BEAT_MS[1]:
                st["run"].append(sl)
            else:
                self._end(st)
                st["run"], st["rec"] = [sl], None
            st["last"] = sl
        rec = st["rec"]
        if rec is None and len(st["run"]) == SPOT_AT and median(s[1] for s in st["run"]) >= self.min_slice_usd:
            rec = st["rec"] = {"id": f"twap-{coin}-{t_ms}-{side}", "coin": coin, "side": side, "wallet": wallet,
                               "t_ms": t_ms, "slices": SPOT_AT, "usd": sum(s[1] for s in st["run"]), "end_ms": None,
                               "results": {}}
            self.records.append(rec)
            self._pending.append(rec)
        elif rec is not None:
            rec["slices"] = len(st["run"])
            rec["usd"] = sum(s[1] for s in st["run"])

    def _end(self, st: dict) -> None:
        rec = st.get("rec")
        if rec is not None and rec["end_ms"] is None and st["run"]:
            rec["end_ms"] = st["run"][-1][0]

    def tick(self, now_ms: int, rows_of) -> None:
        """End runs whose beat stopped, score what's due, forget idle wallets. rows_of(coin) → per-second rows."""
        for k, st in list(self.keys.items()):
            if st["last"] is None or now_ms - st["last"][0] <= END_AFTER_MS:
                continue
            self._end(st)
            del self.keys[k]
        still = []
        for rec in self._pending:
            s0 = rec["t_ms"] // 1000
            rows = rows_of(rec["coin"])
            for h in HOLDS_S:
                if str(h) not in rec["results"] and now_ms >= (s0 + LAG_S + h + 2) * 1000:
                    rec["results"][str(h)] = settle(rows, s0, LAG_S, h, rec["side"])
            if "end" not in rec["results"]:
                end_s = None
                if rec["end_ms"] is not None:
                    end_s = rec["end_ms"] // 1000 + END_AFTER_MS // 1000
                elif now_ms >= (s0 + LAG_S + END_CAP_S + 2) * 1000:
                    end_s = s0 + LAG_S + END_CAP_S
                if end_s is not None:
                    hold = min(max(end_s - (s0 + LAG_S), 1), END_CAP_S)
                    if now_ms >= (s0 + LAG_S + hold + 2) * 1000:
                        rec["results"]["end"] = settle(rows, s0, LAG_S, hold, rec["side"])
                        rec["hold_end_s"] = hold
            if all(str(h) in rec["results"] for h in HOLDS_S) and "end" in rec["results"]:
                self.unsaved.append(rec)
            else:
                still.append(rec)
        self._pending = still

    def load(self, recs: list[dict]) -> None:
        for r in recs:
            self.records.append(r)

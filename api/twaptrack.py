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
CANDIDATE_AT = 2                 # at the 2nd slice the bot can ask Hyperliquid whether it's a TWAP (and how big)
MIN_CANDIDATE_USD = 10_000       # each of the first two slices at least this


class TwapTracker:
    def __init__(self, min_slice_usd: float = MIN_SLICE_USD):
        self.min_slice_usd = min_slice_usd
        self.keys: dict[tuple, dict] = {}            # (wallet, coin, side) → {"last": slice, "run": [slices], "rec": record}
        self.records: deque = deque(maxlen=KEEP)
        self._pending: list[dict] = []
        self.unsaved: list[dict] = []
        self.candidates: list[tuple] = []             # keys worth asking about (drained by the bot)

    def on_fill(self, wallet: str, coin: str, side: int, t_ms: int, usd: float) -> dict | None:
        """One taker fill. Returns the record when this fill makes a run count as a big TWAP (spotted)."""
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
                st["run"], st["rec"], st["asked"] = [sl], None, False
            st["last"] = sl
        rec = st["rec"]
        if (rec is None and not st.get("asked") and len(st["run"]) == CANDIDATE_AT
                and min(s[1] for s in st["run"]) >= MIN_CANDIDATE_USD):
            st["asked"] = True
            self.candidates.append((wallet, coin, side))
        if rec is None and len(st["run"]) == SPOT_AT and median(s[1] for s in st["run"]) >= self.min_slice_usd:
            return self._make(st, wallet, coin, side, t_ms)
        if rec is not None:
            rec["slices"] = len(st["run"])
            rec["usd"] = sum(s[1] for s in st["run"])
            rec["last_ms"] = st["run"][-1][0]
        return None

    def _make(self, st: dict, wallet: str, coin: str, side: int, t_ms: int) -> dict:
        rec = st["rec"] = {"id": f"twap-{coin}-{t_ms}-{side}", "coin": coin, "side": side, "wallet": wallet,
                           "t_ms": t_ms, "start_ms": st["run"][0][0], "last_ms": st["run"][-1][0],
                           "slices": len(st["run"]), "spot_slice": len(st["run"]),
                           "usd": sum(s[1] for s in st["run"]), "end_ms": None, "results": {}}
        self.records.append(rec)
        self._pending.append(rec)
        return rec

    def spot(self, key: tuple, t_ms: int) -> dict | None:
        """Count this run as a big TWAP now (Hyperliquid confirmed it before slice 4). None if it's gone or
        already counted."""
        st = self.keys.get(key)
        if st is None or st["rec"] is not None or not st["run"]:
            return None
        wallet, coin, side = key
        return self._make(st, wallet, coin, side, t_ms)

    def run_start(self, key: tuple) -> int | None:
        st = self.keys.get(key)
        return st["run"][0][0] if st and st["run"] else None

    def run_usd(self, key: tuple) -> float:
        st = self.keys.get(key)
        return sum(s[1] for s in st["run"]) if st and st["run"] else 0.0

    def _end(self, st: dict) -> None:
        rec = st.get("rec")
        if rec is not None and rec["end_ms"] is None and st["run"]:
            rec["end_ms"] = st["run"][-1][0]

    def tick(self, now_ms: int, rows_of, stale_of=None) -> None:
        """End runs whose beat stopped, score what's due, forget idle wallets. rows_of(coin) → per-second rows;
        stale_of(rows, s_from, s_to) → settle()'s gap limit (longer across a restart; default 5s)."""
        lim = (lambda rows, a, b: stale_of(rows, a, b)) if stale_of else (lambda rows, a, b: 5)
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
                    rec["results"][str(h)] = settle(rows, s0, LAG_S, h, rec["side"],
                                                    stale_s=lim(rows, s0 + LAG_S, s0 + LAG_S + h))
            if "end" not in rec["results"]:
                end_s = None
                if rec["end_ms"] is not None:
                    end_s = rec["end_ms"] // 1000 + END_AFTER_MS // 1000
                elif now_ms >= (s0 + LAG_S + END_CAP_S + 2) * 1000:
                    end_s = s0 + LAG_S + END_CAP_S
                if end_s is not None:
                    hold = min(max(end_s - (s0 + LAG_S), 1), END_CAP_S)
                    if now_ms >= (s0 + LAG_S + hold + 2) * 1000:
                        rec["results"]["end"] = settle(rows, s0, LAG_S, hold, rec["side"],
                                                       stale_s=lim(rows, s0 + LAG_S, s0 + LAG_S + hold))
                        rec["hold_end_s"] = hold
            if all(str(h) in rec["results"] for h in HOLDS_S) and "end" in rec["results"]:
                self.unsaved.append(rec)
            else:
                still.append(rec)
        self._pending = still

    def running(self) -> list[dict]:
        """Spotted TWAPs whose beat hasn't stopped yet, oldest first."""
        return sorted((st["rec"] for st in self.keys.values() if st["rec"] is not None and st["rec"]["end_ms"] is None),
                      key=lambda r: r["t_ms"])

    def load(self, recs: list[dict]) -> None:
        for r in recs:
            self.records.append(r)

    # ── surviving a restart (api/livestate) ────────────────────────────────
    def dump(self) -> dict:
        """The runs being followed and the spotted TWAPs not scored yet (records by id, so links survive)."""
        return {"pending": list(self._pending),
                "keys": [[w, c, sd, {"last": st["last"], "run": st["run"], "asked": st.get("asked", False),
                                     "rec": st["rec"]["id"] if st["rec"] is not None else None}]
                         for (w, c, sd), st in self.keys.items()]}

    def restore(self, d: dict) -> list[dict]:
        """→ the open records it picked back up."""
        by_id = {r["id"]: r for r in self.records}
        back = []
        for r in sorted(d.get("pending") or [], key=lambda r: r["t_ms"]):
            if r.get("id") in by_id:
                continue
            self.records.append(r)
            self._pending.append(r)
            by_id[r["id"]] = r
            back.append(r)
        for w, c, sd, st in d.get("keys") or []:
            run, last = st.get("run") or [], st.get("last")
            if run and last == run[-1]:
                last = run[-1]                        # the same slice object, so a fill merged into it counts in both
            self.keys[(w, c, int(sd))] = {"last": last, "run": run, "asked": st.get("asked", False),
                                          "rec": by_id.get(st.get("rec"))}
        return back

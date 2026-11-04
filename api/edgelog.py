"""
The edge terminal's change log: what changed on the edge markets, written by the server as it happens (not by the
page), kept in EDGE_LOG_FILE so a reload or a restart shows the same history.

    move    a market moved MOVE_BPS from where it was last logged (fx 8, oil 15, metals 12 bps)
    sess    the real market opened, shut or went on its daily break (Hyperliquid trading alone)
    roll    the oil roll's weights stepped
    flow    a market's takers ran at FLOW_HOT× its normal pace over 15 minutes (logged once until it calms below
            FLOW_CALM×), with the share bought
    cal     a scheduled event is 15 minutes away
    alert   the yen rallying fast; a source that can't be reached
    sys     the server started; a source came back

Entries older than KEEP_DAYS, or past KEEP_N, are dropped.
"""
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("analytix.edgelog")

KEEP_N = 600
KEEP_DAYS = 7
SAVE_EVERY_S = 5
MOVE_BPS = {"fx": 8, "oil": 15, "metal": 12}
FLOW_HOT = 2.0
FLOW_CALM = 1.5
SOON_MS = 15 * 60_000


def px_text(p: float) -> str:
    return f"{p:,.1f}" if p >= 1000 else f"{p:.2f}" if p >= 10 else f"{p:.4f}"


class ChangeLog:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.rows: deque = deque(maxlen=KEEP_N)
        self.n = 0
        self._dirty = False
        self._saved = 0.0
        self.anchor: dict[str, tuple[float, int]] = {}      # coin → (price, when) last logged from
        self.sess: dict[str, tuple] = {}                     # coin → (open, why)
        self.weights: dict[str, float] = {}                  # coin → the roll's front weight
        self.hot: dict[str, bool] = {}                       # coin → flow surge already logged
        self.said: set[str] = set()                          # events already announced
        self.yen = False
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            got = json.loads(self.path.read_text())
            for r in got.get("rows", [])[-KEEP_N:]:
                self.rows.append(r)
            self.said = set(got.get("said", []))
            self.anchor = {k: tuple(v) for k, v in (got.get("anchor") or {}).items()}
            self.n = len(self.rows)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("change log: %s unreadable (%s); starting a new one", self.path, exc)

    def save(self, force: bool = False) -> None:
        if not self.path or not self._dirty or (not force and time.time() - self._saved < SAVE_EVERY_S):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"rows": list(self.rows), "said": sorted(self.said)[-200:],
                                       "anchor": {k: list(v) for k, v in self.anchor.items()}}, separators=(",", ":")))
            os.replace(tmp, self.path)                     # a crash mid-write never leaves half a file
            self._dirty = False
            self._saved = time.time()
        except OSError as exc:
            log.warning("change log: can't save to %s (%s)", self.path, exc)

    def add(self, kind: str, text: str, t_ms: int, **extra) -> None:
        self.n += 1
        self.rows.append({"t": int(t_ms), "kind": kind, "text": text, "id": f"{int(t_ms)}-{self.n}", **extra})
        while self.rows and t_ms - self.rows[0]["t"] > KEEP_DAYS * 86_400_000:
            self.rows.popleft()
        self._dirty = True

    def newest(self, n: int = 120) -> list[dict]:
        return list(reversed(list(self.rows)[-n:]))

    # ── what changed ──────────────────────────────────────────────────────
    def move(self, name: str, coin: str, kind: str, price: float | None, now_ms: int) -> None:
        if not price:
            return
        a = self.anchor.get(coin)
        if not a or now_ms - a[1] > 6 * 3_600_000:         # nothing yet, or a stale anchor from before a long gap
            self.anchor[coin] = (price, now_ms)
            return
        bps = (price / a[0] - 1) * 1e4
        if abs(bps) >= MOVE_BPS.get(kind, 12):
            mins = max(1, round((now_ms - a[1]) / 60_000))
            self.add("up" if bps > 0 else "down",
                     f"{name} {px_text(price)} {'▲ +' if bps > 0 else '▼ −'}{abs(bps):.0f} bps in {mins} min", now_ms)
            self.anchor[coin] = (price, now_ms)

    def sessions(self, states: dict[str, tuple[str, dict]], now_ms: int) -> None:
        """`states`: coin → (name, session state). Markets that flip together get one line."""
        flipped = []
        for coin, (name, s) in states.items():
            key = (s["open"], s.get("why"))
            was = self.sess.get(coin)
            self.sess[coin] = key
            if was is not None and was[0] != key[0]:
                flipped.append((name, s))
        if flipped:
            s = flipped[0][1]
            what = "open again" if s["open"] else "on its daily break" if s.get("why") == "break" else "shut"
            alone = not s["open"] and s.get("why") != "break"
            self.add("sess", f"{', '.join(n for n, _ in flipped)}: real market {what}{' · Hyperliquid alone' if alone else ''}",
                     now_ms)

    def roll(self, coin: str, label: str, front: str, nxt: str, weight: float, now_ms: int) -> None:
        was = self.weights.get(coin)
        self.weights[coin] = weight
        if was is not None and abs(was - weight) > 1e-9:
            self.add("roll", f"{label} roll: {round(weight * 100)}% {front} / {round((1 - weight) * 100)}% {nxt}", now_ms)

    def flow(self, coin: str, name: str, pace: float | None, buy_share: float | None, now_ms: int) -> None:
        if pace is None:
            return
        if pace >= FLOW_HOT and not self.hot.get(coin):
            self.hot[coin] = True
            side = "" if buy_share is None else f" · {round(buy_share * 100)}% bought" if buy_share >= 0.5 \
                else f" · {round((1 - buy_share) * 100)}% sold"
            self.add("flow", f"{name} flow {pace:.1f}× its normal pace over 15 min{side}", now_ms,
                     side="up" if (buy_share or 0.5) >= 0.5 else "down")
        elif pace < FLOW_CALM:
            self.hot[coin] = False

    def soon(self, events: list[dict], now_ms: int) -> None:
        for e in events:
            left = e["t_ms"] - now_ms
            key = f"{e['t_ms']}-{e['title']}"
            if not e.get("all_day") and 0 < left <= SOON_MS and key not in self.said:
                self.said.add(key)
                self.add("cal", f"{e['title']} in {max(1, round(left / 60_000))} min", now_ms)

    def yen_watch(self, on: bool, now_ms: int) -> None:
        if on and not self.yen:
            self.add("alert", "The yen is rallying fast: the kind of move that forced carry-trade selling in August 2024.",
                     now_ms)
        self.yen = on

    def status(self, src: str, was: bool | None, ok: bool, now_ms: int) -> None:
        if was is True and not ok:
            self.add("alert", f"{src}: can't be reached", now_ms)
        elif was is False and ok:
            self.add("sys", f"{src}: back", now_ms)

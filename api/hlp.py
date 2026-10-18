"""
HLP recorder (edge idea 7). HLP is Hyperliquid's own market maker: it takes the other side of traders, so its
positions show what everyone else did against it. If its shifts predict price, that's an edge built on the same
"uninformed pressure reverses" pattern as the fades. Hyperliquid keeps no history of positions, so this records
them now, for a test in a week or two. Nothing in the UI until it's tested.

    who     HLP and its sub-vaults (from Hyperliquid's vault list, else the known HLP address) and any vault named
            Liquidator. The Liquidator takes over positions the order book couldn't absorb, so what it holds and
            sells comes from forced liquidations, for sure
    what    every ROUND_S seconds: each one's positions on the watched coins and its account value → hlp_positions
            (the liquidation map's columns); every minute: what each one bought and sold on the watched coins,
            from the trade feed → hlp_flow
    where   data/positions.sqlite, kept KEEP_DAYS days; live mode only
"""
import asyncio
import logging
import sqlite3
import time
from pathlib import Path

import httpx

from api.liqmap import parse_state
from config import HL_INFO_URL
from storage.sqlite import connect, transaction

log = logging.getLogger("analytix.hlp")

HLP_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
ROUND_S = 60
FIND_S = 6 * 3600              # look the vaults up again this often (HLP can add sub-vaults)
RETRY_S = 300
KEEP_DAYS = 60
MINUTE_MS = 60_000


class HLPRecorder:
    def __init__(self, runtime, db_path: Path | None, url: str = HL_INFO_URL, vaults_url: str = VAULTS_URL,
                 round_s: float = ROUND_S):
        self.rt = runtime
        self.db_path = db_path
        self.url, self.vaults_url, self.round_s = url, vaults_url, round_s
        self.who: dict[str, str] = {}                     # address → label: "HLP", "HLP Strategy A", "Liquidator", ...
        self.flow: dict[tuple, list[float]] = {}          # (minute ms, address, coin) → [bought $, sold $, taker bought $, taker sold $, fills]
        self.rounds = 0
        self.last = {"t_ms": 0, "vaults": 0, "positions": 0, "liquidator_fills": 0}
        self._found_at = 0.0
        self._db: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.rt.mode == "live" and self.db_path is not None

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            self._db = connect(self.db_path)
            self._db.execute("CREATE TABLE IF NOT EXISTS hlp_positions (t_ms INTEGER NOT NULL, wallet TEXT NOT NULL, "
                             "coin TEXT NOT NULL, szi REAL, entry_px REAL, liq_px REAL, value_usd REAL, leverage REAL, "
                             "account_usd REAL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS hlp_positions_t ON hlp_positions (t_ms, coin)")
            self._db.execute("CREATE TABLE IF NOT EXISTS hlp_flow (t_ms INTEGER NOT NULL, wallet TEXT NOT NULL, "
                             "coin TEXT NOT NULL, bought_usd REAL, sold_usd REAL, taker_bought_usd REAL, "
                             "taker_sold_usd REAL, fills INTEGER, PRIMARY KEY (t_ms, wallet, coin))")
            self._db.execute("CREATE TABLE IF NOT EXISTS hlp_vaults (wallet TEXT PRIMARY KEY, label TEXT, seen_ms INTEGER)")
            self._db.commit()
        except Exception:
            log.exception("could not open the positions history; HLP won't be recorded")
            self._db = None
            return
        self._task = asyncio.create_task(self._loop(), name="hlp")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._flush(force=True)
        if self._db:
            self._db.close()

    # ── fills, from the trade feed ─────────────────────────────────────────
    def on_message(self, msg: dict) -> None:
        if msg.get("channel") != "trades" or not self.who:
            return
        for tr in msg.get("data") or []:
            users = [u.lower() for u in tr.get("users") or []]
            if len(users) != 2 or not (users[0] in self.who or users[1] in self.who):
                continue
            try:
                usd, ms, coin, side = float(tr["px"]) * float(tr["sz"]), int(tr["time"]), tr["coin"], tr["side"]
            except (KeyError, TypeError, ValueError):
                continue
            for i, w in enumerate(users):                  # users = [buyer, seller]; side B = the buyer took
                if w not in self.who:
                    continue
                f = self.flow.setdefault((ms - ms % MINUTE_MS, w, coin), [0.0, 0.0, 0.0, 0.0, 0])
                f[i] += usd
                if (i == 0) == (side == "B"):
                    f[2 + i] += usd
                f[4] += 1

    def _flush(self, force: bool = False) -> None:
        now_min = int(time.time() * 1000) // MINUTE_MS * MINUTE_MS
        done = [k for k in self.flow if force or k[0] < now_min]
        if not done:
            return
        rows = [(*k, *self.flow.pop(k)) for k in done]
        self.last["liquidator_fills"] += sum(r[7] for r in rows if "liquidator" in self.who.get(r[1], "").lower())
        if not self._db:
            return
        try:
            with transaction(self._db):
                self._db.executemany("INSERT OR REPLACE INTO hlp_flow VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        except sqlite3.Error as exc:
            log.warning("could not save HLP fills: %s", exc)

    # ── positions, every minute ────────────────────────────────────────────
    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                t = time.time()
                try:
                    if not self.who or t - self._found_at > FIND_S:
                        await self.find(client)
                    if self.who:
                        await self.round(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("HLP round failed")
                self._flush()
                wait = self.round_s if self.who else RETRY_S
                await asyncio.sleep(max(5.0, wait - (time.time() - t)))

    async def find(self, client: httpx.AsyncClient) -> None:
        listed = []
        try:
            r = await client.get(self.vaults_url, timeout=60)
            r.raise_for_status()
            listed = r.json() or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("could not get the vault list (%s); using the known HLP address", exc)
        hlp, who = find_vaults(listed)
        hlp = hlp or HLP_ADDRESS
        who[hlp] = "HLP"
        r = await client.post(self.url, json={"type": "vaultDetails", "vaultAddress": hlp})
        r.raise_for_status()
        for c in children_of(r.json() or {}):
            name = ""
            try:
                rc = await client.post(self.url, json={"type": "vaultDetails", "vaultAddress": c})
                rc.raise_for_status()
                name = ((rc.json() or {}).get("name") or "").strip()
            except (httpx.HTTPError, ValueError):
                pass
            who[c] = name or f"HLP sub-vault {c[:6]}"
        self.who, self._found_at = who, time.time()
        log.info("HLP: recording %s", ", ".join(f"{v} ({k[:6]}…)" for k, v in who.items()))
        if self._db:
            try:
                with transaction(self._db):
                    self._db.executemany("INSERT OR REPLACE INTO hlp_vaults VALUES (?, ?, ?)",
                                         [(k, v, int(time.time() * 1000)) for k, v in who.items()])
            except sqlite3.Error as exc:
                log.warning("could not save the HLP vault list: %s", exc)

    async def round(self, client: httpx.AsyncClient) -> None:
        coins = set(self.rt.coins)
        t_ms = int(time.time() * 1000)
        rows = []
        for w in list(self.who):
            try:
                r = await client.post(self.url, json={"type": "clearinghouseState", "user": w})
                r.raise_for_status()
                st = r.json() or {}
            except (httpx.HTTPError, ValueError):
                continue
            got = parse_state(st, w, t_ms, coins)
            if not got:                                    # no position: keep the account value so the minute counts
                acct = ((st.get("marginSummary") or {}).get("accountValue"))
                got = [(t_ms, w, "", 0.0, None, None, 0.0, None, float(acct) if acct is not None else None)]
            rows.extend(got)
        self.rounds += 1
        self.last.update(t_ms=t_ms, vaults=len(self.who), positions=sum(1 for r in rows if r[2]))
        if not self._db or not rows:
            return
        try:
            with transaction(self._db):
                self._db.executemany("INSERT INTO hlp_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                if self.rounds % 1440 == 1:                # about once a day
                    cut = t_ms - KEEP_DAYS * 86_400_000
                    self._db.execute("DELETE FROM hlp_positions WHERE t_ms < ?", (cut,))
                    self._db.execute("DELETE FROM hlp_flow WHERE t_ms < ?", (cut,))
        except sqlite3.Error as exc:
            log.warning("could not save HLP positions: %s", exc)


def find_vaults(listed: list) -> tuple[str | None, dict[str, str]]:
    """From the vault list: HLP's address, and every vault named Liquidator."""
    hlp, who = None, {}
    for e in listed:
        s = e.get("summary") if isinstance(e, dict) and isinstance(e.get("summary"), dict) else e
        if not isinstance(s, dict) or not s.get("vaultAddress"):
            continue
        name, addr = (s.get("name") or "").strip(), s["vaultAddress"].lower()
        if name.startswith("Hyperliquidity Provider"):
            hlp = addr
        elif name.lower() == "liquidator":
            who[addr] = "Liquidator"
    return hlp, who


def children_of(details: dict) -> list[str]:
    rel = details.get("relationship") or {}
    data = (rel.get("data") or {}) if isinstance(rel, dict) else {}
    return [c.lower() for c in data.get("childAddresses") or [] if isinstance(c, str)]

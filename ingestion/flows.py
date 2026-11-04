"""
Slow money into BTC, from public sources (for scripts/btcEstimate.py):

    ETF flows      US spot bitcoin ETFs' daily net flows in US$m, per fund and in total: TFTC's open JSON (the
                   whole series since 11 Jan 2024, from SoSoValue and Farside; CC BY 4.0, credit "TFTC Bitcoin ETF
                   Flows"), or Farside Investors' table (farside.co.uk/btc/, which turns scripts away, so from a copy
                   saved in a browser: --etf-file)
    Strategy       every 8-K Strategy (MicroStrategy) filed with the SEC: BTC bought and total held, by filing date
                   (when the market learned of it). SEC asks scripts to name themselves: set ANALYTIX_SEC_UA to
                   "your name your@email" if it refuses the default
    other DATs     treasury companies' purchases from a CSV kept by hand (date,company,btc)
    prices         BTC hourly closes from Binance's or Coinbase's public candles (kept in a cache file, topped up each
                   run), MSTR daily closes from Nasdaq's quote history; Yahoo and Stooq as fallbacks (Yahoo
                   rate-limits busy servers)
"""
import asyncio
import csv
import json
import os
import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import httpx

FARSIDE_URL = "https://farside.co.uk/btc/"
TFTC_URL = "https://www.tftc.io/bitcoin-etf-flows/data.json"    # TFTC's tracker, CC BY 4.0 (SoSoValue and Farside data)
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
SEC_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
STRATEGY_CIK = 1050446
YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}")
STOOQ = "https://stooq.com/q/d/l/?s={symbol}&i=d"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
NASDAQ_HISTORY = "https://api.nasdaq.com/api/quote/{symbol}/historical"
HOUR_MS = 3_600_000
BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


def sec_headers() -> dict:
    return {"User-Agent": os.getenv("ANALYTIX_SEC_UA", "Analytix research script"), "Accept-Encoding": "gzip, deflate"}


# ── HTML to rows / text ───────────────────────────────────────────────────────
class _Rows(HTMLParser):
    """Every table row's cells as text."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def table_rows(html: str) -> list[list[str]]:
    p = _Rows()
    p.feed(html)
    return p.rows


def html_text(html: str) -> str:
    """The visible text of a page, one space between everything."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return " ".join(unescape(re.sub(r"(?s)<[^>]+>", " ", html)).replace("\xa0", " ").split())


# ── ETF flows ─────────────────────────────────────────────────────────────────
def _usd_m(cell: str) -> float | None:
    """Farside's numbers: "12.3", "(45.6)" negative, "-" or blank none."""
    c = cell.replace(",", "").strip()
    if c in ("", "-", "–"):
        return None
    neg = c.startswith("(") and c.endswith(")")
    try:
        v = float(c.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def parse_farside(html: str) -> list[dict]:
    """[{"date": "2024-01-11", "total": $m, "funds": {ticker: $m}}] in date order, from the flow table. The header row
    is the one starting "Date"; summary rows (Total, Average, Maximum, Minimum) and days with no total are skipped."""
    rows = table_rows(html)
    head = next((r for r in rows if r and r[0].strip().lower() == "date"), None)
    out = []
    for r in rows:
        if not r:
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d %b %Y").date()
        except ValueError:
            continue
        vals = [_usd_m(c) for c in r[1:]]
        total = vals[-1] if vals else None
        if total is None:
            continue
        names = head[1:] if head and len(head) == len(r) else [f"col{i}" for i in range(1, len(r))]
        out.append({"date": d.isoformat(), "total": total,
                    "funds": {n: v for n, v in zip(names[:-1], vals[:-1]) if v is not None}})
    by = {x["date"]: x for x in out}
    return [by[k] for k in sorted(by)]


def parse_tftc(j: dict) -> list[dict]:
    """TFTC's data.json: {"days": [{"date", "netFlowUsd", "perEtfUsd"}]}, dollars → US$m. Days with no flows at
    all (perEtfUsd null and a zero total: market holidays) are left out."""
    out = []
    for x in (j or {}).get("days") or []:
        d, total, funds = x.get("date"), x.get("netFlowUsd"), x.get("perEtfUsd")
        if not d or total is None or (funds is None and not total):
            continue
        out.append({"date": d, "total": round(float(total) / 1e6, 2),
                    "funds": {k: round(float(v) / 1e6, 2) for k, v in (funds or {}).items() if v is not None}})
    by = {x["date"]: x for x in out}
    return [by[k] for k in sorted(by)]


async def fetch_tftc(url: str = TFTC_URL) -> list[dict]:
    async with httpx.AsyncClient(timeout=60, headers=BROWSER, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return parse_tftc(r.json())


async def fetch_farside(url: str = FARSIDE_URL) -> str:
    async with httpx.AsyncClient(timeout=30, headers=BROWSER, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


def read_etf_file(path: str | Path) -> list[dict]:
    """A saved copy of the Farside page (.html), or a CSV with date and total columns (US$m)."""
    p = Path(path)
    text = p.read_text(errors="replace")
    if p.suffix.lower() in (".htm", ".html") or "<table" in text[:200_000].lower():
        return parse_farside(text)
    out = []
    for row in csv.DictReader(text.splitlines()):
        k = {x.lower().strip(): v for x, v in row.items() if x}
        d, t = k.get("date"), k.get("total")
        if not d or t is None:
            continue
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%m/%d/%Y"):
            try:
                day = datetime.strptime(d.strip(), fmt).date().isoformat()
                break
            except ValueError:
                day = None
        v = _usd_m(t)
        if day and v is not None:
            out.append({"date": day, "total": v, "funds": {}})
    return sorted(out, key=lambda x: x["date"])


# ── Strategy's 8-K filings ────────────────────────────────────────────────────
MONTH_DAY_YEAR = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_OLD_BOUGHT = re.compile(r"acquired (?:an aggregate of )?approximately ([\d,]+) (?:additional )?(?:bitcoins?|BTC)\b", re.I)
_OLD_HELD = re.compile(r"held (?:an aggregate of )?approximately ([\d,]+) (?:bitcoins?|BTC)\b", re.I)
_PERIOD = re.compile(r"(?:([A-Z][a-z]+ \d{1,2}, \d{4})\s*(?:to|through|-|–|—)\s*([A-Z][a-z]+ \d{1,2}, \d{4})"
                     r"|(\d{1,2}/\d{1,2}/\d{4})\s*(?:to|-|–|—)\s*(\d{1,2}/\d{1,2}/\d{4}))")
_CELL = re.compile(r"\$?\s?(\d[\d,]*(?:\.\d+)?)|(?<!\S)([—–-])(?!\S)")
PARSER_VERSION = 2                                         # bump when the reading changes: cached filings are read again
_NONE = re.compile(r"did not (?:acquire|purchase) any (?:bitcoin|BTC)", re.I)


def _day(x: str) -> str | None:
    for fmt in ("%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(x, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_table(text: str) -> dict | None:
    """The bitcoin table Strategy's 8-Ks carry since 2025: after the "BTC Acquired" header, one row per period,
    "April 1, 2026 to April 5, 2026  4,871  $329.9  $67,718  766,970  $58.02  $75,644" (a dash where none were
    bought). Columns: acquired, purchase price, average price, aggregate holdings, ... Rows are summed; holdings and
    the period end are the last row's."""
    i = text.find("BTC Acquired")
    if i < 0:
        return None
    window = re.sub(r"\(\d\)", " ", text[i:i + 3000])           # footnote marks like (1)
    rows = list(_PERIOD.finditer(window))
    bought, held, end = 0.0, None, None
    for k, m in enumerate(rows):
        stop = rows[k + 1].start() if k + 1 < len(rows) else len(window)
        cells = [(a, d) for a, d in _CELL.findall(window[m.end():stop])][:4]
        if len(cells) < 4:
            continue
        first, fourth = cells[0], cells[3]
        bought += 0.0 if first[1] else float(first[0].replace(",", ""))
        if not fourth[1]:
            held = float(fourth[0].replace(",", ""))
        end = _day(m.group(2) or m.group(4))
    if end is None:
        return None
    return {"bought": bought, "held": held, "period_end": end}


def parse_purchase(text: str) -> dict | None:
    """{"bought": BTC, "held": BTC or None, "period_end": date or None} from one filing's text, or None if it says
    nothing about buying. Reads the table Strategy has used since 2025 and the sentences it used before."""
    t = parse_table(text)
    if t:
        return t
    b = _OLD_BOUGHT.search(text)
    h = _OLD_HELD.search(text)
    if b or (h and _NONE.search(text)):
        end = None
        mm = re.search(r"between " + MONTH_DAY_YEAR + r" and " + MONTH_DAY_YEAR, text)
        if mm:
            try:
                end = datetime.strptime(mm.group(2), "%B %d, %Y").date().isoformat()
            except ValueError:
                end = None
        return {"bought": float(b.group(1).replace(",", "")) if b else 0.0,
                "held": float(h.group(1).replace(",", "")) if h else None, "period_end": end}
    return None


async def fetch_strategy(since: str = "2024-01-01", cik: int = STRATEGY_CIK, cache: Path | None = None, log=None) -> list[dict]:
    """[{"filed": date, "bought", "held", "period_end", "accession"}] for every 8-K since `since` that reports buying
    (or a week without). Documents already read are kept in `cache` (a JSON file), so reruns only fetch new ones."""
    seen = json.loads(cache.read_text()) if cache and cache.exists() else {}
    if seen.get("_version") != PARSER_VERSION:                # read with an older parser: read everything again
        seen = {"_version": PARSER_VERSION}
    async with httpx.AsyncClient(timeout=30, headers=sec_headers(), follow_redirects=True) as c:
        r = await c.get(SEC_SUBMISSIONS.format(cik=cik))
        r.raise_for_status()
        recent = r.json()["filings"]["recent"]
        out = []
        for form, filed, acc, doc in zip(recent["form"], recent["filingDate"], recent["accessionNumber"], recent["primaryDocument"]):
            if form != "8-K" or filed < since:
                continue
            key = acc
            if key not in seen:
                acc_dir = acc.replace("-", "")
                docs = [doc]
                try:
                    ix = await c.get(SEC_INDEX.format(cik=cik, acc=acc_dir))
                    if ix.status_code == 200:
                        docs += [x["name"] for x in ix.json().get("directory", {}).get("item", [])
                                 if x["name"].lower().endswith(".htm") and x["name"] != doc and "ex99" in x["name"].lower()]
                except (httpx.HTTPError, ValueError):
                    pass
                found = None
                for d in docs:
                    await asyncio.sleep(0.25)                           # SEC allows 10 a second; stay far under
                    try:
                        page = await c.get(SEC_ARCHIVE.format(cik=cik, acc=acc_dir, doc=d))
                    except httpx.HTTPError:
                        continue
                    if page.status_code != 200:
                        continue
                    found = parse_purchase(html_text(page.text))
                    if found:
                        break
                seen[key] = found
                if cache:
                    cache.write_text(json.dumps(seen))
                if log:
                    print(f"  8-K {filed}: {found}", file=log)
            if seen.get(key):
                out.append({"filed": filed, "accession": acc, **seen[key]})
    if log:
        n_8k = sum(1 for k in seen if k != "_version")
        print(f"Strategy: {n_8k} 8-Ks since {since}, {len(out)} with bitcoin purchases or holdings", file=log)
    return sorted(out, key=lambda x: x["filed"])


def read_dat_csv(path: str | Path) -> list[dict]:
    """Other treasury companies' purchases, by hand: date (the day it was announced), company, btc."""
    out = []
    for row in csv.DictReader(Path(path).read_text().splitlines()):
        try:
            out.append({"filed": row["date"].strip(), "company": row.get("company", "").strip(), "bought": float(row["btc"])})
        except (KeyError, ValueError):
            continue
    return out


# ── prices ────────────────────────────────────────────────────────────────────
async def fetch_yahoo(symbol: str, rng: str = "5y", interval: str = "1d", cache: Path | None = None) -> list[tuple[int, float]]:
    """[(time ms, close)] from Yahoo's chart endpoint (hourly goes back 730 days at most). Yahoo rate-limits busy
    server addresses (429), so it waits and retries, tries its second host, and keeps the last good reply in `cache`
    (a folder) to fall back on."""
    key = cache / f"yahoo_{symbol}_{rng}_{interval}.json" if cache else None
    last = None
    async with httpx.AsyncClient(timeout=30, headers=BROWSER, follow_redirects=True) as c:
        for attempt in range(4):
            url = YAHOO[attempt % 2].format(symbol=symbol)
            try:
                r = await c.get(url, params={"range": rng, "interval": interval})
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                ts, closes = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
                rows = [(int(t) * 1000, float(x)) for t, x in zip(ts, closes) if x is not None]
                if key:
                    key.write_text(json.dumps(rows))
                return rows
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(5)
    if key and key.exists():
        return [tuple(x) for x in json.loads(key.read_text())]
    raise RuntimeError(f"Yahoo {symbol}: {last}")


async def fetch_stooq(symbol: str) -> list[tuple[int, float]]:
    """Daily closes from Stooq's free CSV (symbol like "mstr.us"), stamped at midnight UTC of each trading day."""
    async with httpx.AsyncClient(timeout=30, headers=BROWSER, follow_redirects=True) as c:
        r = await c.get(STOOQ.format(symbol=symbol))
        r.raise_for_status()
    out = []
    for row in csv.DictReader(r.text.splitlines()):
        try:
            d = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            out.append((int(d.timestamp() * 1000), float(row["Close"])))
        except (KeyError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"Stooq {symbol}: no rows ({r.text[:80]!r})")
    return out


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()

async def fetch_hourly_btc(start_ms: int, cache: Path | None = None, log=None) -> tuple[list[tuple[int, float]], str]:
    """BTC/USD hourly closes from `start_ms` to now, stamped at each hour's end: from the cache, topped up from
    Binance (BTCUSDT), or Coinbase (BTC-USD) when Binance refuses this server."""
    key = cache / "btc_hourly.json" if cache else None
    have = [tuple(x) for x in json.loads(key.read_text())] if key and key.exists() else []
    have = [x for x in have if x[0] >= start_ms]
    since = have[-1][0] if have and have[0][0] <= start_ms + 2 * HOUR_MS else start_ms   # top up, or start over
    now = int(time_ms())
    new, src = [], ""
    try:
        async with httpx.AsyncClient(timeout=30, headers=BROWSER) as c:
            t = since - HOUR_MS
            while t < now - HOUR_MS:
                r = await c.get(BINANCE_KLINES, params={"symbol": "BTCUSDT", "interval": "1h", "startTime": t, "limit": 1000})
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                new += [(int(k[0]) + HOUR_MS, float(k[4])) for k in rows]
                t = int(rows[-1][0]) + HOUR_MS
                await asyncio.sleep(0.2)
        src = "Binance BTCUSDT"
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        if log:
            print(f"Binance hourly: {type(exc).__name__}: {exc}; trying Coinbase", file=log)
        new = []
        try:
            async with httpx.AsyncClient(timeout=30, headers=BROWSER) as c:
                t = since - HOUR_MS
                while t < now - HOUR_MS:
                    end = min(t + 300 * HOUR_MS, now)
                    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    r = await c.get(COINBASE_CANDLES.format(product="BTC-USD"),
                                    params={"granularity": 3600, "start": iso(t), "end": iso(end)})
                    r.raise_for_status()
                    new += [(int(k[0]) * 1000 + HOUR_MS, float(k[4])) for k in r.json()]
                    t = end
                    await asyncio.sleep(0.4)
            src = "Coinbase BTC-USD"
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc2:
            if log:
                print(f"Coinbase hourly: {type(exc2).__name__}: {exc2}", file=log)
            new, src = [], "cache only" if have else ""
    by = dict(have)
    by.update(new)
    out = sorted(by.items())
    if key and out:
        key.write_text(json.dumps(out))
    return out, src


async def fetch_nasdaq_daily(symbol: str, start: str = "2023-12-01") -> list[tuple[int, float]]:
    """Daily closes from Nasdaq's public quote history, stamped at 21:00 UTC (just after the US close)."""
    headers = {**BROWSER, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
               "Referer": "https://www.nasdaq.com/"}
    today = datetime.now(timezone.utc).date().isoformat()
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as c:
        r = await c.get(NASDAQ_HISTORY.format(symbol=symbol),
                        params={"assetclass": "stocks", "fromdate": start, "todate": today, "limit": 9999})
        r.raise_for_status()
        rows = (((r.json() or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for x in rows:
        try:
            d = datetime.strptime(x["date"], "%m/%d/%Y").replace(tzinfo=timezone.utc)
            out.append((int(d.timestamp() * 1000) + 21 * HOUR_MS, float(str(x["close"]).replace("$", "").replace(",", ""))))
        except (KeyError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"Nasdaq {symbol}: no rows")
    return sorted(out)


def time_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000

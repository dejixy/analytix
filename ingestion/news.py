"""
News for the followed coins: scheduled US economic releases and crypto headlines, each kept with when it was
published (or is due) and when the server first saw it, tagged by coin and category.

    calendar    Forex Factory's weekly calendar (this week and next): US releases of High or Medium impact and
                Japan's High-impact ones (the Bank of Japan, for the yen), with the forecast and previous values;
                their times are known days ahead
    headlines   CoinDesk, Cointelegraph, The Block and Decrypt RSS (free, minutes behind the market: they explain
                moves, they don't front-run them)
    tags        coins by name or ticker in the title or summary; a category from keywords (fed, inflation, jobs,
                growth, etf, regulation, hack, listing, unlock, exchange, whales, macro, other)
    kept        data/news.jsonl, one item per line, deduplicated by id, so studies can line news up with the
                recordings later

Item:
    {"id", "kind": "scheduled"|"headline", "source", "title", "url", "t_ms" (published or due), "seen_ms",
     "coins": [...], "category", "impact" (scheduled: High|Medium), "country" (scheduled: USD|JPY), "forecast",
     "previous"}
"""
import email.utils
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

log = logging.getLogger("analytix.news")

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
}
CALENDAR_FEEDS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                  "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
KEEP_IN_MEMORY = 3000

# coin -> (case-insensitive names, case-sensitive tickers)
COIN_WORDS = {
    "BTC": (r"bitcoin", r"BTC"),
    "ETH": (r"ether|ethereum", r"ETH"),
    "SOL": (r"solana", r"SOL"),
    "HYPE": (r"hyperliquid", r"HYPE"),
}
CATEGORIES = (                      # first match wins
    ("hack", r"hack|exploit|drain|stolen|breach|attack"),
    ("fed", r"\bfed\b|fomc|powell|federal reserve|rate (cut|hike|decision)|interest rate"),
    ("inflation", r"\bcpi\b|\bppi\b|\bpce\b|inflation"),
    ("jobs", r"payroll|jobs report|jobless|unemployment|employment"),
    ("growth", r"\bgdp\b|retail sales|recession|\bism\b|\bpmi\b"),
    ("etf", r"\betf|exchange-traded"),
    ("regulation", r"\bsec\b|cftc|regulat|lawsuit|court|senate|congress|\bbill\b|ban\b|sanction"),
    ("listing", r"\blist(s|ing|ed)?\b|delist"),
    ("unlock", r"unlock|vesting|airdrop"),
    ("whales", r"whale|treasury|buys \$|acquir|purchase"),
    ("exchange", r"binance|coinbase|kraken|okx|bybit|exchange"),
    ("macro", r"tariff|treasur(y|ies) yield|dollar|stocks|nasdaq|s&p|trump|china|war\b"),
)
MARKET_WIDE = {"fed", "inflation", "jobs", "growth", "macro", "etf", "regulation"}


def tag(text: str) -> tuple[list[str], str]:
    """(coins named, category) for a headline or event title."""
    coins = [c for c, (names, ticker) in COIN_WORDS.items()
             if re.search(rf"\b({names})\b", text, re.I) or re.search(rf"\b\$?{ticker}\b", text)]
    low = text.lower()
    category = next((name for name, rx in CATEGORIES if re.search(rx, low)), "other")
    return coins, category


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _text(el, *names) -> str:
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def parse_rss(xml_text: str, source: str, seen_ms: int) -> list[dict]:
    """RSS 2.0 <item>s or Atom <entry>s -> items. Entries without a date are skipped."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    atom = "{http://www.w3.org/2005/Atom}"
    for el in list(root.iter("item")) + list(root.iter(f"{atom}entry")):
        title = _text(el, "title", f"{atom}title")
        link = _text(el, "link")
        if not link:
            ln = el.find(f"{atom}link")
            link = ln.get("href", "") if ln is not None else ""
        guid = _text(el, "guid", f"{atom}id") or link or title
        when = _text(el, "pubDate", f"{atom}published", f"{atom}updated")
        summary = re.sub(r"<[^>]+>", " ", _text(el, "description", f"{atom}summary"))[:400]
        try:
            dt = email.utils.parsedate_to_datetime(when) if "," in when or when[:3].isalpha() else \
                datetime.fromisoformat(when.replace("Z", "+00:00"))
        except (TypeError, ValueError, IndexError):
            continue
        if not title or dt.tzinfo is None:
            continue
        coins, category = tag(f"{title} {summary}")
        out.append({"id": f"{source}:{guid}", "kind": "headline", "source": source, "title": title, "url": link,
                    "t_ms": _ms(dt), "seen_ms": seen_ms, "coins": coins, "category": category})
    return out


def parse_calendar(payload, seen_ms: int, countries=("USD",), impacts=("High", "Medium")) -> list[dict]:
    """Forex Factory's weekly JSON -> scheduled items (US, High or Medium impact)."""
    out = []
    for ev in payload if isinstance(payload, list) else []:
        if ev.get("country") not in countries or ev.get("impact") not in impacts:
            continue
        try:
            dt = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if dt.tzinfo is None:
            continue
        title = str(ev.get("title", "")).strip()
        _, category = tag(title)
        t = _ms(dt)
        out.append({"id": f"cal:{ev.get('country')}:{title}:{t}", "kind": "scheduled", "source": "Calendar",
                    "country": ev.get("country"),
                    "title": title, "url": "", "t_ms": t, "seen_ms": seen_ms, "coins": [],
                    "category": category if category != "other" else "macro", "impact": ev.get("impact"),
                    "forecast": ev.get("forecast") or None, "previous": ev.get("previous") or None})
    return out


class NewsStore:
    """Every item ever seen, appended to one JSONL file; the newest KEEP_IN_MEMORY in memory, by time."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: dict[str, dict] = {}
        try:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        it = json.loads(line)
                        self.items[it["id"]] = it
                    except (ValueError, KeyError):
                        continue
        except FileNotFoundError:
            pass
        self._trim()

    def add(self, items: list[dict]) -> list[dict]:
        new = [it for it in items if it["id"] not in self.items]
        if not new:
            return []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for it in new:
                    fh.write(json.dumps(it, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("couldn't save news to %s: %s", self.path, exc)
        for it in new:
            self.items[it["id"]] = it
        self._trim()
        return new

    def _trim(self) -> None:
        if len(self.items) > KEEP_IN_MEMORY:
            keep = sorted(self.items.values(), key=lambda it: it["t_ms"])[-KEEP_IN_MEMORY:]
            self.items = {it["id"]: it for it in keep}

    def recent(self, now_ms: int, hours: float, kind: str | None = None) -> list[dict]:
        lo = now_ms - hours * 3_600_000
        return sorted((it for it in self.items.values() if lo <= it["t_ms"] <= now_ms and (kind is None or
                       it["kind"] == kind)), key=lambda it: -it["t_ms"])

    def upcoming(self, now_ms: int, days: float = 7) -> list[dict]:
        hi = now_ms + days * 86_400_000
        return sorted((it for it in self.items.values() if it["kind"] == "scheduled" and now_ms < it["t_ms"] <= hi),
                      key=lambda it: it["t_ms"])

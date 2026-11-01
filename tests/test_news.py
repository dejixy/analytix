"""News (ingestion/news.py, api/news.py): feeds parsed, tagged, kept once, and each coin's reaction measured."""
from types import SimpleNamespace

import numpy as np

from api.news import NewsService, reaction
from engine import calendar
from ingestion.news import NewsStore, parse_calendar, parse_rss, tag
from models.barModel import Bar

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin ETF inflows hit a record as BTC tops $120K</title><link>https://x.test/a</link>
<guid>a-1</guid><pubDate>Sat, 31 Oct 2026 14:05:00 +0000</pubDate><description>&lt;p&gt;Big day&lt;/p&gt;</description></item>
<item><title>Hyperliquid lists a new perp</title><link>https://x.test/b</link><pubDate>Sat, 31 Oct 2026 15:00:00 GMT</pubDate></item>
<item><title>No date here</title><link>https://x.test/c</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Solana network hit by exploit</title><link href="https://y.test/1"/><id>tag:y,1</id>
<updated>2026-10-31T16:00:00Z</updated><summary>funds drained</summary></entry></feed>"""
CAL = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High",
        "forecast": "0.3%", "previous": "0.4%"},
       {"title": "Unemployment Claims", "country": "USD", "date": "2026-11-05T08:30:00-05:00", "impact": "Medium"},
       {"title": "German ZEW", "country": "EUR", "date": "2026-11-10T05:00:00-05:00", "impact": "High"},
       {"title": "Bank Holiday", "country": "USD", "date": "2026-11-11T00:00:00-05:00", "impact": "Holiday"}]


def test_tags():
    assert tag("Ethereum and SOL rally after FOMC") == (["ETH", "SOL"], "fed")
    assert tag("Crypto hype fades") == ([], "other")                      # 'hype' the word isn't HYPE the coin
    assert tag("HYPE jumps 10%")[0] == ["HYPE"]


def test_feeds_parse():
    items = parse_rss(RSS, "Desk", seen_ms=1)
    assert [it["title"][:7] for it in items] == ["Bitcoin", "Hyperli"]   # the undated one is skipped
    assert items[0]["coins"] == ["BTC"] and items[0]["category"] == "etf" and items[0]["id"] == "Desk:a-1"
    assert items[1]["category"] == "listing" and items[1]["coins"] == ["HYPE"]
    atom = parse_rss(ATOM, "Y", seen_ms=1)
    assert atom[0]["coins"] == ["SOL"] and atom[0]["category"] == "hack" and atom[0]["url"] == "https://y.test/1"
    cal = parse_calendar(CAL, seen_ms=1)
    assert [c["title"] for c in cal] == ["CPI m/m", "Unemployment Claims"]   # US, High or Medium only
    assert cal[0]["category"] == "inflation" and cal[0]["forecast"] == "0.3%" and cal[1]["category"] == "jobs"
    assert cal[0]["t_ms"] == 1_794_490_200_000                               # 13:30 UTC


def test_store_keeps_each_item_once(tmp_path):
    st = NewsStore(tmp_path / "news.jsonl")
    items = parse_rss(RSS, "Desk", 1)
    assert len(st.add(items)) == 2 and st.add(items) == []
    again = NewsStore(tmp_path / "news.jsonl")
    assert len(again.items) == 2
    now = max(it["t_ms"] for it in items) + 1
    assert [it["title"][:3] for it in again.recent(now, 24)] == ["Hyp", "Bit"]
    again.add(parse_calendar(CAL, 1))
    assert [it["title"] for it in again.upcoming(now, days=30)] == ["Unemployment Claims", "CPI m/m"]


def test_reaction_from_minute_bars():
    t0 = 1_790_000_000_000 // 60_000 * 60_000
    bars = [Bar(t0 - 1440 * 60_000 + i * 60_000, 60, 100, 100, 100, 100, fills=10) for i in range(1440 - 10)]
    bars += [Bar(t0 - 10 * 60_000 + i * 60_000, 60, 100, 100, 100, 100.0 if i < 9 else 100.2, fills=10) for i in range(10)]
    bars += [Bar(t0 + i * 60_000, 60, 100, 100, 100, 100.2 + 0.01 * (i + 1), fills=40) for i in range(30)]
    r = reaction(bars, t0, now=t0 + 31 * 60_000)
    assert r["before_bps"] == 20.0 and r["after_bps"] == round((100.5 / 100.2 - 1) * 1e4, 1)
    assert r["activity_x"] == 4.0 and r["done"]
    assert reaction(bars, t0 + 3 * 86_400_000, now=t0 + 4 * 86_400_000) is None     # no bars near then: no reading


def test_which_coins_and_the_calendar_reaches_the_forecast(tmp_path):
    rt = SimpleNamespace(mode="replay", coins=["BTC", "ETH"], pipelines={})
    svc = NewsService(rt, path=tmp_path / "news.jsonl")
    assert svc.coins_for({"kind": "headline", "coins": ["ETH", "SOL"], "category": "other"}) == ["ETH"]
    assert svc.coins_for({"kind": "headline", "coins": [], "category": "fed"}) == ["BTC", "ETH"]
    assert svc.coins_for({"kind": "headline", "coins": [], "category": "other"}) == ["BTC"]
    cal = parse_calendar(CAL, 1)
    svc._feed_calendar(cal)
    t = np.array([cal[0]["t_ms"] - 600_000])
    assert calendar.upcoming(t, 1_800_000).tolist() == [True]           # the gauge now expects the CPI release
    calendar.LIVE.clear()

"""
Futures and index quotes from Yahoo Finance's public chart endpoint (delayed about 10 minutes), for the oil roll:
the front and next contract of WTI (CL…NYM) and Brent (BZ…NYM). Free and keyless; if it can't be reached, the roll
panel says so and shows the schedule alone.
"""
import httpx

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Analytix quotes)"}


def parse_chart(payload: dict) -> dict | None:
    """{"px", "t_ms", "prev"} from one chart response, or None."""
    try:
        meta = payload["chart"]["result"][0]["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    px = meta.get("regularMarketPrice")
    if not px:
        return None
    t = meta.get("regularMarketTime")
    return {"px": float(px), "t_ms": int(t) * 1000 if t else None,
            "prev": meta.get("chartPreviousClose") or meta.get("previousClose")}


async def fetch_quotes(symbols: list[str], url: str = CHART_URL) -> tuple[dict[str, dict], dict[str, str]]:
    """({symbol: quote}, {symbol: error}) for each symbol."""
    got, errors = {}, {}
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as http:
        for s in symbols:
            try:
                r = await http.get(url.format(symbol=s), params={"range": "1d", "interval": "15m"})
                r.raise_for_status()
                q = parse_chart(r.json())
                if q:
                    got[s] = q
                else:
                    errors[s] = "no price in the reply"
            except Exception as exc:
                errors[s] = f"{type(exc).__name__}: {exc}"[:160]
    return got, errors

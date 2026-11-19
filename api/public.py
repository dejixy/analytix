"""
The public link: what someone opening Analytix through Cloudflare (deploy/analytix-link.service) may see.

The app listens only on the server itself. The SSH tunnel reaches it as localhost and sees everything. A request that
came through Cloudflare carries Cloudflare's headers and the link's own host name, and sees only the bets page: the
page, its read-only data (GET /api/bets, /api/bets/combo and /api/bets/combo/code) and the built assets. "/" goes to the page; everything else is a 404 and a
websocket is closed, so the terminal, the Analyzer, settings, alerts and the odds push stay private.
"""
import re

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}
VIA_PROXY = (b"cf-connecting-ip", b"cf-ray", b"cf-visitor", b"cdn-loop", b"x-forwarded-for", b"x-forwarded-host")
OPEN_PATHS = {"/bets", "/api/bets", "/api/bets/combo", "/api/bets/combo/code"}
OPEN_PREFIXES = ("/assets/",)
ROBOTS = b"User-agent: *\nDisallow: /\n"


def is_public(scope: dict) -> bool:
    """True when a request came through the public link rather than the SSH tunnel."""
    headers = dict(scope.get("headers") or [])
    if any(h in headers for h in VIA_PROXY):
        return True
    host = headers.get(b"host", b"").decode("latin-1").strip().lower()
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host      # "localhost:8001" → "localhost"
    return bool(name) and name not in LOCAL_HOSTS


def allowed(method: str, path: str) -> bool:
    return method in ("GET", "HEAD") and (path in OPEN_PATHS or path.startswith(OPEN_PREFIXES))


class PublicGate:
    """ASGI middleware: public requests see the bets page only (marked in the scope so the page can hide its links)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not is_public(scope):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        method, path = scope.get("method", "GET"), scope.get("path", "/")
        if path == "/" and method in ("GET", "HEAD"):
            return await _reply(send, 302, b"", [(b"location", b"/bets")])
        if path == "/robots.txt":
            return await _reply(send, 200, ROBOTS, [(b"content-type", b"text/plain")])
        if not allowed(method, path):
            return await _reply(send, 404, b"Not here. The bets page is at /bets", [(b"content-type", b"text/plain")])
        scope = {**scope, "analytix_public": True}

        async def send_noindex(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"x-robots-tag", b"noindex, nofollow")]}
            await send(msg)
        await self.app(scope, receive, send_noindex)


async def _reply(send, status: int, body: bytes, headers: list) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [*headers, (b"content-length", str(len(body)).encode()), (b"x-robots-tag", b"noindex, nofollow")]})
    await send({"type": "http.response.body", "body": body})


def scrub(text: str) -> str:
    """Error text with any key in a URL blanked (an HTTP error's message carries the URL it asked for)."""
    return re.sub(r"((?:api_?key|apiKey|key|token)=)[^&\s'\"]+", r"\1…", str(text), flags=re.I)


class CacheHeaders:
    """Built files have their content's hash in their names, so a browser (and Cloudflare) may keep them for a year; the
    page itself is always checked, so a new build shows up at once."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        rule = (b"public, max-age=31536000, immutable" if path.startswith("/assets/")
                else b"no-cache" if path in ("/", "/bets", "/terminal", "/analyzer", "/index.html") else None)
        if rule is None:
            return await self.app(scope, receive, send)

        async def send_cached(msg):
            if msg["type"] == "http.response.start" and msg.get("status") == 200:
                msg = {**msg, "headers": [*[h for h in msg.get("headers", []) if h[0].lower() != b"cache-control"],
                                          (b"cache-control", rule)]}
            await send(msg)
        await self.app(scope, receive, send_cached)

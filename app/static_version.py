"""Versioned static URLs: /static/v/<content hash>/js/map.js.

Every deploy that changes a file changes the URL, so no browser or proxy can mix old and new
files. Cloudflare, for one, rewrites our "Cache-Control: no-cache" to a 4-hour max-age, and
a stale common.js next to a new sites.js stopped the Sites page loading. Relative module
imports ("./common.js") resolve under the same versioned prefix, so they're covered too.
Versioned files can then be cached for good.
"""

import hashlib
from pathlib import Path

STATIC_DIR = Path("app/static")
PREFIX = "/static/v/"


def _compute() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(STATIC_DIR)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


VERSION = _compute()


def asset(path: str) -> str:
    """Jinja helper: asset('js/map.js') -> /static/v/<hash>/js/map.js"""
    return f"{PREFIX}{VERSION}/{path.lstrip('/')}"


class VersionedStatic:
    """ASGI middleware: serve /static/v/<any>/<path> from /static/<path>, cached as immutable.

    Any version is accepted, so a page opened before a deploy still loads (current) files.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(PREFIX):
            return await self.app(scope, receive, send)
        _, _, rest = scope["path"][len(PREFIX):].partition("/")
        path = "/static/" + rest
        scope = {**scope, "path": path, "raw_path": path.encode()}

        async def send_cached(msg):
            if msg["type"] == "http.response.start" and msg["status"] == 200:
                headers = [(k, v) for k, v in msg.get("headers", []) if k.lower() != b"cache-control"]
                msg = {**msg, "headers": headers + [(b"cache-control", b"public, max-age=31536000, immutable")]}
            await send(msg)

        await self.app(scope, receive, send_cached)

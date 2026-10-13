"""FastAPI entry point. Run with a single worker: the pollers and SSE bus live in-process."""

import asyncio
import logging
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import platform_settings, security_guard
from app.config import get_settings
from app.deps import NotAuthenticated, render
from app.net import client_ip
from app.routers import account, api, auth, guards, links, pages, platform, share, site_models, sso, stream, tenants, tiles
from app.services import arming
from app.static_version import VersionedStatic
from app.services.poller import manager

# Paths that answer errors as JSON instead of an HTML page / login redirect.
API_PREFIXES = ("/api/", "/media/", "/tiles/", "/branding/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


# Sign-in pages a banned IP may not use. Signed-in sessions are never cut off by a ban.
BAN_GUARDED = ("/login", "/setup", "/forgot", "/reset", "/auth/", "/a/", "/media/a/", "/g/")   # /a/, /g/: texted links


class BanGate:
    """Answers banned IPs with the blocked page on the sign-in pages, and on /api/ calls that have no
    signed-in session. Pure ASGI, so it costs nothing on the live stream and media. Sits inside
    SessionMiddleware (it reads the session) and fails open (security_guard.active_ban)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path.startswith(BAN_GUARDED) or (path.startswith("/api/") and not scope.get("session", {}).get("uid")):
                request = Request(scope)
                ban = await security_guard.active_ban(client_ip(request))
                if ban is not None:
                    response = auth.banned_page(request, ban, api=path.startswith("/api/"))
                    return await response(scope, receive, send)
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    await platform_settings.refresh()
    if get_settings().start_pollers:
        await manager.start_all()
        tasks.append(asyncio.create_task(arming.run_scheduler(), name="arming-scheduler"))
        tasks.append(asyncio.create_task(security_guard.run_sweeper(), name="security-sweeper"))
        from app.routers.tiles import run_prefetcher
        tasks.append(asyncio.create_task(run_prefetcher(), name="imagery-prefetch"))
        await site_models.resume()
        from app.services import trackers
        tasks.append(asyncio.create_task(trackers.run(), name="guard-trackers"))
    yield
    await site_models.shutdown()
    for t in tasks:
        t.cancel()
    from app import sms
    await sms.shutdown()
    await manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TWG Alarm Portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BanGate)             # added first = runs inside SessionMiddleware
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="twg_portal",
                       max_age=settings.session_max_age_s, same_site="lax", https_only=settings.cookie_secure)

    @app.middleware("http")
    async def https_only(request: Request, call_next):
        # The Cloudflare Tunnel also answers plain http:// (2026-10-13: a phone signing in that way never kept the
        # secure session cookie, so every sign-in failed the CSRF check, and passwords would cross the internet
        # unencrypted). Cloudflare says how the visitor connected in CF-Visitor: send http visitors to https first.
        visitor = request.headers.get("cf-visitor", "").replace(" ", "")
        if '"scheme":"http"' in visitor:
            url = request.url.replace(scheme="https", port=None)
            return RedirectResponse(str(url), 308 if request.method not in ("GET", "HEAD") else 301)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Our own JS/CSS change on deploy; make browsers revalidate (cheap 304s via ETag).
        if request.url.path.startswith(("/static/js/", "/static/css/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith(API_PREFIXES):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login" if target == "/" else f"/login?next={quote(target)}", 303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(API_PREFIXES) or exc.status_code < 400:
            return await http_exception_handler(request, exc)
        response = render(request, "error.html", status=exc.status_code, message=exc.detail)
        response.status_code = exc.status_code
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        # Browsers ask for /favicon.ico on their own (pages also link it): the TWG eye.
        return FileResponse("app/static/favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.add_middleware(VersionedStatic)     # outermost: /static/v/<hash>/... -> /static/..., cached for good
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(stream.router)
    app.include_router(tenants.router)
    app.include_router(platform.router)
    app.include_router(account.router)
    app.include_router(sso.router)
    app.include_router(tiles.router)
    app.include_router(links.router)
    app.include_router(site_models.router)
    app.include_router(guards.router)
    app.include_router(share.router)
    return app


app = create_app()

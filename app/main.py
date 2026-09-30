"""FastAPI entry point. Run with a single worker: the pollers and SSE bus live in-process."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.deps import NotAuthenticated, render
from app.routers import api, auth, pages, stream, tiles
from app.services import arming
from app.static_version import VersionedStatic
from app.services.poller import manager

# Paths that answer errors as JSON instead of an HTML page / login redirect.
API_PREFIXES = ("/api/", "/media/", "/tiles/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if get_settings().start_pollers:
        await manager.start_all()
        scheduler = asyncio.create_task(arming.run_scheduler(), name="arming-scheduler")
    yield
    if scheduler:
        scheduler.cancel()
    await manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TWG Alarm Portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="twg_portal",
                       max_age=settings.session_max_age_s, same_site="lax", https_only=settings.cookie_secure)

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
        return RedirectResponse("/login", 303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(API_PREFIXES) or exc.status_code < 400:
            return await http_exception_handler(request, exc)
        response = render(request, "error.html", status=exc.status_code, message=exc.detail)
        response.status_code = exc.status_code
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.add_middleware(VersionedStatic)     # outermost: /static/v/<hash>/... -> /static/..., cached for good
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(stream.router)
    app.include_router(tiles.router)
    return app


app = create_app()

"""FastAPI entry point. Run with a single worker: the pollers and SSE bus live in-process."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.deps import NotAuthenticated, render
from app.routers import api, auth, pages, stream
from app.services.poller import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_settings().start_pollers:
        await manager.start_all()
    yield
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
        return response

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith(("/api/", "/media/")):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", 303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/media/")) or exc.status_code < 400:
            return await http_exception_handler(request, exc)
        return render(request, "error.html", status=exc.status_code, message=exc.detail)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(stream.router)
    return app


app = create_app()

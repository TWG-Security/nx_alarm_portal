"""Request dependencies: current user, admin gate, CSRF check, templates."""

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import User
from app.permissions import load_permissions
from app.security import new_csrf_token
from app.static_version import asset

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["asset"] = asset

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class NotAuthenticated(Exception):
    pass


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method not in UNSAFE_METHODS:
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        supplied = (await request.form()).get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        raise HTTPException(403, "CSRF token missing or invalid — reload the page")


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise NotAuthenticated()
    user = await db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    user._perms = await load_permissions(db, user)
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin role required")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def render(request: Request, name: str, user: User | None = None, **ctx):
    settings = get_settings()
    return templates.TemplateResponse(request, name, {
        "user": user,
        "csrf_token": ensure_csrf(request),
        "clip_config": {"pre": settings.clip_pre_s, "post": settings.clip_post_s, "max": settings.clip_max_window_s},
        "map_config": {
            "tileUrl": "/tiles/{z}/{x}/{y}.png",
            "attribution": settings.map_tile_attribution,
            "maxZoom": settings.map_tile_max_zoom,
        },
        **ctx,
    })

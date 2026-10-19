"""Request dependencies: current user, admin gate, CSRF check, templates."""

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings, sessions
from app.config import get_settings
from app.db import get_db
from app.models import Tenant, User
from app.net import client_ip as _client_ip
from app.permissions import load_permissions
from app.security import new_csrf_token
from app.security_guard import note_session_ip
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


def bearer_device_token(request: Request) -> str:
    """The iPhone app's device token from "Authorization: Bearer twgm_…" ("" if none)."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    return token if token.startswith(sessions.MOBILE_PREFIX) else ""


async def csrf_protect(request: Request) -> None:
    # MCP calls and the iPhone app's Bearer requests carry no cookie, so there's nothing to forge: a browser can't
    # add an Authorization header to a cross-site request. A bad token is refused by current_user (401).
    if request.method not in UNSAFE_METHODS or request.scope.get("twg_mcp") or bearer_device_token(request):
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        supplied = (await request.form()).get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        raise HTTPException(403, "CSRF token missing or invalid — reload the page")


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    mcp = request.scope.get("twg_mcp")
    if mcp is not None:
        # A tool call from the MCP server (app/mcp_server.py), made inside this process after it checked the API
        # token. Only the server puts this key in the ASGI scope; nothing from outside can.
        user = await db.get(User, mcp["user_id"])
        tenant = await db.get(Tenant, user.tenant_id) if user is not None else None
        if user is None or not user.is_active or user.deleted_at is not None or tenant is None or not tenant.is_active:
            raise NotAuthenticated()
        user._tenant = tenant
        user._perms = await load_permissions(db, user, tenant)
        return user
    token = bearer_device_token(request)
    if token:
        # The iPhone app (app/routers/mobile.py): the token is the sign-in; cookies are ignored. Always the user's
        # own company (no company switcher); request.scope["twg_mobile"] tells the stream which device this is.
        found = await sessions.check_mobile(request, db, token)
        if found is None:
            raise NotAuthenticated()
        user, tenant, row = found
        request.scope["twg_mobile"] = {"sid": row.id, "tv": user.token_version or 0}
        user._tenant = tenant
        user._perms = await load_permissions(db, user, tenant)
        note_session_ip(client_ip(request))   # phones move between Wi-Fi and cellular addresses: never banned while in use
        return user
    uid = request.session.get("uid")
    if not uid:
        raise NotAuthenticated()
    user = await db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:      # the company's sign-in is disabled
        request.session.clear()
        raise NotAuthenticated()
    if await sessions.check(request, db, user):         # revoked, signed out everywhere, or expired
        request.session.clear()
        raise NotAuthenticated()
    user._tenant = tenant
    user._perms = await load_permissions(db, user, tenant)
    note_session_ip(client_ip(request))       # IPs with a live session are never banned (security_guard)
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin role required")
    return user


def _brand(user, scope) -> dict:
    """Top-bar branding: the company in view (the user's own company outside support mode)."""
    tenant = (scope.tenant if scope else None) or getattr(user, "_tenant", None)
    if scope is not None and scope.mode == "all":
        tenant = getattr(user, "_tenant", None)
    if tenant is None:
        return {"name": "TWG Security", "logo": None, "powered_by": False}
    return {"name": tenant.label, "logo": f"/branding/{tenant.id}/logo" if tenant.logo else None,
            "powered_by": not tenant.is_platform}


def client_ip(request: Request) -> str:
    """The real client address (CF-Connecting-IP only from the trusted tunnel connector): app/net.py."""
    return _client_ip(request)


def render(request: Request, name: str, user: User | None = None, scope=None, **ctx):
    """scope: app.scope.Scope for the top bar's company branding and switcher (pages pass it)."""
    settings = get_settings()
    return templates.TemplateResponse(request, name, {
        "user": user,
        "scope": scope,
        "brand": _brand(user, scope),
        "csrf_token": ensure_csrf(request),
        "clip_config": {"pre": settings.clip_pre_s, "post": settings.clip_post_s, "max": settings.clip_max_window_s},
        "session_config": {"idleMin": 0 if user is not None and user.monitoring_station else platform_settings.current().idle_timeout_min},
        "map_config": {
            "tileUrl": "/tiles/{z}/{x}/{y}.png",
            "attribution": settings.map_tile_attribution,
            "maxZoom": settings.map_tile_max_zoom,
            "satUrl": "/tiles/sat/{z}/{x}/{y}.jpg",
            "satMaxZoom": settings.sat_tile_max_zoom,
            "satAttribution": settings.sat_tile_attribution,
            "google3dKey": settings.google_map_tiles_key or None,
        },
        **ctx,
    })

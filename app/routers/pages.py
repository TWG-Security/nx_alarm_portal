"""Server-rendered pages. Data is loaded client-side from /api and kept live over SSE."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user, render, require_admin
from app.models import Site, User
from app.scope import Scope, get_scope
from app.services.serialize import site_dict

router = APIRouter()


@router.get("/")
async def map_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    return render(request, "map.html", user, scope=scope, page="map")


@router.get("/alarms")
async def alarms_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    return render(request, "alarms.html", user, scope=scope, page="alarms")


@router.get("/sites")
async def sites_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    return render(request, "sites.html", user, scope=scope, page="sites")


@router.get("/sites/new")
async def site_new(request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope)):
    return render(request, "site_form.html", user, scope=scope, page="sites", site=None)


@router.get("/sites/{site_id}/edit")
async def site_edit(site_id: int, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                    db: AsyncSession = Depends(get_db)):
    site = await db.scalar(select(Site).where(Site.id == site_id, scope.where(Site.tenant_id)))
    if site is None:
        raise HTTPException(404)
    return render(request, "site_form.html", user, scope=scope, page="sites", site=site_dict(site))


@router.get("/audit")
async def audit_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    return render(request, "audit.html", user, scope=scope, page="audit")


@router.get("/settings")
async def settings_page(request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope)):
    return render(request, "settings.html", user, scope=scope, page="settings")


@router.get("/users")
async def users_page(request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope)):
    return render(request, "users.html", user, scope=scope, page="users")


@router.get("/companies")
async def companies_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    if not user.can("platform.view"):
        raise HTTPException(403, "Only TWG staff can see all companies")
    return render(request, "companies.html", user, scope=scope, page="companies")

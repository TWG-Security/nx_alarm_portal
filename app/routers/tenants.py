"""Companies (tenants): TWG's multi-company view, company management, and branding.

- POST /api/scope            platform.view: switch the view (own company / all companies / one company)
- GET  /api/tenants          platform.view: every company with live health numbers
- POST /api/tenants          platform.manage: create a company and its first admin
- PUT  /api/tenants/{id}     display name (platform.manage, or that company's admin); sign-in on/off (platform.manage)
- POST/DELETE /api/tenants/{id}/logo   same rule as the display name
- GET  /branding/{id}/logo   the logo, for anyone who can see that company
"""

import io

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user
from app.models import Alarm, Site, Tenant, User
from app.permissions import require
from app.scope import Scope, get_scope
from app.security import hash_password

router = APIRouter(dependencies=[Depends(csrf_protect)])

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MAX_PX = (600, 160)          # re-encoded to a PNG no bigger than this


class ScopeIn(BaseModel):
    scope: str = Field(pattern=r"^(own|all|\d+)$")


@router.post("/api/scope")
async def set_scope(body: ScopeIn, request: Request, user: User = Depends(require("platform.view")),
                    db: AsyncSession = Depends(get_db)):
    if body.scope.isdigit():
        tenant = await db.get(Tenant, int(body.scope))
        if tenant is None:
            raise HTTPException(404, "Company not found")
        if tenant.id == user.tenant_id:
            body.scope = "own"
        else:
            # Entering a customer's portal is recorded in their audit log, so they can see TWG looked.
            audit(db, tenant.id, "support.viewed", user_id=user.id, ip=client_ip(request))
            await db.commit()
    request.session["scope"] = body.scope
    return {"scope": body.scope}


@router.get("/api/tenants")
async def list_tenants(user: User = Depends(require("platform.view")), db: AsyncSession = Depends(get_db)):
    tenants = (await db.scalars(select(Tenant).order_by(Tenant.kind.desc(), Tenant.name))).all()
    sites = {tid: (n, off, dis) for tid, n, off, dis in (await db.execute(
        select(Site.tenant_id, func.count(),
               func.sum(case((Site.status.in_(("offline", "auth_error")), 1), else_=0)),
               func.sum(case((Site.armed.is_(False), 1), else_=0)))
        .where(Site.archived_at.is_(None)).group_by(Site.tenant_id))).all()}
    open_counts: dict[int, dict[int, int]] = {}
    for tid, pri, n in (await db.execute(select(Alarm.tenant_id, Alarm.priority, func.count())
                                         .where(Alarm.state == "new").group_by(Alarm.tenant_id, Alarm.priority))).all():
        open_counts.setdefault(tid, {})[pri] = n
    last = dict((await db.execute(select(Alarm.tenant_id, func.max(Alarm.event_ts_ms)).group_by(Alarm.tenant_id))).all())
    users = dict((await db.execute(select(User.tenant_id, func.count()).where(User.is_active.is_(True))
                                   .group_by(User.tenant_id))).all())
    out = []
    for t in tenants:
        n, off, dis = sites.get(t.id, (0, 0, 0))
        c = open_counts.get(t.id, {})
        out.append({"id": t.id, "name": t.name, "display_name": t.display_name, "label": t.label, "kind": t.kind,
                    "is_active": t.is_active, "has_logo": bool(t.logo), "own": t.id == user.tenant_id,
                    "sites": n, "sites_offline": int(off or 0), "sites_disarmed": int(dis or 0),
                    "open_critical": c.get(1, 0), "open_alarm": c.get(2, 0), "open_warning": c.get(3, 0),
                    "last_alarm_ms": last.get(t.id), "users": users.get(t.id, 0)})
    return out


class TenantIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    display_name: str = Field(default="", max_length=200)
    admin_email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    admin_name: str = Field(default="", max_length=200)
    admin_password: str = Field(min_length=12, max_length=200)


@router.post("/api/tenants")
async def create_tenant(body: TenantIn, request: Request, user: User = Depends(require("platform.manage")),
                        db: AsyncSession = Depends(get_db)):
    tenant = Tenant(name=body.name.strip(), display_name=body.display_name.strip(), kind="customer")
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, f"A company named '{body.name}' already exists") from exc
    admin = User(tenant_id=tenant.id, email=body.admin_email.strip().lower(), display_name=body.admin_name.strip(),
                 password_hash=hash_password(body.admin_password), role="admin")
    db.add(admin)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "A user with that email already exists (emails are unique across all companies)") from exc
    ip = client_ip(request)
    audit(db, user.tenant_id, "tenant.created", user_id=user.id, ip=ip, tenant=tenant.name, company_id=tenant.id,
          admin=admin.email)
    audit(db, tenant.id, "tenant.created", user_id=user.id, ip=ip, admin=admin.email)
    await db.commit()
    return {"id": tenant.id, "name": tenant.name, "admin_id": admin.id}


class TenantUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


def _can_brand(user: User, tenant_id: int) -> bool:
    return user.can("platform.manage") or (user.is_admin and user.tenant_id == tenant_id)


@router.get("/api/tenants/{tenant_id}/branding")
async def get_branding(tenant_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not _can_brand(user, tenant_id):
        raise HTTPException(404)
    return {"id": tenant.id, "name": tenant.name, "display_name": tenant.display_name, "label": tenant.label,
            "has_logo": bool(tenant.logo), "is_platform": tenant.is_platform}


@router.put("/api/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, body: TenantUpdate, request: Request, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not _can_brand(user, tenant_id):
        raise HTTPException(404)
    fields = []
    if body.display_name is not None and body.display_name.strip() != tenant.display_name:
        tenant.display_name = body.display_name.strip()
        fields.append("display_name")
    if body.is_active is not None and body.is_active != tenant.is_active:
        if not user.can("platform.manage"):
            raise HTTPException(403, "Only TWG can turn a company's sign-in on or off")
        if tenant.is_platform:
            raise HTTPException(400, "The platform company can't be disabled")
        tenant.is_active = body.is_active
        fields.append("is_active")
    if fields:
        audit(db, tenant.id, "tenant.updated", user_id=user.id, ip=client_ip(request), fields=fields,
              is_active=tenant.is_active, display_name=tenant.display_name)
        await db.commit()
    return {"id": tenant.id, "display_name": tenant.display_name, "is_active": tenant.is_active, "label": tenant.label}


@router.post("/api/tenants/{tenant_id}/logo")
async def upload_logo(tenant_id: int, request: Request, file: UploadFile = File(...),
                      user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not _can_brand(user, tenant_id):
        raise HTTPException(404)
    raw = await file.read(LOGO_MAX_BYTES + 1)
    if len(raw) > LOGO_MAX_BYTES:
        raise HTTPException(413, "Logo is larger than 2 MB")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP"):
                raise HTTPException(400, "Use a PNG, JPEG or WebP logo")
            im.load()
            # Re-encode: strips metadata, bounds the size, and never serves the uploaded bytes as-is.
            img = im.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "That file isn't a readable image") from exc
    img.thumbnail(LOGO_MAX_PX)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    tenant.logo, tenant.logo_type = buf.getvalue(), "image/png"
    audit(db, tenant.id, "tenant.logo", user_id=user.id, ip=client_ip(request), size=len(tenant.logo))
    await db.commit()
    return {"ok": True, "width": img.width, "height": img.height}


@router.delete("/api/tenants/{tenant_id}/logo")
async def delete_logo(tenant_id: int, request: Request, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not _can_brand(user, tenant_id):
        raise HTTPException(404)
    tenant.logo, tenant.logo_type = None, ""
    audit(db, tenant.id, "tenant.logo", user_id=user.id, ip=client_ip(request), removed=True)
    await db.commit()
    return {"ok": True}


@router.get("/branding/{tenant_id}/logo")
async def tenant_logo(tenant_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    if not (scope.sees(tenant_id) or tenant_id == scope.own_id or scope.user.can("platform.view")):
        raise HTTPException(404)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.logo:
        raise HTTPException(404)
    return Response(tenant.logo, media_type=tenant.logo_type or "image/png",
                    headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

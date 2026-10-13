"""Guards & vehicles: live GPS positions on the map (everyone, per company) and their setup on the Sites page
(each company's admins, with the company's own InControl2 API client; TWG in support mode like elsewhere).

  GET    /api/trackers                     the company's trackers with a short trail (map pages)
  GET    /api/trackers/{id}/trail          position history (?minutes=, at most TRAIL_KEEP_DAYS)
  GET    /api/tracking                     the company's InControl2 settings + its trackers (admins)
  PUT    /api/tracking/source              Client ID / Secret / on-off / interval
  POST   /api/tracking/test                get a token and count the organizations it sees
  GET    /api/tracking/devices             GPS routers in those organizations, to pick from
  POST   /api/tracking/trackers            track a router
  PUT    /api/tracking/trackers/{id}       rename / kind / on-off
  DELETE /api/tracking/trackers/{id}
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import peplink
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, require_admin
from app.models import Tenant, Tracker, TrackerPoint, TrackerSource, User
from app.scope import Scope, get_scope
from app.security import encrypt
from app.services import trackers as svc
from app.services.bus import bus

router = APIRouter(dependencies=[Depends(csrf_protect)])
TRAIL_DEFAULT_MIN = 30
TRAIL_MAX_POINTS = 5000


async def _trails(db: AsyncSession, ids: list[int], minutes: int) -> dict[int, list]:
    if not ids:
        return {}
    since = svc.utcnow() - timedelta(minutes=minutes)
    out: dict[int, list] = {i: [] for i in ids}
    rows = (await db.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id.in_(ids), TrackerPoint.ts >= since)
                             .order_by(TrackerPoint.ts.desc()).limit(TRAIL_MAX_POINTS))).all()
    for p in reversed(rows):
        out[p.tracker_id].append([round(p.lat, 7), round(p.lng, 7), svc.aware(p.ts).isoformat()])
    return out


@router.get("/api/trackers")
async def list_trackers(trail_min: int = Query(TRAIL_DEFAULT_MIN, ge=0, le=24 * 60), scope: Scope = Depends(get_scope),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Tracker).where(scope.where(Tracker.tenant_id), Tracker.enabled.is_(True))
                             .order_by(Tracker.name))).all()
    trails = await _trails(db, [t.id for t in rows], trail_min) if trail_min else {}
    return [{**svc.tracker_dict(t), "trail": trails.get(t.id, [])} for t in rows]


@router.get("/api/trackers/{tracker_id}/trail")
async def tracker_trail(tracker_id: int, minutes: int = Query(60, ge=1, le=svc.TRAIL_KEEP_DAYS * 24 * 60),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await db.scalar(select(Tracker).where(Tracker.id == tracker_id, scope.where(Tracker.tenant_id)))
    if t is None:
        raise HTTPException(404)
    return {"id": t.id, "points": (await _trails(db, [t.id], minutes))[t.id]}


# ------------------------------------------------------------------------- setup (Sites page, admins)

def _company(scope: Scope) -> Tenant:
    from app.routers.api import _one_company
    return _one_company(scope)


async def _source(db: AsyncSession, tenant_id: int, create: bool = False) -> TrackerSource | None:
    src = await db.scalar(select(TrackerSource).where(TrackerSource.tenant_id == tenant_id, TrackerSource.kind == "peplink"))
    if src is None and create:
        src = TrackerSource(tenant_id=tenant_id, kind="peplink")
        db.add(src)
        await db.flush()
    return src


async def _view(db: AsyncSession, tenant: Tenant) -> dict:
    src = await _source(db, tenant.id)
    trackers = (await db.scalars(select(Tracker).where(Tracker.tenant_id == tenant.id).order_by(Tracker.name))).all()
    iso = lambda d: svc.aware(d).isoformat() if d else None  # noqa: E731
    return {"company": tenant.label, "enabled": bool(src and src.enabled), "client_id": src.client_id if src else "",
            "secret_set": bool(src and src.secret_enc), "poll_s": src.poll_s if src else 30,
            "last_ok_at": iso(src.last_ok_at) if src else None, "last_error": src.last_error if src else "",
            "last_error_at": iso(src.last_error_at) if src else None,
            "trackers": [{**svc.tracker_dict(t), "ic2_org": t.ic2_org, "ic2_group": t.ic2_group, "ic2_device": t.ic2_device}
                         for t in trackers]}


@router.get("/api/tracking")
async def get_tracking(user: User = Depends(require_admin), scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    return await _view(db, _company(scope))


class SourceIn(BaseModel):
    enabled: bool
    client_id: str = Field(default="", max_length=200)
    secret: str | None = Field(default=None, max_length=500)       # None = keep; "" = remove
    poll_s: int = Field(default=30, ge=svc.POLL_MIN_S, le=600)


@router.put("/api/tracking/source")
async def put_source(body: SourceIn, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                     db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, scope.write_tenant())
    src = await _source(db, tenant.id, create=True)
    cid = body.client_id.strip()
    secret_set = bool(src.secret_enc) if body.secret is None else bool(body.secret.strip())
    if body.enabled and not (cid and secret_set):
        raise HTTPException(400, "Enter the InControl2 Client ID and Client Secret before switching tracking on.")
    changed = []
    for k, v in (("enabled", body.enabled), ("client_id", cid), ("poll_s", body.poll_s)):
        if getattr(src, k) != v:
            setattr(src, k, v)
            changed.append(k)
    if body.secret is not None:
        src.secret_enc = encrypt(body.secret.strip()) if body.secret.strip() else ""
        changed.append("secret")
    if {"client_id", "secret"} & set(changed):
        src.last_ok_at, src.last_error, src.last_error_at = None, "", None
    if changed:
        audit(db, tenant.id, "tracking.settings", user_id=user.id, ip=client_ip(request), fields=changed)   # names, never values
    await db.commit()
    svc.wake()
    return await _view(db, tenant)


async def _client(db: AsyncSession, tenant_id: int) -> peplink.Client:
    src = await _source(db, tenant_id)
    client = svc.client_for(src) if src else None
    if client is None:
        raise HTTPException(400, "Save your InControl2 Client ID and Client Secret first.")
    return client


@router.post("/api/tracking/test")
async def test_source(user: User = Depends(require_admin), scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    client = await _client(db, scope.write_tenant())
    await db.commit()
    try:
        orgs = await client.organizations()
    except peplink.PeplinkError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "organizations": [o["name"] for o in orgs]}


@router.get("/api/tracking/devices")
async def find_devices(user: User = Depends(require_admin), scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Routers that have reported a real GPS position, across every organization these credentials see."""
    tenant_id = scope.write_tenant()
    client = await _client(db, tenant_id)
    tracked = {(t.ic2_org, t.ic2_device): t.name for t in (await db.scalars(select(Tracker).where(Tracker.tenant_id == tenant_id))).all()}
    await db.commit()                        # don't hold the connection while IC2 answers
    out, problems = [], []
    try:
        orgs = await client.organizations()
    except peplink.PeplinkError as e:
        raise HTTPException(502, str(e)) from None
    for org in orgs:
        try:
            devices = await client.org_locations(org["id"])
        except peplink.PeplinkError as e:
            problems.append(f"{org['name']}: {e}")
            continue
        for d in devices:
            fix = peplink.newest_fix(d["points"], "UTC")           # the org-wide list's ts is UTC (a cached position)
            if fix is None:
                continue
            out.append({"org": org["id"], "org_name": org["name"], "group": d["group_id"], "device": d["id"], "serial": d["sn"],
                        "name": d["name"], "online": d["status"] == "online", "product": d["product_name"],
                        "lat": fix.lat, "lng": fix.lng, "fix_at": fix.at.isoformat(),
                        "tracked_as": tracked.get((org["id"], d["id"]))})
    out.sort(key=lambda x: (not x["online"], x["org_name"].lower(), (x["name"] or "").lower()))
    return {"devices": out, "problems": problems}


class TrackerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="vehicle", pattern="^(vehicle|guard)$")
    org: str = Field(min_length=1, max_length=32)
    group: int
    device: int
    serial: str = Field(default="", max_length=40)
    device_name: str = Field(default="", max_length=200)


@router.post("/api/tracking/trackers")
async def add_tracker(body: TrackerIn, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, scope.write_tenant())
    if await db.scalar(select(Tracker).where(Tracker.tenant_id == tenant.id, Tracker.ic2_org == body.org, Tracker.ic2_device == body.device)):
        raise HTTPException(409, "That router is already tracked")
    t = Tracker(tenant_id=tenant.id, name=body.name.strip(), kind=body.kind, ic2_org=body.org, ic2_group=body.group,
                ic2_device=body.device, serial=body.serial, device_name=body.device_name)
    db.add(t)
    await db.flush()
    audit(db, tenant.id, "tracker.added", user_id=user.id, ip=client_ip(request), name=t.name, serial=t.serial)
    await db.commit()
    bus.publish(t.tenant_id, "tracker.moved", svc.tracker_dict(t))
    svc.wake()
    return await _view(db, tenant)


class TrackerEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    kind: str | None = Field(default=None, pattern="^(vehicle|guard)$")
    enabled: bool | None = None


async def _tracker(db: AsyncSession, scope: Scope, tracker_id: int) -> Tracker:
    t = await db.scalar(select(Tracker).where(Tracker.id == tracker_id, scope.where(Tracker.tenant_id)))
    if t is None:
        raise HTTPException(404)
    scope.require_write(t.tenant_id)
    return t


@router.put("/api/tracking/trackers/{tracker_id}")
async def edit_tracker(tracker_id: int, body: TrackerEdit, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await _tracker(db, scope, tracker_id)
    changed = []
    for k in ("name", "kind", "enabled"):
        v = getattr(body, k)
        if v is not None and getattr(t, k) != (v.strip() if isinstance(v, str) else v):
            setattr(t, k, v.strip() if isinstance(v, str) else v)
            changed.append(k)
    if changed:
        audit(db, t.tenant_id, "tracker.updated", user_id=user.id, ip=client_ip(request), name=t.name, fields=changed)
    await db.commit()
    bus.publish(t.tenant_id, "tracker.moved" if t.enabled else "tracker.removed",
                svc.tracker_dict(t) if t.enabled else {"id": t.id})
    return await _view(db, await db.get(Tenant, t.tenant_id))


@router.delete("/api/tracking/trackers/{tracker_id}")
async def delete_tracker(tracker_id: int, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                         db: AsyncSession = Depends(get_db)):
    t = await _tracker(db, scope, tracker_id)
    tenant_id = t.tenant_id
    await db.execute(TrackerPoint.__table__.delete().where(TrackerPoint.tracker_id == t.id))
    audit(db, tenant_id, "tracker.removed", user_id=user.id, ip=client_ip(request), name=t.name, serial=t.serial)
    await db.delete(t)
    await db.commit()
    bus.publish(tenant_id, "tracker.removed", {"id": tracker_id})
    return await _view(db, await db.get(Tenant, tenant_id))

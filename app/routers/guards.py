"""Guard tracking: trackers on the map (everyone, per company) and their setup (Platform page, TWG).

  GET    /api/trackers                     the company's trackers with a short trail (map pages)
  GET    /api/trackers/{id}/trail          position history (?minutes=, at most TRAIL_KEEP_DAYS)
  GET    /api/platform/peplink             InControl2 settings + every tracker (platform.view)
  PUT    /api/platform/peplink             client id / secret / on-off / interval (platform.manage)
  POST   /api/platform/peplink/test        get a token and count the organizations
  GET    /api/platform/peplink/devices     GPS routers across the organizations, to pick from
  POST   /api/platform/trackers            track a router for a company
  PUT    /api/platform/trackers/{id}       rename / kind / company / on-off
  DELETE /api/platform/trackers/{id}
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
from app.deps import client_ip, csrf_protect
from app.models import PlatformSettings, Tenant, Tracker, TrackerPoint, User
from app.permissions import require
from app.scope import Scope, get_scope
from app.security import encrypt
from app.services import trackers as svc
from app.services.bus import bus

router = APIRouter(dependencies=[Depends(csrf_protect)])
VIEW, MANAGE = require("platform.view"), require("platform.manage")
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


# ------------------------------------------------------------------------------------- Platform page

async def _row(db: AsyncSession) -> PlatformSettings:
    from app import platform_settings
    return await platform_settings.get_row(db)


async def _admin_view(db: AsyncSession, row: PlatformSettings) -> dict:
    names = {t.id: t.label for t in (await db.scalars(select(Tenant))).all()}
    trackers = (await db.scalars(select(Tracker).order_by(Tracker.name))).all()
    return {"enabled": row.peplink_enabled, "client_id": row.peplink_client_id, "secret_set": bool(row.peplink_secret_enc),
            "poll_s": row.peplink_poll_s, "last_ok_at": svc.aware(row.peplink_last_ok_at).isoformat() if row.peplink_last_ok_at else None,
            "last_error": row.peplink_last_error,
            "last_error_at": svc.aware(row.peplink_last_error_at).isoformat() if row.peplink_last_error_at else None,
            "companies": [{"id": i, "name": n} for i, n in sorted(names.items(), key=lambda x: x[1].lower())],
            "trackers": [{**svc.tracker_dict(t), "company": names.get(t.tenant_id, ""), "ic2_org": t.ic2_org,
                          "ic2_group": t.ic2_group, "ic2_device": t.ic2_device} for t in trackers]}


@router.get("/api/platform/peplink")
async def get_peplink(user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    return await _admin_view(db, await _row(db))


class PeplinkIn(BaseModel):
    enabled: bool
    client_id: str = Field(default="", max_length=200)
    secret: str | None = Field(default=None, max_length=500)       # None = keep; "" = remove
    poll_s: int = Field(default=30, ge=svc.POLL_MIN_S, le=600)


@router.put("/api/platform/peplink")
async def put_peplink(body: PeplinkIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    row = await _row(db)
    cid = body.client_id.strip()
    secret_set = bool(row.peplink_secret_enc) if body.secret is None else bool(body.secret.strip())
    if body.enabled and not (cid and secret_set):
        raise HTTPException(400, "Enter the InControl2 Client ID and Client Secret before switching tracking on.")
    changed = []
    for k, col, v in (("enabled", "peplink_enabled", body.enabled), ("client_id", "peplink_client_id", cid),
                      ("poll_s", "peplink_poll_s", body.poll_s)):
        if getattr(row, col) != v:
            setattr(row, col, v)
            changed.append(k)
    if body.secret is not None:
        row.peplink_secret_enc = encrypt(body.secret.strip()) if body.secret.strip() else ""
        changed.append("secret")
    if {"client_id", "secret"} & set(changed):
        row.peplink_last_ok_at, row.peplink_last_error, row.peplink_last_error_at = None, "", None
    if changed:
        row.updated_by_id = user.id
        audit(db, user.tenant_id, "platform.settings", user_id=user.id, ip=client_ip(request), section="peplink", fields=changed)
    await db.commit()
    svc.wake()
    return await _admin_view(db, row)


def _client(row: PlatformSettings) -> peplink.Client:
    client = svc.client_for(row)
    if client is None:
        raise HTTPException(400, "Save the InControl2 Client ID and Client Secret first.")
    return client


@router.post("/api/platform/peplink/test")
async def test_peplink(user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    client = _client(await _row(db))
    await db.commit()
    try:
        orgs = await client.organizations()
    except peplink.PeplinkError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "organizations": [o["name"] for o in orgs]}


@router.get("/api/platform/peplink/devices")
async def peplink_devices(user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    """Routers that have reported a GPS position, across every organization the IC2 account can see."""
    client = _client(await _row(db))
    tracked = {(t.ic2_org, t.ic2_device): t.name for t in (await db.scalars(select(Tracker))).all()}
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
            fix = peplink.newest_fix(d["points"], None)
            if fix is None:
                continue
            out.append({"org": org["id"], "org_name": org["name"], "group": d["group_id"], "device": d["id"], "serial": d["sn"],
                        "name": d["name"], "online": d["status"] == "online", "product": d["product_name"],
                        "lat": fix.lat, "lng": fix.lng, "fix_local": next((p.get("ts") for p in d["points"] if p.get("la") is not None), None),
                        "tracked_as": tracked.get((org["id"], d["id"]))})
    out.sort(key=lambda x: (not x["online"], x["org_name"].lower(), (x["name"] or "").lower()))
    return {"devices": out, "problems": problems}


class TrackerIn(BaseModel):
    tenant_id: int
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="vehicle", pattern="^(vehicle|guard)$")
    org: str = Field(min_length=1, max_length=32)
    group: int
    device: int
    serial: str = Field(default="", max_length=40)
    device_name: str = Field(default="", max_length=200)


@router.post("/api/platform/trackers")
async def add_tracker(body: TrackerIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    if await db.get(Tenant, body.tenant_id) is None:
        raise HTTPException(400, "Unknown company")
    if await db.scalar(select(Tracker).where(Tracker.ic2_org == body.org, Tracker.ic2_device == body.device)):
        raise HTTPException(409, "That router is already tracked")
    t = Tracker(tenant_id=body.tenant_id, name=body.name.strip(), kind=body.kind, ic2_org=body.org, ic2_group=body.group,
                ic2_device=body.device, serial=body.serial, device_name=body.device_name)
    db.add(t)
    await db.flush()
    audit(db, body.tenant_id, "tracker.added", user_id=user.id, ip=client_ip(request), name=t.name, serial=t.serial)
    await db.commit()
    bus.publish(t.tenant_id, "tracker.moved", svc.tracker_dict(t))
    svc.wake()
    return await _admin_view(db, await _row(db))


class TrackerEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    kind: str | None = Field(default=None, pattern="^(vehicle|guard)$")
    enabled: bool | None = None
    tenant_id: int | None = None


@router.put("/api/platform/trackers/{tracker_id}")
async def edit_tracker(tracker_id: int, body: TrackerEdit, request: Request, user: User = Depends(MANAGE),
                       db: AsyncSession = Depends(get_db)):
    t = await db.get(Tracker, tracker_id)
    if t is None:
        raise HTTPException(404)
    if body.tenant_id is not None and await db.get(Tenant, body.tenant_id) is None:
        raise HTTPException(400, "Unknown company")
    old_tenant, changed = t.tenant_id, []
    for k in ("name", "kind", "enabled", "tenant_id"):
        v = getattr(body, k)
        if v is not None and getattr(t, k) != (v.strip() if isinstance(v, str) else v):
            setattr(t, k, v.strip() if isinstance(v, str) else v)
            changed.append(k)
    if changed:
        audit(db, t.tenant_id, "tracker.updated", user_id=user.id, ip=client_ip(request), name=t.name, fields=changed)
    await db.commit()
    if old_tenant != t.tenant_id or not t.enabled:
        bus.publish(old_tenant, "tracker.removed", {"id": t.id})
    if t.enabled:
        bus.publish(t.tenant_id, "tracker.moved", svc.tracker_dict(t))
    return await _admin_view(db, await _row(db))


@router.delete("/api/platform/trackers/{tracker_id}")
async def delete_tracker(tracker_id: int, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    t = await db.get(Tracker, tracker_id)
    if t is None:
        raise HTTPException(404)
    tenant_id = t.tenant_id
    await db.execute(TrackerPoint.__table__.delete().where(TrackerPoint.tracker_id == t.id))
    audit(db, tenant_id, "tracker.removed", user_id=user.id, ip=client_ip(request), name=t.name, serial=t.serial)
    await db.delete(t)
    await db.commit()
    bus.publish(tenant_id, "tracker.removed", {"id": tracker_id})
    return await _admin_view(db, await _row(db))

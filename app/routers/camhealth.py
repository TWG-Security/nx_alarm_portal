"""Camera image health: the page's API, the pictures, and the live updates (the checks run in the vision worker,
app/camhealth/worker.py, which writes the camera_health tables).

Problems never reach the alarm feed, sounds or texts (the user's decision, 2026-10-20): only this page, the site
panel and the camera icons. Check now: anyone who sees the camera. Accept view, mute and per-camera settings:
administrators (accepting a view or muting could hide tampering), audit-logged.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.audit import audit
from app.camhealth import state as S
from app.config import get_settings
from app.db import get_db, sessionmaker
from app.deps import client_ip, csrf_protect, current_user, require_admin
from app.models import CameraHealth, CameraHealthEvent, CameraHealthImage, Site, User, VisionStatus
from app.scope import Scope, get_scope, tenant_names
from app.services.bus import bus
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("portal.camhealth")
router = APIRouter(dependencies=[Depends(csrf_protect)])
IMAGE_KINDS = {"last", "ref_day", "ref_night"}
CHECK_EVERY_S = 5            # Check now: at most this often per camera


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:                        # SQLite hands back naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def muted(row: CameraHealth) -> bool:
    return row.muted_until is not None and _ms(row.muted_until) > _ms(utcnow())


def camera_dict(row: CameraHealth, site_names: dict[int, str], tenants: dict[int, str] | None = None,
                refs: dict | None = None) -> dict:
    return {
        "site_id": row.site_id, "site_name": site_names.get(row.site_id, ""), "tenant_id": row.tenant_id,
        "tenant_name": (tenants or {}).get(row.tenant_id, ""), "device_id": row.device_id, "name": row.name,
        "nx_status": row.nx_status, "status": row.status, "muted": muted(row), "muted_until": _ms(row.muted_until),
        "issues": row.issues or [], "mode": row.mode, "ai": row.ai, "learning": row.learning,
        "settings": row.settings or {}, "error": row.error, "checked_at": _ms(row.checked_at),
        "check_requested": row.check_requested_at is not None, "metrics": row.metrics or {},
        "refs": refs or {},
    }


async def _row(db: AsyncSession, scope: Scope, site_id: int, device_id: str, write: bool = False) -> CameraHealth:
    row = await db.scalar(select(CameraHealth).where(CameraHealth.site_id == site_id, CameraHealth.device_id == device_id,
                                                     scope.where(CameraHealth.tenant_id)))
    if row is None:
        raise HTTPException(404, "Camera not found")
    if write:
        scope.require_write(row.tenant_id)
    return row


async def _site_names(db: AsyncSession, scope: Scope) -> dict[int, str]:
    return dict((await db.execute(select(Site.id, Site.name).where(scope.where(Site.tenant_id)))).all())


@router.get("/api/camera-health")
async def list_health(site_id: int | None = None, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Every camera in view with its health (no pictures; those are /media/camera-health/...)."""
    q = select(CameraHealth).where(scope.where(CameraHealth.tenant_id))
    if site_id is not None:
        q = q.where(CameraHealth.site_id == site_id)
    rows = (await db.scalars(q)).all()
    names = await _site_names(db, scope)
    tenants = await tenant_names(db) if scope.mode == "all" else {}
    cams = [camera_dict(r, names, tenants) for r in rows if r.site_id in names]
    order = {"problem": 0, "warning": 1, "offline": 2, "pending": 3, "ok": 4}
    cams.sort(key=lambda c: (c["muted"], order.get(c["status"], 5), c["site_name"].lower(), c["name"].lower()))
    summary = {k: 0 for k in ("problem", "warning", "ok", "offline", "pending", "muted")}
    for c in cams:
        key = "muted" if c["muted"] and c["status"] in ("problem", "warning") else c["status"]
        summary[key] = summary.get(key, 0) + 1
    last = max((c["checked_at"] or 0 for c in cams), default=0) or None
    return {"cameras": cams, "summary": summary, "last_check": last,
            "interval_s": get_settings().camhealth_interval_s, "ai": any(c["ai"] for c in cams)}


@router.get("/api/camera-health/summary")
async def health_summary(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Counts only (the nav badge on every page, the map's site panel): {total: {...}, sites: {id: {...}}}."""
    rows = (await db.execute(select(CameraHealth.site_id, CameraHealth.status, CameraHealth.muted_until)
                             .where(scope.where(CameraHealth.tenant_id)))).all()
    now = _ms(utcnow())
    total: dict[str, int] = {}
    sites: dict[int, dict[str, int]] = {}
    for site_id, status, until in rows:
        key = "muted" if status in ("problem", "warning") and until is not None and _ms(until) > now else status
        total[key] = total.get(key, 0) + 1
        per = sites.setdefault(site_id, {})
        per[key] = per.get(key, 0) + 1
    return {"total": total, "sites": sites}


@router.get("/api/camera-health/processing")
async def processing(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """TWG staff only (platform.view): the vision worker's GPU, model timings, queue and recent checks."""
    if not user.can("platform.view"):
        raise HTTPException(403)
    row = await db.get(VisionStatus, 1)
    if row is None:
        return {"data": None, "age_s": None}
    return {"data": row.data, "age_s": round((_ms(utcnow()) - _ms(row.updated_at)) / 1000)}


@router.get("/api/camera-health/{site_id}/{device_id}")
async def get_health(site_id: int, device_id: str, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    row = await _row(db, scope, site_id, device_id)
    imgs = (await db.execute(select(CameraHealthImage.kind, CameraHealthImage.taken_at, CameraHealthImage.source)
                             .where(CameraHealthImage.site_id == site_id, CameraHealthImage.device_id == device_id))).all()
    refs = {k: {"taken_at": _ms(t), "source": s} for k, t, s in imgs}
    events = (await db.scalars(select(CameraHealthEvent).where(CameraHealthEvent.site_id == site_id,
                                                               CameraHealthEvent.device_id == device_id)
                               .order_by(CameraHealthEvent.id.desc()).limit(40))).all()
    out = camera_dict(row, await _site_names(db, scope), refs=refs)
    out["events"] = [{"at": _ms(e.at), "kind": e.kind, "change": e.change, **(e.detail or {})} for e in events]
    return out


@router.get("/media/camera-health/{site_id}/{device_id}/{kind}.jpg")
async def health_image(site_id: int, device_id: str, kind: str, scope: Scope = Depends(get_scope),
                       db: AsyncSession = Depends(get_db)):
    if kind not in IMAGE_KINDS:
        raise HTTPException(404)
    await _row(db, scope, site_id, device_id)
    img = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == site_id,
                                                          CameraHealthImage.device_id == device_id,
                                                          CameraHealthImage.kind == kind))
    if img is None:
        raise HTTPException(404, "No picture yet")
    # The page asks with ?t=<taken_at>, so a cached copy is always the right one.
    return Response(img.jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _publish(row: CameraHealth) -> None:
    bus.publish(row.tenant_id, "camera.health", {"site_id": row.site_id, "device_id": row.device_id, "status": row.status,
                                                 "muted": muted(row), "checked_at": _ms(row.checked_at),
                                                 "kinds": [i["kind"] for i in row.issues or []]})


@router.post("/api/camera-health/{site_id}/{device_id}/check")
async def check_now(site_id: int, device_id: str, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Ask the worker to check this camera now (it looks every few seconds)."""
    row = await _row(db, scope, site_id, device_id)
    if row.check_requested_at is None or (_ms(utcnow()) - _ms(row.check_requested_at)) > CHECK_EVERY_S * 1000:
        row.check_requested_at = utcnow()
        await db.commit()
    return {"ok": True}


@router.post("/api/camera-health/{site_id}/{device_id}/accept")
async def accept_view(site_id: int, device_id: str, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The last checked picture becomes the reference for its mode (day / night); view issues start over."""
    row = await _row(db, scope, site_id, device_id, write=True)
    last = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == site_id,
                                                           CameraHealthImage.device_id == device_id,
                                                           CameraHealthImage.kind == "last"))
    if last is None or row.mode not in ("day", "night"):
        raise HTTPException(409, "No checked picture yet")
    kind = f"ref_{row.mode}"
    ref = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == site_id,
                                                          CameraHealthImage.device_id == device_id,
                                                          CameraHealthImage.kind == kind))
    now = utcnow()
    if ref is None:
        db.add(CameraHealthImage(site_id=site_id, device_id=device_id, kind=kind, jpeg=last.jpeg, taken_at=now,
                                 source="user", by_user_id=user.id))
    else:
        ref.jpeg, ref.taken_at, ref.source, ref.by_user_id = last.jpeg, now, "user", user.id
    # The time-of-day library starts over too (the worker clears it when it sees the new marker).
    row.settings = {**(row.settings or {}), "library_reset": now.isoformat()}
    cleared = [i for i in row.issues or [] if i["kind"] in S.REFERENCE_KINDS]
    row.issues = [i for i in row.issues or [] if i["kind"] not in S.REFERENCE_KINDS]
    row.state = S.forget(row.state, S.REFERENCE_KINDS)
    row.status = "offline" if row.status == "offline" else S.status(row.issues)
    row.learning, row.changed_at, row.check_requested_at = False, now, now
    db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=site_id, device_id=device_id, at=now, kind="reference",
                             change="reference", detail={"mode": row.mode, "source": "user", "by": user.label,
                                                         "cleared": [i["title"] for i in cleared]}))
    audit(db, row.tenant_id, "camera.reference_accepted", user_id=user.id, site_id=site_id, ip=client_ip(request),
          name=row.name, device_id=device_id, mode=row.mode, cleared=[i["title"] for i in cleared])
    await db.commit()
    _publish(row)
    return {"ok": True}


class ReferenceIn(BaseModel):
    mode: str = Field(pattern="^(day|night)$")
    action: str = Field(pattern="^(retake|relearn|cancel)$")


@router.post("/api/camera-health/{site_id}/{device_id}/reference")
async def reference(site_id: int, device_id: str, body: ReferenceIn, request: Request, user: User = Depends(require_admin),
                    scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Day / night reference. retake: the next check in that mode with nobody in view becomes the reference (now if the
    camera is in that mode, else tonight / in the morning); relearn: forget it, it's learned again automatically;
    cancel: drop a pending retake."""
    row = await _row(db, scope, site_id, device_id, write=True)
    now = utcnow()
    settings = {k: v for k, v in (row.settings or {}).items() if k != "retake"}
    if body.action == "retake":
        settings["retake"] = {"mode": body.mode, "by": user.id, "label": user.label, "at": now.isoformat()}
        if row.mode == body.mode:
            row.check_requested_at = now
    elif body.action == "relearn":
        ref = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == site_id,
                                                              CameraHealthImage.device_id == device_id,
                                                              CameraHealthImage.kind == f"ref_{body.mode}"))
        if ref is not None:
            await db.delete(ref)
        if row.mode == body.mode:
            row.issues = [i for i in row.issues or [] if i["kind"] not in S.REFERENCE_KINDS]
            row.state = S.forget(row.state, S.REFERENCE_KINDS)
            row.status = "offline" if row.status == "offline" else S.status(row.issues)
            row.learning = True
    row.settings, row.changed_at = settings, now
    if body.action != "cancel":
        db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=site_id, device_id=device_id, at=now, kind="reference",
                                 change=body.action, detail={"mode": body.mode, "by": user.label}))
    audit(db, row.tenant_id, f"camera.reference_{body.action}", user_id=user.id, site_id=site_id, ip=client_ip(request),
          name=row.name, device_id=device_id, mode=body.mode)
    await db.commit()
    _publish(row)
    return {"ok": True, "pending": settings.get("retake")}


class MuteIn(BaseModel):
    hours: int = Field(ge=0, le=24 * 30)      # 0 = unmute


@router.post("/api/camera-health/{site_id}/{device_id}/mute")
async def mute(site_id: int, device_id: str, body: MuteIn, request: Request, user: User = Depends(require_admin),
               scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    row = await _row(db, scope, site_id, device_id, write=True)
    now = utcnow()
    row.muted_until = now + timedelta(hours=body.hours) if body.hours else None
    row.changed_at = now
    change = "muted" if body.hours else "unmuted"
    db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=site_id, device_id=device_id, at=now, kind="mute",
                             change=change, detail={"hours": body.hours, "by": user.label}))
    audit(db, row.tenant_id, f"camera.health_{change}", user_id=user.id, site_id=site_id, ip=client_ip(request),
          name=row.name, device_id=device_id, hours=body.hours)
    await db.commit()
    _publish(row)
    return {"ok": True, "muted_until": _ms(row.muted_until)}


class SettingsIn(BaseModel):
    skip: bool | None = None                  # don't check this camera at all
    view_checks: bool | None = None           # moved / view changed / scene change (off for PTZ by default)


@router.put("/api/camera-health/{site_id}/{device_id}/settings")
async def put_settings(site_id: int, device_id: str, body: SettingsIn, request: Request,
                       user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                       db: AsyncSession = Depends(get_db)):
    row = await _row(db, scope, site_id, device_id, write=True)
    new = dict(row.settings or {})
    for k, v in body.model_dump(exclude_none=True).items():
        new[k] = v
    if new.get("skip"):                       # not checked any more: nothing to show
        row.issues, row.state, row.status = [], {}, "offline" if row.status == "offline" else "pending"
    elif new.get("view_checks") is False:
        view = {"moved", "view_changed", "scene_change"}
        row.issues = [i for i in row.issues or [] if i["kind"] not in view]
        row.state = S.forget(row.state, view)
        row.status = "offline" if row.status == "offline" else S.status(row.issues)
    row.settings, row.changed_at, row.check_requested_at = new, utcnow(), utcnow()
    audit(db, row.tenant_id, "camera.health_settings", user_id=user.id, site_id=site_id, ip=client_ip(request),
          name=row.name, device_id=device_id, settings=new)
    await db.commit()
    _publish(row)
    return camera_dict(row, await _site_names(db, scope))


async def watch() -> None:
    """Publish `camera.health` for every camera the worker wrote since the last look (the worker is another process,
    so the database is the hand-over). Cheap: one indexed query every CAMHEALTH_WATCH_S."""
    since = utcnow()
    while True:
        await asyncio.sleep(get_settings().camhealth_watch_s)
        try:
            async with sessionmaker()() as db:
                rows = (await db.scalars(select(CameraHealth).where(CameraHealth.changed_at > since))).all()
            for r in rows:
                since = max(since, r.changed_at if r.changed_at.tzinfo else r.changed_at.replace(tzinfo=timezone.utc))
                _publish(r)
        except Exception:  # noqa: BLE001 - try again next round
            log.exception("camera health watch failed")

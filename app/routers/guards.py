"""Guards & vehicles: live GPS positions on the map (everyone, per company) and their setup on the Sites page
(each company's admins, with the company's own InControl2 API client; TWG in support mode like elsewhere).

  GET    /api/trackers                     the company's trackers with a short trail (map pages)
  GET    /api/trackers/{id}/trail          position history (?minutes= or ?start=&end=, within TRAIL_KEEP_DAYS)
  GET    /api/alarms/{id}/guards           guards closest first, who can be texted, who was dispatched
  POST   /api/alarms/{id}/dispatch         text one, several or all guards to go to the alarm (anyone who can acknowledge it)
  GET    /api/alarms/{id}/timeline         everything that happened, in order (also in the incident report)
  POST   /api/trackers/{id}/refresh        ask InControl2 for its position now (anyone who sees it)
  POST   /api/trackers/{id}/locate         "Locate" with no alarm: a router is asked now; a phone guard is texted their link
  GET    /api/tracking                     the company's InControl2 settings + its trackers (admins)
  PUT    /api/tracking/source              Client ID / Secret / on-off / interval
  POST   /api/tracking/test                get a token and count the organizations it sees
  GET    /api/tracking/orgs                the InControl2 organizations these credentials see
  GET    /api/tracking/orgs/{org}/groups   an organization's groups
  GET    /api/tracking/orgs/{org}/groups/{group}/devices   a group's routers with their GPS state, to pick from
  POST   /api/tracking/trackers            track a router
  PUT    /api/tracking/trackers/{id}       rename / kind / on-off
  DELETE /api/tracking/trackers/{id}
  POST   /api/tracking/phones              a guard without a router: tracked by their phone's browser (texts the link)
  POST   /api/tracking/trackers/{id}/share text (or renew) a phone guard's location link
  PUT    /api/tracking/dispatch            auto-dispatch the closest guard on confirm (company setting)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import guard_share, peplink, platform_settings, sms
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, require_admin
from app.models import Tenant, Tracker, TrackerPoint, TrackerSource, User, utcnow
from app.scope import Scope, get_scope
from app.security import encrypt
from app.services import dispatch as dispatch_svc
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


HISTORY_MAX_POINTS = 4000


@router.get("/api/trackers/{tracker_id}/trail")
async def tracker_trail(tracker_id: int, minutes: int = Query(60, ge=1, le=svc.TRAIL_KEEP_DAYS * 24 * 60),
                        start: datetime | None = None, end: datetime | None = None,
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Where it was: the last `minutes`, or from `start` to `end` (the map's History). Long ranges are thinned to
    HISTORY_MAX_POINTS evenly, always keeping the first and last point."""
    t = await db.scalar(select(Tracker).where(Tracker.id == tracker_id, scope.where(Tracker.tenant_id)))
    if t is None:
        raise HTTPException(404)
    if start is None:
        return {"id": t.id, "points": (await _trails(db, [t.id], minutes))[t.id]}
    start, end = svc.aware(start), svc.aware(end) if end else utcnow()
    if end <= start:
        raise HTTPException(400, "The end must be after the start")
    rows = (await db.execute(select(TrackerPoint.lat, TrackerPoint.lng, TrackerPoint.ts, TrackerPoint.speed)
                             .where(TrackerPoint.tracker_id == t.id, TrackerPoint.ts >= start, TrackerPoint.ts <= end)
                             .order_by(TrackerPoint.ts))).all()
    total = len(rows)
    if total > HISTORY_MAX_POINTS:
        step = total / HISTORY_MAX_POINTS
        rows = [rows[int(i * step)] for i in range(HISTORY_MAX_POINTS - 1)] + [rows[-1]]
    return {"id": t.id, "name": t.name, "total": total, "kept_days": svc.TRAIL_KEEP_DAYS,
            "points": [[round(r.lat, 7), round(r.lng, 7), svc.aware(r.ts).isoformat(), r.speed] for r in rows]}


# ------------------------------------------------------------------------- dispatch (alarm details)

@router.get("/api/alarms/{alarm_id}/guards")
async def alarm_guards(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    from app.routers.api import _get_alarm
    alarm = await _get_alarm(db, scope, alarm_id)
    where = dispatch_svc.target(alarm)
    return {"guards": await dispatch_svc.candidates(db, alarm), "dispatched": await dispatch_svc.dispatched(db, alarm.id),
            "from": where[2] if where else None, "texts_ready": platform_settings.current().sms_ready,
            "auto": dispatch_svc.auto_on(await db.get(Tenant, alarm.tenant_id))}


class DispatchIn(BaseModel):
    tracker_id: int | None = None                                       # one guard...
    tracker_ids: list[int] | None = Field(default=None, max_length=200)   # ...or several (the page's "All guards")


@router.post("/api/alarms/{alarm_id}/dispatch")
async def dispatch_guard(alarm_id: int, body: DispatchIn, request: Request, scope: Scope = Depends(get_scope),
                         db: AsyncSession = Depends(get_db)):
    """Returns {sent: [{to, person, distance, phone}], failed: [{name, error}]}. 400 if none could be sent."""
    from app.routers.api import _get_alarm
    alarm = await _get_alarm(db, scope, alarm_id, write=True)
    ids = list(dict.fromkeys((body.tracker_ids or []) + ([body.tracker_id] if body.tracker_id else [])))
    if not ids:
        raise HTTPException(400, "Pick at least one guard")
    rows = (await db.scalars(select(Tracker).where(Tracker.id.in_(ids), Tracker.tenant_id == alarm.tenant_id))).all()
    if len(rows) != len(ids):
        raise HTTPException(404, "Guard not found")
    names = {t.id: t.name for t in rows}
    user = scope.user
    await db.commit()
    sent, failed = [], []
    for tid in ids:
        try:
            sent.append(await dispatch_svc.dispatch(alarm.id, tid, user, ip=client_ip(request)))
        except dispatch_svc.DispatchError as e:
            failed.append({"name": names[tid], "error": str(e)})
    if not sent:
        raise HTTPException(400, "; ".join(f"{f['name']}: {f['error']}" for f in failed))
    return {"sent": sent, "failed": failed}


@router.get("/api/alarms/{alarm_id}/timeline")
async def alarm_timeline(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    from app.routers.api import _get_alarm
    from app.services.report import timeline
    alarm = await _get_alarm(db, scope, alarm_id)
    return await timeline(db, alarm)


@router.post("/api/trackers/{tracker_id}/refresh")
async def refresh_tracker(tracker_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await db.scalar(select(Tracker).where(Tracker.id == tracker_id, scope.where(Tracker.tenant_id)))
    if t is None:
        raise HTTPException(404)
    await db.commit()                        # don't hold the connection while IC2 answers
    try:
        out = await svc.refresh_one(tracker_id)
    except peplink.PeplinkError as e:
        raise HTTPException(502, str(e)) from None
    if out is None:
        raise HTTPException(404)
    return out


LOCATE_EVERY_S = 60                      # a guard is texted "where are you?" at most this often
_located: dict[int, float] = {}


@router.post("/api/trackers/{tracker_id}/locate")
async def locate_tracker(tracker_id: int, request: Request, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The dispatcher wants to see where a guard is now, with no alarm. A router: asked at once (like ↻). A guard
    tracked by their phone: texted their location link ("Dispatch asks where you are"), at most once a minute."""
    import time
    t = await db.scalar(select(Tracker).where(Tracker.id == tracker_id, scope.where(Tracker.tenant_id)))
    if t is None:
        raise HTTPException(404)
    scope.require_write(t.tenant_id)
    if (t.source or "peplink") != "phone":
        await db.commit()
        try:
            out = await svc.refresh_one(tracker_id)
        except peplink.PeplinkError as e:
            raise HTTPException(502, str(e)) from None
        return {"kind": "refreshed", "tracker": out}
    u = await db.get(User, t.user_id) if t.user_id else None
    reason = dispatch_svc.why_not(t, u)
    cfg = await sms.config(db) if not reason else None
    if not reason and cfg is None:
        reason = "Text messages are switched off or not set up"
    if reason:
        raise HTTPException(400, reason)
    wait = LOCATE_EVERY_S - (time.monotonic() - _located.get(t.id, -1e9))
    if wait > 0:
        raise HTTPException(429, f"{t.name} was asked less than a minute ago. Try again in {int(wait) + 1} s.")
    _located[t.id] = time.monotonic()
    link = guard_share.share_url(t)
    tenant = await db.get(Tenant, t.tenant_id)
    by = scope.user
    audit(db, t.tenant_id, "tracker.location_requested", user_id=by.id, ip=client_ip(request), name=t.name, to=u.label)
    await db.commit()
    sms._spawn(sms._send(cfg, u, sms.locate_text(tenant.label if tenant else "", by.label, link), "locate", None, time.time()))
    return {"kind": "texted", "phone": sms.masked(u.phone), "name": t.name}


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


def _share_state(t: Tracker) -> dict:
    if (t.source or "peplink") != "phone":
        return {}
    return {"share_expires_at": svc.aware(t.share_expires_at).isoformat() if t.share_expires_at else None,
            "share_seen_at": svc.aware(t.share_seen_at).isoformat() if t.share_seen_at else None}


async def _view(db: AsyncSession, tenant: Tenant) -> dict:
    src = await _source(db, tenant.id)
    trackers = (await db.scalars(select(Tracker).where(Tracker.tenant_id == tenant.id).order_by(Tracker.name))).all()
    people = (await db.scalars(select(User).where(User.tenant_id == tenant.id, User.deleted_at.is_(None),
                                                  User.is_active.is_(True)).order_by(User.email))).all()
    iso = lambda d: svc.aware(d).isoformat() if d else None  # noqa: E731
    return {"company": tenant.label, "enabled": bool(src and src.enabled), "client_id": src.client_id if src else "",
            "secret_set": bool(src and src.secret_enc), "poll_s": src.poll_s if src else 30,
            "last_ok_at": iso(src.last_ok_at) if src else None, "last_error": src.last_error if src else "",
            "last_error_at": iso(src.last_error_at) if src else None,
            "auto_dispatch": dispatch_svc.auto_on(tenant), "texts_ready": platform_settings.current().sms_ready,
            "people": [{"id": u.id, "name": u.label, "phone": sms.masked(u.phone) if u.phone else "", "consent": u.sms_consent}
                       for u in people],
            "trackers": [{**svc.tracker_dict(t), "ic2_org": t.ic2_org, "ic2_group": t.ic2_group, "ic2_device": t.ic2_device,
                          **_share_state(t)} for t in trackers]}


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


ORG_ID = r"^[A-Za-z0-9_-]{1,32}$"            # goes into IC2 URLs


async def _ic2(call):
    try:
        return await call
    except peplink.PeplinkError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/api/tracking/orgs")
async def list_orgs(user: User = Depends(require_admin), scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    client = await _client(db, scope.write_tenant())
    await db.commit()                        # don't hold the connection while IC2 answers
    orgs = await _ic2(client.organizations())
    return sorted(orgs, key=lambda o: (o["name"] or "").lower())


@router.get("/api/tracking/orgs/{org}/groups")
async def list_groups(org: str = Path(pattern=ORG_ID), user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    client = await _client(db, scope.write_tenant())
    await db.commit()
    groups = await _ic2(client.groups(org))
    return sorted(groups, key=lambda g: (g["name"] or "").lower())


@router.get("/api/tracking/orgs/{org}/groups/{group}/devices")
async def list_group_devices(group: int, org: str = Path(pattern=ORG_ID), user: User = Depends(require_admin),
                             scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """A group's routers: live GPS (with how fresh), GPS but no position, or no GPS (can't be tracked)."""
    tenant_id = scope.write_tenant()
    client = await _client(db, tenant_id)
    tracked = {(t.ic2_org, t.ic2_device): t.name for t in (await db.scalars(select(Tracker).where(Tracker.tenant_id == tenant_id))).all()}
    await db.commit()
    out = []
    for d in await _ic2(client.group_locations(org, group)):
        fix = peplink.newest_fix(d["points"], "UTC", peplink.zone_offset(d["group_time"]))
        out.append({"org": org, "group": d["group_id"] or group, "device": d["id"], "serial": d["sn"], "name": d["name"],
                    "online": d["status"] == "online", "product": d["product_name"], "gps_off": d["gps_off"],
                    "gps": "live" if fix else "none", "fix_at": fix.at.isoformat() if fix else None,
                    "lat": fix.lat if fix else None, "lng": fix.lng if fix else None,
                    "tracked_as": tracked.get((org, d["id"]))})
    out.sort(key=lambda x: (x["gps"] != "live", not x["online"], (x["name"] or "").lower()))
    return out


class TrackerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="vehicle", pattern="^(vehicle|guard)$")
    org: str = Field(pattern=ORG_ID)
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
    src = await _source(db, tenant.id)
    if src is not None and not src.enabled and src.client_id and src.secret_enc:
        # Tracking a router means "track it": switch tracking on (2026-10-13: a router was tracked with it off,
        # and its position never showed).
        src.enabled = True
        audit(db, tenant.id, "tracking.settings", user_id=user.id, ip=client_ip(request), fields=["enabled"], auto=True)
    await db.commit()
    bus.publish(t.tenant_id, "tracker.moved", svc.tracker_dict(t))
    svc.wake()
    return await _view(db, tenant)


class TrackerEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    kind: str | None = Field(default=None, pattern="^(vehicle|guard)$")
    enabled: bool | None = None
    user_id: int | None = -1             # the person dispatch texts go to; -1 = unchanged, None / 0 = nobody


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
    if body.user_id != -1:
        uid = await _person(db, t.tenant_id, body.user_id or None)
        if uid != t.user_id:
            t.user_id = uid
            changed.append("user_id")
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


# ------------------------------------------------------------------------- phone guards, dispatch setting

async def _person(db: AsyncSession, tenant_id: int, user_id: int | None) -> int | None:
    if user_id is None:
        return None
    u = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id, User.deleted_at.is_(None)))
    if u is None:
        raise HTTPException(400, "That person isn't in this company")
    return u.id


async def _text_link(db: AsyncSession, t: Tracker, renew: bool = False) -> dict:
    """Make the phone guard's link and text it to them if texts are possible. Returns what the admin sees."""
    link = guard_share.share_url(t, renew=renew)
    u = await db.get(User, t.user_id) if t.user_id else None
    reason = dispatch_svc.why_not(t, u)
    cfg = await sms.config(db) if not reason else None
    if not reason and cfg is None:
        reason = "Text messages are switched off or not set up"
    tenant = await db.get(Tenant, t.tenant_id)
    await db.commit()
    if not reason:
        sms._spawn(sms._send(cfg, u, sms.share_invite_text(tenant.label if tenant else "", link), "share", None, 0.0))
    return {"link": link, "texted": not reason, "reason": reason}


class PhoneIn(BaseModel):
    user_id: int
    name: str = Field(default="", max_length=100)
    kind: str = Field(default="guard", pattern="^(vehicle|guard)$")


@router.post("/api/tracking/phones")
async def add_phone(body: PhoneIn, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                    db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, scope.write_tenant())
    uid = await _person(db, tenant.id, body.user_id)
    if await db.scalar(select(Tracker).where(Tracker.tenant_id == tenant.id, Tracker.source == "phone", Tracker.user_id == uid)):
        raise HTTPException(409, "That person is already tracked by phone")
    person = await db.get(User, uid)
    t = Tracker(tenant_id=tenant.id, name=(body.name.strip() or person.label)[:100], kind=body.kind, source="phone", user_id=uid)
    db.add(t)
    await db.flush()
    audit(db, tenant.id, "tracker.added", user_id=user.id, ip=client_ip(request), name=t.name, source="phone", person=person.label)
    sent = await _text_link(db, t)
    bus.publish(t.tenant_id, "tracker.moved", svc.tracker_dict(t))
    return {**await _view(db, tenant), "share": {**sent, "id": t.id}}


class ShareIn(BaseModel):
    renew: bool = False


@router.post("/api/tracking/trackers/{tracker_id}/share")
async def send_share(tracker_id: int, body: ShareIn, request: Request, user: User = Depends(require_admin),
                     scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await _tracker(db, scope, tracker_id)
    if (t.source or "peplink") != "phone":
        raise HTTPException(400, "Only guards tracked by their phone have a location link")
    audit(db, t.tenant_id, "tracker.share_sent", user_id=user.id, ip=client_ip(request), name=t.name, renew=body.renew)
    sent = await _text_link(db, t, renew=body.renew)
    return {**await _view(db, await db.get(Tenant, t.tenant_id)), "share": {**sent, "id": t.id}}


class DispatchSettingIn(BaseModel):
    auto: bool


@router.put("/api/tracking/dispatch")
async def put_dispatch(body: DispatchSettingIn, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, scope.write_tenant())
    tenant.settings = {**(tenant.settings or {}), "auto_dispatch": body.auto}
    audit(db, tenant.id, "tracking.settings", user_id=user.id, ip=client_ip(request), fields=["auto_dispatch"], auto_dispatch=body.auto)
    await db.commit()
    return await _view(db, tenant)

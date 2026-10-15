"""JSON API used by the portal's own pages."""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user, require_admin
from app.models import Alarm, AlarmNote, AuditLog, Site, Tenant, User, UserGroup, UserGroupMember
from app.permissions import applicable, require
from app.scope import Scope, get_scope
from app.services.alarm_filter import CHOICES, EVENT_TYPES, LEVELS, Policy
from app.security import decrypt, encrypt, hash_password
from app.services import ack as ack_service
from app.services import arming
from app.services.bus import bus
from app.services.poller import manager, now_ms
from app.services.serialize import alarm_dict, audit_dict, note_dict, site_dict
from app.services.sites import ConnectError, make_client, probe, resolve_host
from app.config import get_settings

router = APIRouter(prefix="/api", dependencies=[Depends(csrf_protect)])


# --------------------------------------------------------------------------- sites

class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=500)          # Nx Cloud id or URL
    nx_user: str = Field(min_length=1, max_length=200)
    nx_pass: str = Field(default="", max_length=500)         # blank on edit = keep current
    address: str = Field(default="", max_length=500)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    notes: str = Field(default="", max_length=5000)
    timezone: str = Field(default="", max_length=64)         # IANA name for the arming schedule; "" = default
    arm_schedule: list[dict] | None = None                   # [{action, time "HH:MM", days [...]}]; None = keep
    connect: bool = True                                     # False = save without testing

    @field_validator("name", "host", "nx_user", "address")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


ADDRESS_REQUIRED = "Enter the site's address (street, city, state): dispatch texts and the incident report use it"


class ConnTest(BaseModel):
    host: str
    nx_user: str
    nx_pass: str = ""
    site_id: int | None = None


async def _open_counts(db: AsyncSession, scope: Scope) -> dict[int, dict[int, int]]:
    """{site_id: {priority: open_count}} for the companies in view."""
    rows = await db.execute(
        select(Alarm.site_id, Alarm.priority, func.count())
        .where(scope.where(Alarm.tenant_id), Alarm.state == "new")
        .group_by(Alarm.site_id, Alarm.priority)
    )
    out: dict[int, dict[int, int]] = {}
    for site_id, priority, n in rows.all():
        out.setdefault(site_id, {})[priority] = n
    return out


async def _get_site(db: AsyncSession, scope: Scope, site_id: int, write: bool = False) -> Site:
    site = await db.scalar(select(Site).where(Site.id == site_id, scope.where(Site.tenant_id)))
    if site is None:
        raise HTTPException(404, "Site not found")
    if write:
        scope.require_write(site.tenant_id)
    return site


def _site_out(site: Site, counts: dict) -> dict:
    return {**site_dict(site, counts.get(site.id)), "push": manager.push_state(site.id), **manager.rule_info(site.id)}


def _apply_arming_settings(site: Site, body: SiteIn, user: User) -> list[str]:
    """Timezone + schedule from the site form. Returns the changed field names. Raises HTTPException(400)."""
    tz = body.timezone.strip()
    if tz and not arming.valid_timezone(tz):
        raise HTTPException(400, {"message": f"Unknown time zone '{tz}'"})
    old_entries = (site.arm_schedule or {}).get("entries", [])
    try:
        entries = old_entries if body.arm_schedule is None else arming.clean_entries(body.arm_schedule)
    except ValueError as exc:
        raise HTTPException(400, {"message": f"Arming schedule: {exc}"}) from exc
    changed = [name for name, differs in (("timezone", tz != (site.timezone or "")),
                                          ("arm_schedule", entries != old_entries)) if differs]
    if not changed:
        return []
    t = now_ms()
    if site.id is not None:
        # Keep the current state until the next scheduled time: a schedule edit never arms or disarms on the spot.
        cur = arming.site_state(site, t)
        site.arm_override = {"armed": cur.armed, "at_ms": t, "until_ms": None if cur.armed else cur.until_ms,
                             "by": user.label, "user_id": user.id, "note": "State kept when the schedule was changed"}
    site.timezone = tz
    site.arm_schedule = {"entries": entries, "since_ms": t} if entries else None
    return changed


async def _probe(host: str, nx_user: str, nx_pass: str) -> dict:
    client = make_client(host, nx_user, nx_pass)
    try:
        return await probe(client)
    except ConnectError as exc:
        raise HTTPException(400, {"message": str(exc), "status": exc.status}) from exc
    finally:
        await client.close()


@router.get("/sites")
async def list_sites(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    sites = (await db.scalars(
        select(Site).where(scope.where(Site.tenant_id), Site.archived_at.is_(None)).order_by(Site.name)
    )).all()
    counts = await _open_counts(db, scope)
    return [_site_out(s, counts) for s in sites]


@router.post("/sites/test")
async def test_connection(body: ConnTest, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                          db: AsyncSession = Depends(get_db)):
    try:
        host, _ = resolve_host(body.host)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc
    password = body.nx_pass
    if not password and body.site_id:
        password = decrypt((await _get_site(db, scope, body.site_id, write=True)).nx_pass_enc)
    info = await _probe(host, body.nx_user, password)
    info.pop("devices", None)
    return {"host": host, **info}


@router.post("/sites")
async def create_site(body: SiteIn, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant_id = scope.write_tenant()
    try:
        host, cloud_id = resolve_host(body.host)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc
    if not body.nx_pass:
        raise HTTPException(400, {"message": "NX password is required"})
    if not body.address:
        raise HTTPException(400, {"message": ADDRESS_REQUIRED})
    site = Site(tenant_id=tenant_id, name=body.name, host=host, cloud_id=cloud_id, nx_user=body.nx_user,
                nx_pass_enc=encrypt(body.nx_pass), address=body.address, lat=body.lat, lng=body.lng,
                notes=body.notes)
    _apply_arming_settings(site, body, user)
    if body.connect:
        info = await _probe(host, body.nx_user, body.nx_pass)
        site.nx_site_name, site.nx_version = info["nx_site_name"], info["nx_version"]
        site.camera_count, site.cloud_id = info["camera_count"], info["cloud_id"] or cloud_id
        site.status = "online"
        site.last_seen_at = datetime.now(timezone.utc)
    # Start from "now": the portal does not import a site's historical backlog.
    site.event_cursor_ms = now_ms() - get_settings().initial_lookback_ms
    db.add(site)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, {"message": f"A site named '{body.name}' already exists"}) from exc
    audit(db, tenant_id, "site.created", user_id=user.id, site_id=site.id, ip=client_ip(request),
          name=site.name, host=host, connected=body.connect)
    await db.commit()
    await db.refresh(site)
    manager.start(site)
    data = site_dict(site)
    bus.publish(tenant_id, "site.updated", data)
    return data


@router.put("/sites/{site_id}")
async def update_site(site_id: int, body: SiteIn, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, site_id, write=True)
    if not body.address and (site.address or "").strip():          # sites made before addresses were required may
        raise HTTPException(400, {"message": ADDRESS_REQUIRED})      # still be blank; a set one can't be cleared
    try:
        host, cloud_id = resolve_host(body.host)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc
    password = body.nx_pass or decrypt(site.nx_pass_enc)
    creds_changed = host != site.host or body.nx_user != site.nx_user or bool(body.nx_pass)
    if creds_changed and body.connect:
        info = await _probe(host, body.nx_user, password)
        site.nx_site_name, site.nx_version, site.camera_count = info["nx_site_name"], info["nx_version"], info["camera_count"]
        cloud_id = info["cloud_id"] or cloud_id
    changed = sorted(k for k, v in {"name": body.name, "host": host, "nx_user": body.nx_user, "address": body.address,
                                    "lat": body.lat, "lng": body.lng, "notes": body.notes}.items()
                     if getattr(site, k) != v)
    if body.nx_pass:
        changed.append("nx_pass")
    changed += _apply_arming_settings(site, body, user)
    site.name, site.host, site.cloud_id, site.nx_user = body.name, host, cloud_id, body.nx_user
    site.nx_pass_enc = encrypt(password)
    site.address, site.lat, site.lng, site.notes = body.address, body.lat, body.lng, body.notes
    audit(db, site.tenant_id, "site.updated", user_id=user.id, site_id=site.id, ip=client_ip(request), fields=changed)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, {"message": f"A site named '{body.name}' already exists"}) from exc
    if creds_changed:
        await manager.restart(site)
    data = _site_out(site, await _open_counts(db, scope))
    bus.publish(site.tenant_id, "site.updated", data)
    return data


class ArmIn(BaseModel):
    note: str = Field(default="", max_length=500)
    minutes: int | None = Field(default=None, ge=1, le=arming.MAX_DISARM_MINUTES)   # disarm only: auto re-arm after


@router.post("/sites/{site_id}/arm")
@router.post("/sites/{site_id}/disarm")
async def arm_site(site_id: int, body: ArmIn, request: Request, scope: Scope = Depends(get_scope),
                   db: AsyncSession = Depends(get_db)):
    """Operators arm/disarm by hand. It holds until the next scheduled change (or the disarm timer)."""
    user = scope.user
    site = await _get_site(db, scope, site_id, write=True)
    if site.archived_at is not None:
        raise HTTPException(400, "Site is archived")
    armed = request.url.path.endswith("/arm")
    t = now_ms()
    until = t + body.minutes * 60_000 if body.minutes and not armed else None
    site.arm_override = {"armed": armed, "at_ms": t, "until_ms": until, "by": user.label, "user_id": user.id,
                         "note": body.note.strip()}
    site.armed = armed
    audit(db, site.tenant_id, "site.armed" if armed else "site.disarmed", user_id=user.id, site_id=site.id,
          ip=client_ip(request), source="manual", note=body.note.strip(), until_ms=until)
    await db.commit()
    data = _site_out(site, await _open_counts(db, scope))
    bus.publish(site.tenant_id, "site.updated", data)
    return data


# --------------------------------------------------------------------------- camera positions (3D view)

class CameraPlaceIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    heading: float = Field(default=0, ge=0, le=360)        # degrees clockwise from north, the way it faces
    fov: float = Field(default=60, ge=5, le=360)           # field of view, degrees (360 = fisheye / dome)
    range_m: float = Field(default=40, ge=2, le=500)       # how far the view cone is drawn
    # 3D pose (the 3D view's camera body, view cone and Calibrate). None = keep what's saved (the 2D editors don't
    # send them), so placing or aiming a camera on the map never undoes a calibration.
    height_m: float | None = Field(default=None, ge=0, le=300)   # lens height above the ground at its spot
    tilt: float | None = Field(default=None, ge=-90, le=90)      # degrees below the horizon (down is positive)
    roll: float | None = Field(default=None, ge=-45, le=45)      # degrees, clockwise as seen from behind the camera


async def _site_cameras(site: Site) -> dict[str, str]:
    """{device id: name} of the site's cameras, fresh from NX; the poller's last list if NX can't be reached."""
    try:
        devices = await manager.client_for(site).list_devices() or []
        return {(d.get("id") or "").strip("{}"): d.get("name", "") for d in devices
                if (d.get("deviceType") or "Camera").lower() not in ("server", "io")}
    except Exception:  # noqa: BLE001
        return manager.device_names(site.id)


@router.get("/sites/{site_id}/cameras")
async def list_site_cameras(site_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, site_id)
    names = await _site_cameras(site)
    places = site.camera_places or {}
    ids = list(names) + [d for d in places if d not in names]      # placed cameras NX no longer lists stay visible
    return sorted(({"device_id": d, "name": names.get(d) or places.get(d, {}).get("name") or d,
                    "in_nx": d in names, "place": places.get(d)} for d in ids), key=lambda c: c["name"].lower())


class ArmScheduleIn(BaseModel):
    timezone: str = Field(default="", max_length=64)
    arm_schedule: list[dict] = Field(default_factory=list, max_length=200)


@router.put("/sites/{site_id}/arm-schedule")
async def put_arm_schedule(site_id: int, body: ArmScheduleIn, request: Request, user: User = Depends(require_admin),
                           scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Save only the arming schedule and its time zone (the schedule card's own Save button): the NX
    connection is never touched, so nothing reconnects."""
    site = await _get_site(db, scope, site_id, write=True)
    changed = _apply_arming_settings(site, body, user)
    if changed:
        audit(db, site.tenant_id, "site.updated", user_id=user.id, site_id=site.id, ip=client_ip(request), fields=changed)
    await db.commit()
    data = _site_out(site, await _open_counts(db, scope))
    if changed:
        bus.publish(site.tenant_id, "site.updated", data)
    return data


async def _site_policy(db: AsyncSession, site: Site) -> Policy:
    tenant = await db.get(Tenant, site.tenant_id)
    return Policy.from_settings(tenant.settings if tenant else None)


@router.get("/sites/{site_id}/rules")
async def site_rules(site_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The NX rules the portal last read for this site and what each does here (level / ignored and why)."""
    site = await _get_site(db, scope, site_id)
    return manager.rules_view(site, await _site_policy(db, site))


class RuleEditIn(BaseModel):
    comment: str | None = Field(default=None, max_length=500)
    level: str | None = Field(default=None, pattern="^(critical|alarm|warning|ignore|none)$")   # None = keep
    always: bool | None = None              # #24h
    enabled: bool | None = None
    interval_s: int | None = Field(default=None, ge=0, le=86400)


@router.patch("/sites/{site_id}/rules/{rule_id}")
async def edit_site_rule(site_id: int, rule_id: str, body: RuleEditIn, request: Request, user: User = Depends(require_admin),
                         scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Administrators: change a rule on the site's NX server (tag, #24h, comment, on/off, Interval of action), checked
    by reading it back (app/services/rule_edit.py). Audit-logged with what changed."""
    from app.services import rule_edit
    site = await _get_site(db, scope, site_id, write=True)
    rule_id = rule_id.strip("{}")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", rule_id):
        raise HTTPException(404, "Rule not found")
    await db.commit()                                   # don't hold the connection while NX answers
    level = rule_edit.UNCHANGED if body.level is None else (None if body.level == "none" else body.level)
    try:
        before, changes, after = await rule_edit.edit(manager.client_for(site), rule_id, comment=body.comment, level=level,
                                                      always=body.always, enabled=body.enabled, interval_s=body.interval_s)
    except rule_edit.RuleEditError as e:
        audit(db, site.tenant_id, "nx.rule_edit_failed", user_id=user.id, site_id=site.id, ip=client_ip(request),
              rule_id=rule_id, error=str(e))
        await db.commit()
        raise HTTPException(400, str(e)) from None
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"NX refused the change (HTTP {e.response.status_code}). The portal's NX account needs to be "
                                 "in Power Users or Administrators") from None
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach NX ({type(e).__name__})") from None
    if changes:
        audit(db, site.tenant_id, "nx.rule_edited", user_id=user.id, site_id=site.id, ip=client_ip(request), rule_id=rule_id,
              **{("interval_s" if k == "action" else k):
                 {"from": before.get(k) if k != "action" else int((before.get("action") or {}).get("intervalS") or 0),
                  "to": v if k != "action" else int(v.get("intervalS") or 0)} for k, v in changes.items()})
        await db.commit()
        await manager.check_now(site.id)                 # re-read the rules so the portal uses the new tags at once
    return {"changed": sorted("interval_s" if k == "action" else k for k in changes),
            "rules": manager.rules_view(site, await _site_policy(db, site))}


_RULE_ID = re.compile(r"[0-9a-fA-F-]{36}")


def _rule_id(rule_id: str) -> str:
    rule_id = rule_id.strip("{}")
    if not _RULE_ID.fullmatch(rule_id):
        raise HTTPException(404, "Rule not found")
    return rule_id


async def _rule_call(db: AsyncSession, site: Site, user: User, request: Request, rule_id: str, coro):
    """Run a rule write against NX, turning its failures into answers for the admin (and the audit log)."""
    from app.services import rule_edit
    try:
        return await coro
    except rule_edit.RuleEditError as e:
        audit(db, site.tenant_id, "nx.rule_edit_failed", user_id=user.id, site_id=site.id, ip=client_ip(request),
              rule_id=rule_id, error=str(e))
        await db.commit()
        raise HTTPException(400, str(e)) from None
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = str(body.get("errorString") or body.get("error") or body.get("message") or "")[:300]
        except ValueError:
            pass
        audit(db, site.tenant_id, "nx.rule_edit_failed", user_id=user.id, site_id=site.id, ip=client_ip(request),
              rule_id=rule_id, error=f"HTTP {e.response.status_code} {detail}".strip())
        await db.commit()
        if e.response.status_code in (401, 403):
            raise HTTPException(502, "NX refused: the portal's NX account needs to be in Power Users or Administrators") from None
        raise HTTPException(502, f"NX refused the change (HTTP {e.response.status_code}){': ' + detail if detail else ''}") from None
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        raise HTTPException(502, f"Couldn't reach NX ({type(e).__name__})") from None


async def _catalog(site: Site, refresh: bool = False) -> dict:
    from app.services import rule_catalog
    try:
        return await rule_catalog.get(site, manager.rules_raw(site.id), refresh=refresh)
    except (RuntimeError, httpx.HTTPError, asyncio.TimeoutError) as e:
        raise HTTPException(502, f"Couldn't read the site's rule options from NX: {str(e) or type(e).__name__}"[:400]) from None


@router.get("/sites/{site_id}/rules/catalog")
async def site_rule_catalog(site_id: int, refresh: bool = False, user: User = Depends(require_admin),
                            scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Administrators: what this site's NX rule engine offers (event and action types with their fields, cameras,
    users, analytics event / object types and zones per camera), for the rule editor's drop-downs."""
    site = await _get_site(db, scope, site_id, write=True)
    await db.commit()                                   # don't hold the connection while NX answers
    return await _catalog(site, refresh)


@router.get("/sites/{site_id}/rules/cameras/{device_id}/still.jpg")
async def rule_camera_still(site_id: int, device_id: str, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                            db: AsyncSession = Depends(get_db)):
    """Administrators: a live still of one of the site's cameras, for the rule editor's zone preview."""
    from fastapi import Response
    from app.routers.stream import camera_frame
    site = await _get_site(db, scope, site_id, write=True)
    await db.commit()
    device_id = device_id.strip("{}").lower()
    if device_id not in {d.lower() for d in await _site_cameras(site)}:
        raise HTTPException(404, "That camera isn't on this site")
    return Response(await camera_frame(site, device_id, -1), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/sites/{site_id}/rules/{rule_id}")
async def site_rule(site_id: int, rule_id: str, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                    db: AsyncSession = Depends(get_db)):
    """Administrators: one rule as NX has it now (an action's login is never sent to the browser)."""
    from app.services import rule_edit
    site = await _get_site(db, scope, site_id, write=True)
    rule_id = _rule_id(rule_id)
    await db.commit()
    try:
        rule = await rule_edit._Retrying(manager.client_for(site)).get_rule(rule_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(404 if e.response.status_code == 404 else 502,
                            "Rule not found" if e.response.status_code == 404 else f"NX answered HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach NX ({type(e).__name__})") from None
    return rule_edit.redact(rule)


class RulePart(BaseModel):
    type: str | None = Field(default=None, max_length=100)
    fields: dict = Field(default_factory=dict)


class RuleFullIn(BaseModel):
    event: RulePart | None = None
    action: RulePart | None = None
    comment: str | None = Field(default=None, max_length=500)
    level: str | None = Field(default=None, pattern="^(critical|alarm|warning|ignore|none)$")
    always: bool | None = None
    enabled: bool | None = None


def _level(v: str | None):
    from app.services import rule_edit
    return rule_edit.UNCHANGED if v is None else (None if v == "none" else v)


def _audit_part(v: dict | None) -> dict | None:
    from app.services import rule_edit
    return rule_edit.redact({"action": v}).get("action") if v else v


async def _after_rule_write(db: AsyncSession, site: Site) -> dict:
    await manager.check_now(site.id)                     # the portal uses the new rule (tags, levels) at once
    return manager.rules_view(site, await _site_policy(db, site))


@router.put("/sites/{site_id}/rules/{rule_id}")
async def save_site_rule(site_id: int, rule_id: str, body: RuleFullIn, request: Request, user: User = Depends(require_admin),
                         scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Administrators: the full rule editor's Save: event type, cameras, analytics type / zone, filters, action,
    tags, on/off; written to NX, read back and checked (put back if NX didn't apply it as sent)."""
    from app.services import rule_edit
    site = await _get_site(db, scope, site_id, write=True)
    rule_id = _rule_id(rule_id)
    await db.commit()
    catalog = await _catalog(site)
    before, changes, _ = await _rule_call(db, site, user, request, rule_id, rule_edit.save(
        manager.client_for(site), rule_id, catalog,
        event=body.event.model_dump() if body.event else None, action=body.action.model_dump() if body.action else None,
        comment=body.comment, level=_level(body.level), always=body.always, enabled=body.enabled))
    if changes:
        detail = {}
        for k, v in changes.items():
            old = before.get(k)
            detail["nx_action" if k == "action" else k] = {"from": _audit_part(old) if k == "action" else old,
                                                           "to": _audit_part(v) if k == "action" else v}
        audit(db, site.tenant_id, "nx.rule_edited", user_id=user.id, site_id=site.id, ip=client_ip(request), rule_id=rule_id, **detail)
        await db.commit()
    return {"changed": sorted(changes), "rules": await _after_rule_write(db, site) if changes else
            manager.rules_view(site, await _site_policy(db, site))}


class RuleNewIn(BaseModel):
    event: RulePart
    action: RulePart
    comment: str = Field(default="", max_length=500)
    level: str = Field(default="none", pattern="^(critical|alarm|warning|ignore|none)$")
    always: bool = False
    enabled: bool = True


@router.post("/sites/{site_id}/rules")
async def create_site_rule(site_id: int, body: RuleNewIn, request: Request, user: User = Depends(require_admin),
                           scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Administrators: a new NX rule on the site, checked by reading it back (deleted again if NX stored it differently)."""
    from app.services import rule_edit
    site = await _get_site(db, scope, site_id, write=True)
    await db.commit()
    catalog = await _catalog(site)
    level = None if body.level == "none" else body.level
    made = await _rule_call(db, site, user, request, "", rule_edit.create(
        manager.client_for(site), catalog, event=body.event.model_dump(), action=body.action.model_dump(),
        comment=body.comment, level=level, always=body.always, enabled=body.enabled))
    rid = (made.get("id") or "").strip("{}")
    audit(db, site.tenant_id, "nx.rule_created", user_id=user.id, site_id=site.id, ip=client_ip(request), rule_id=rid,
          rule=rule_edit.redact(made))
    await db.commit()
    return {"id": rid, "rules": await _after_rule_write(db, site)}


@router.delete("/sites/{site_id}/rules/{rule_id}")
async def delete_site_rule(site_id: int, rule_id: str, request: Request, user: User = Depends(require_admin),
                           scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Administrators: delete an NX rule. The audit log keeps the whole rule (minus any login) so it can be rebuilt."""
    from app.services import rule_edit
    site = await _get_site(db, scope, site_id, write=True)
    rule_id = _rule_id(rule_id)
    await db.commit()
    before = await _rule_call(db, site, user, request, rule_id, rule_edit.delete(manager.client_for(site), rule_id))
    audit(db, site.tenant_id, "nx.rule_deleted", user_id=user.id, site_id=site.id, ip=client_ip(request), rule_id=rule_id,
          rule=rule_edit.redact(before))
    await db.commit()
    return {"rules": await _after_rule_write(db, site)}


class TaggedOnlyIn(BaseModel):
    on: bool


@router.put("/settings/tagged-only")
async def put_tagged_only(body: TaggedOnlyIn, request: Request, user: User = Depends(require_admin),
                          scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The company's "tagged NX rules only" switch, from the NX rules list (same setting as Settings → Alarm levels)."""
    tenant = await db.get(Tenant, scope.write_tenant())
    old = Policy.from_settings(tenant.settings)
    policy = Policy(levels=dict(old.levels), force_ack_critical=old.force_ack_critical, tagged_only=body.on)
    tenant.settings = {**(tenant.settings or {}), "alarm_policy": policy.to_settings()}
    if old.tagged_only != body.on:
        audit(db, tenant.id, "settings.alarm_levels", user_id=user.id, ip=client_ip(request), tagged_only=body.on)
    await db.commit()
    return {"tagged_only": policy.tagged_only}


CHECK_EVERY_S = 5.0
_checked: dict[int, float] = {}


@router.post("/sites/{site_id}/check")
async def check_site(site_id: int, request: Request, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """"Check now": re-read the site from NX right away (status, cameras, rules, new events). Read-only towards NX."""
    import time as _t
    site = await _get_site(db, scope, site_id)
    if _t.monotonic() - _checked.get(site.id, -1e9) < CHECK_EVERY_S:
        raise HTTPException(429, "Just checked: try again in a few seconds")
    _checked[site.id] = _t.monotonic()
    audit(db, site.tenant_id, "site.checked", user_id=scope.user.id, site_id=site.id, ip=client_ip(request))
    await db.commit()                                   # don't hold the connection while NX answers
    error = await manager.check_now(site.id)
    site = await _get_site(db, scope, site_id)
    await db.refresh(site)
    data = _site_out(site, await _open_counts(db, scope))
    bus.publish(site.tenant_id, "site.updated", data)
    return {"error": error, "site": data, "rules": manager.rules_view(site, await _site_policy(db, site))}


@router.put("/sites/{site_id}/cameras/{device_id}")
async def place_camera(site_id: int, device_id: str, body: CameraPlaceIn, request: Request,
                       user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                       db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, site_id, write=True)
    device_id = device_id.strip("{}")
    names = await _site_cameras(site)
    if device_id not in names and device_id not in (site.camera_places or {}):
        raise HTTPException(404, "That camera isn't on this site")
    places = dict(site.camera_places or {})
    old = places.get(device_id, {})
    places[device_id] = {"name": names.get(device_id) or old.get("name", ""),
                         "lat": round(body.lat, 7), "lng": round(body.lng, 7), "heading": round(body.heading % 360, 2),
                         "fov": round(body.fov, 2), "range_m": round(body.range_m, 1)}
    for k, digits in (("height_m", 2), ("tilt", 2), ("roll", 2)):
        v = getattr(body, k)
        if v is not None:
            places[device_id][k] = round(v, digits)
        elif k in old:
            places[device_id][k] = old[k]
    site.camera_places = places
    audit(db, site.tenant_id, "site.camera_placed", user_id=user.id, site_id=site.id, ip=client_ip(request),
          device_id=device_id, camera=places[device_id]["name"])
    await db.commit()
    data = _site_out(site, await _open_counts(db, scope))
    bus.publish(site.tenant_id, "site.updated", data)
    return data


@router.delete("/sites/{site_id}/cameras/{device_id}")
async def unplace_camera(site_id: int, device_id: str, request: Request, user: User = Depends(require_admin),
                         scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, site_id, write=True)
    device_id = device_id.strip("{}")
    places = dict(site.camera_places or {})
    gone = places.pop(device_id, None)
    if gone is None:
        raise HTTPException(404, "That camera isn't placed")
    site.camera_places = places or None
    audit(db, site.tenant_id, "site.camera_removed", user_id=user.id, site_id=site.id, ip=client_ip(request),
          device_id=device_id, camera=gone.get("name", ""))
    await db.commit()
    data = _site_out(site, await _open_counts(db, scope))
    bus.publish(site.tenant_id, "site.updated", data)
    return data


@router.post("/sites/{site_id}/{op}")
async def site_op(site_id: int, op: str, request: Request, user: User = Depends(require_admin),
                  scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    if op not in ("enable", "disable", "archive"):
        raise HTTPException(404)
    site = await _get_site(db, scope, site_id, write=True)
    if op == "enable":
        site.enabled = True
    elif op == "disable":
        site.enabled = False
    else:
        site.enabled = False
        site.archived_at = datetime.now(timezone.utc)
    audit(db, site.tenant_id, f"site.{op}d" if op != "archive" else "site.archived",
          user_id=user.id, site_id=site.id, ip=client_ip(request))
    await db.commit()
    await manager.restart(site)
    data = site_dict(site)
    bus.publish(site.tenant_id, "site.removed" if op == "archive" else "site.updated", data)
    return data


# -------------------------------------------------------------------------- alarms

class AckIn(BaseModel):
    note: str = Field(default="", max_length=4000)
    verdict: str = Field(default="", pattern="^(|real|false|dismissed)$")     # optional so pages opened before verdicts still ack


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Note is empty")
        return v.strip()


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    verdict: str = Field(pattern="^(real|false|dismissed)$")
    note: str = Field(default="", max_length=4000)


@router.get("/alarms")
async def list_alarms(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                      state: str = Query("open", pattern="^(open|active|acknowledged|disarmed|all)$"),
                      site_id: int | None = None, category: str | None = Query(None, pattern="^(security|system)$"),
                      priority: int | None = Query(None, ge=1, le=3),
                      q: str | None = Query(None, max_length=200),
                      verdict: str | None = Query(None, pattern="^(real|false|dismissed|none)$"),
                      tenant_id: int | None = None,
                      alerting: bool = False,
                      before_id: int | None = None, after_id: int | None = None,
                      order: str = Query("newest", pattern="^(newest|oldest)$"),
                      limit: int = Query(100, ge=1, le=500)):
    """alerting=1 (the page's open-alarm store) also includes the user's own company while they view
    another one, so their own alarms keep sounding. order=oldest (pages with after_id) is the Alarms page's
    open list: the oldest waiting alarm first."""
    stmt = select(Alarm).where(scope.alerting_where(Alarm.tenant_id) if alerting else scope.where(Alarm.tenant_id))
    if tenant_id:
        stmt = stmt.where(Alarm.tenant_id == tenant_id)
    if state == "open":
        stmt = stmt.where(Alarm.state == "new")
    elif state == "active":                          # calls in progress: a guard was sent, nobody has ended it yet
        stmt = stmt.where(Alarm.call_state == "active")
    elif state in ("acknowledged", "disarmed"):
        stmt = stmt.where(Alarm.state == state)
    if site_id:
        stmt = stmt.where(Alarm.site_id == site_id)
    if category:
        stmt = stmt.where(Alarm.category == category)
    if priority:
        stmt = stmt.where(Alarm.priority == priority)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Alarm.caption.ilike(like) | Alarm.source_name.ilike(like) | Alarm.description.ilike(like))
    if verdict:
        stmt = stmt.where(Alarm.verdict == ("" if verdict == "none" else verdict))
    if before_id:
        stmt = stmt.where(Alarm.id < before_id)
    if after_id:
        stmt = stmt.where(Alarm.id > after_id)
    rows = (await db.scalars(stmt.order_by(Alarm.id.asc() if order == "oldest" else Alarm.id.desc()).limit(limit))).unique().all()
    return [alarm_dict(a) for a in rows]


@router.post("/alarms/bulk")
async def bulk_edit(body: BulkIn, request: Request, user: User = Depends(require("alarms.bulk_edit")),
                    scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Admin override: mark many alarms real/false/dismissed. Open ones are acknowledged with that verdict
    (and the note); closed ones get their verdict changed. Every change is audit-logged."""
    ids = sorted(set(body.ids))
    ip, n = client_ip(request), len(ids)
    acked, changed, unchanged = [], [], []
    confirmed, withdrawn = [], []              # for texts: became Real event / stopped being Real event
    for aid in ids:
        alarm = await db.scalar(select(Alarm).where(Alarm.id == aid, scope.where(Alarm.tenant_id))
                                .execution_options(populate_existing=True))
        if alarm is None or not scope.can_write(alarm.tenant_id):
            unchanged.append(aid)
            continue
        if alarm.state == "new":
            try:
                await ack_service.acknowledge(db, alarm, user, body.note, ip=ip, verdict=body.verdict, bulk=n)
                acked.append(aid)
                if body.verdict == "real":
                    confirmed.append(aid)
                continue
            except ack_service.AlreadyAcknowledged:     # someone acked it meanwhile: fall through to the verdict
                alarm = await db.scalar(select(Alarm).where(Alarm.id == aid).execution_options(populate_existing=True))
        was = alarm.verdict
        if await ack_service.set_verdict(db, alarm, user, body.verdict, body.note, ip=ip, bulk=n):
            await db.commit()
            await db.refresh(alarm)
            bus.publish(alarm.tenant_id, "alarm.updated", alarm_dict(alarm))
            changed.append(aid)
            if body.verdict == "real":
                confirmed.append(aid)
            elif was == "real":
                withdrawn.append((aid, body.verdict))
        else:
            unchanged.append(aid)
    if confirmed or withdrawn:
        from app import sms
        sms.notify_verdicts(confirmed, withdrawn, user.id)
    return {"acknowledged": acked, "changed": changed, "unchanged": unchanged}


async def _get_alarm(db: AsyncSession, scope: Scope, alarm_id: int, write: bool = False) -> Alarm:
    # alerting_where: your own company's alarms stay reachable (e.g. a critical pop-up) while viewing another.
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, scope.alerting_where(Alarm.tenant_id)))
    if alarm is None:
        raise HTTPException(404, "Alarm not found")
    if write:
        scope.require_write(alarm.tenant_id)
    return alarm


@router.get("/alarms/{alarm_id}")
async def get_alarm(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    return alarm_dict(await _get_alarm(db, scope, alarm_id))


@router.post("/alarms/{alarm_id}/ack")
async def ack_alarm(alarm_id: int, body: AckIn, request: Request, scope: Scope = Depends(get_scope),
                    db: AsyncSession = Depends(get_db)):
    alarm = await _get_alarm(db, scope, alarm_id, write=True)
    try:
        alarm = await ack_service.acknowledge(db, alarm, scope.user, body.note, ip=client_ip(request), verdict=body.verdict)
    except ack_service.AlreadyAcknowledged as exc:
        raise HTTPException(409, "Alarm was already acknowledged") from exc
    return alarm_dict(alarm)


class EndCallIn(BaseModel):
    note: str = Field(default="", max_length=4000)


@router.post("/alarms/{alarm_id}/end-call")
async def end_call(alarm_id: int, body: EndCallIn, request: Request, scope: Scope = Depends(get_scope),
                   db: AsyncSession = Depends(get_db)):
    """End a call (a dispatched alarm): it leaves Active calls; dispatched guards who haven't cleared are told."""
    from app.services import calls
    alarm = await _get_alarm(db, scope, alarm_id, write=True)
    try:
        alarm = await calls.end(db, alarm, scope.user, body.note, ip=client_ip(request))
    except calls.CallError as e:
        raise HTTPException(400, str(e)) from None
    return alarm_dict(alarm)


@router.get("/alarms/{alarm_id}/notes")
async def list_notes(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    alarm = await _get_alarm(db, scope, alarm_id)
    rows = (await db.scalars(select(AlarmNote).where(AlarmNote.alarm_id == alarm.id).order_by(AlarmNote.id))).all()
    return [note_dict(n) for n in rows]


@router.post("/alarms/{alarm_id}/notes")
async def add_note(alarm_id: int, body: NoteIn, request: Request, scope: Scope = Depends(get_scope),
                   db: AsyncSession = Depends(get_db)):
    """Follow-up note on any alarm, open or closed. Append-only; broadcast so open drawers update."""
    user = scope.user
    alarm = await _get_alarm(db, scope, alarm_id, write=True)
    note = AlarmNote(tenant_id=alarm.tenant_id, alarm_id=alarm.id, user_id=user.id, text=body.text)
    note.user = user
    db.add(note)
    await db.flush()
    audit(db, alarm.tenant_id, "alarm.note", user_id=user.id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), note_id=note.id, text=body.text)
    await db.commit()
    data = note_dict(note)
    bus.publish(alarm.tenant_id, "alarm.note", data)
    return data


VIEW_AGAIN_S = 300      # the same person re-opening the same alarm within 5 min isn't logged again


@router.post("/alarms/{alarm_id}/viewed")
async def alarm_viewed(alarm_id: int, request: Request, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """The alarm details were opened: on the timeline and in the incident report (the user, 2026-10-14: "add when
    it was viewed"). Read access is enough; re-opens within VIEW_AGAIN_S are skipped."""
    user = scope.user
    alarm = await _get_alarm(db, scope, alarm_id)
    now = datetime.now(timezone.utc)
    seen = select(AuditLog.id).where(AuditLog.alarm_id == alarm.id, AuditLog.action == "alarm.viewed").limit(1)
    if await db.scalar(seen.where(AuditLog.user_id == user.id, AuditLog.ts >= now - timedelta(seconds=VIEW_AGAIN_S))):
        return {"logged": False}
    first = await db.scalar(seen) is None
    received = alarm.received_at.replace(tzinfo=alarm.received_at.tzinfo or timezone.utc)
    audit(db, alarm.tenant_id, "alarm.viewed", user_id=user.id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), first=first, state=alarm.state, after_s=max(0, round((now - received).total_seconds())))
    await db.commit()
    return {"logged": True}


@router.get("/summary")
async def summary(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    counts = await _open_counts(db, scope)
    total = lambda p: sum(c.get(p, 0) for c in counts.values())  # noqa: E731
    return {"open_critical": total(1), "open_alarm": total(2), "open_warning": total(3)}


# --------------------------------------------------------------------------- audit

@router.get("/audit")
async def list_audit(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                     action: str | None = Query(None, max_length=64), site_id: int | None = None,
                     alarm_id: int | None = None, before_id: int | None = None,
                     limit: int = Query(100, ge=1, le=500)):
    stmt = select(AuditLog).where(scope.where(AuditLog.tenant_id))
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if site_id:
        stmt = stmt.where(AuditLog.site_id == site_id)
    if alarm_id:
        stmt = stmt.where(AuditLog.alarm_id == alarm_id)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    rows = (await db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit))).unique().all()
    return [audit_dict(r) for r in rows]


# ------------------------------------------------------------------------ settings

class AlarmPolicyIn(BaseModel):
    levels: dict[str, str]
    force_ack_critical: bool = True
    tagged_only: bool | None = None          # None = keep (pages opened before the setting existed)


def _policy_payload(policy: Policy) -> dict:
    return {
        "force_ack_critical": policy.force_ack_critical,
        "tagged_only": policy.tagged_only,
        "types": [{"type": t, "label": label, "category": cat, "default": default, "level": policy.level_for(t)}
                  for t, (label, cat, default) in EVENT_TYPES.items()],
    }


def _one_company(scope: Scope) -> Tenant:
    """Per-company pages (settings, users, groups) need a single company in view."""
    if scope.tenant is None:
        raise HTTPException(400, "Pick a company first: this can't be done in the All companies view.")
    return scope.tenant


@router.get("/settings/alarm-levels")
async def get_alarm_levels(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, _one_company(scope).id)
    return _policy_payload(Policy.from_settings(tenant.settings))


@router.put("/settings/alarm-levels")
async def put_alarm_levels(body: AlarmPolicyIn, request: Request, user: User = Depends(require_admin),
                           scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tid = scope.write_tenant()
    bad = {t: lv for t, lv in body.levels.items() if lv not in CHOICES or t not in EVENT_TYPES}
    if bad:
        raise HTTPException(400, f"Unknown event type or level: {bad}")
    tenant = await db.get(Tenant, tid)
    old = Policy.from_settings(tenant.settings)
    # Store only differences from the defaults so future default changes still apply.
    overrides = {t: lv for t, lv in body.levels.items() if lv != EVENT_TYPES[t][2]}
    policy = Policy(levels=overrides, force_ack_critical=body.force_ack_critical,
                    tagged_only=old.tagged_only if body.tagged_only is None else body.tagged_only)
    tenant.settings = {**(tenant.settings or {}), "alarm_policy": policy.to_settings()}

    # Re-level alarms that are still open so the console reflects the change right away.
    for t in EVENT_TYPES:
        level = policy.level_for(t)
        if level == "ignore":
            continue
        stmt = update(Alarm).where(Alarm.tenant_id == tid, Alarm.state == "new", Alarm.event_type == t)
        if policy.force_ack_critical:
            stmt = stmt.where(Alarm.nx_ack_required.is_(False))
        await db.execute(stmt.values(priority=LEVELS[level]))
    if policy.force_ack_critical:
        await db.execute(update(Alarm).where(Alarm.tenant_id == tid, Alarm.state == "new",
                                             Alarm.nx_ack_required.is_(True)).values(priority=1))

    changed = {t: [old.level_for(t), policy.level_for(t)] for t in EVENT_TYPES if old.level_for(t) != policy.level_for(t)}
    audit(db, tid, "settings.alarm_levels", user_id=user.id, ip=client_ip(request), changed=changed,
          force_ack_critical=policy.force_ack_critical, tagged_only=policy.tagged_only)
    await db.commit()
    bus.publish(tid, "alarms.reload", {})
    return _policy_payload(policy)


# --------------------------------------------------------------------------- users

class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)          # "" = invite by email (a setup link)
    role: str = Field(default="operator", pattern="^(admin|operator)$")
    phone: str = Field(default="", max_length=30)
    sms_level: str = Field(default="off", pattern="^(off|critical|alarm)$")
    sms_sites: list[int] | None = Field(default=None, max_length=1000)     # None = all sites
    sms_when: str = Field(default="raised", pattern="^(raised|confirmed|both)$")
    monitoring_station: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, pattern="^(admin|operator)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)                       # "" = remove
    sms_level: str | None = Field(default=None, pattern="^(off|critical|alarm)$")
    sms_when: str | None = Field(default=None, pattern="^(raised|confirmed|both)$")
    sms_all_sites: bool | None = None                                     # True = every site (clears sms_sites)
    monitoring_station: bool | None = None                                # never signed out for inactivity / max age
    sms_sites: list[int] | None = Field(default=None, max_length=1000)    # only these sites


async def _site_choice(db: AsyncSession, tenant_id: int, ids: list[int] | None) -> list[int] | None:
    """Validate "only these sites": the company's own sites, at least one. None = every site."""
    if ids is None:
        return None
    own = set((await db.scalars(select(Site.id).where(Site.tenant_id == tenant_id, Site.archived_at.is_(None)))).all())
    picked = sorted(set(ids) & own)
    if len(picked) != len(set(ids)):
        raise HTTPException(400, "Unknown site")
    if not picked:
        raise HTTPException(400, "Pick at least one site, or choose All sites")
    return picked


def _ask_consent(target: User) -> bool:
    """A number that should get texts but was never asked: mark it "pending" (the caller sends the request
    after committing). Texts off: stays "none" and the Users page offers "Send opt-in" later."""
    from app import platform_settings
    if target.phone and target.sms_level != "off" and target.sms_consent == "none" and platform_settings.current().sms_ready:
        target.sms_consent = "pending"
        return True
    return False


def _phone(raw: str) -> str:
    from app import sms
    try:
        return sms.normalize_phone(raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def _check_password(pw: str) -> None:
    from app import passwords
    problem = passwords.check(pw)
    if problem:
        raise HTTPException(400, problem)


def _user_dict(u: User, passkeys: int = 0, signed_in: int = 0, invite: dict | None = None, text: dict | None = None) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role,
            "phone": u.phone, "sms_level": u.sms_level, "sms_sites": u.sms_sites, "sms_when": u.sms_when, "monitoring_station": bool(u.monitoring_station), "last_text": text, "sms_consent": u.sms_consent,
            "sms_consent_at": u.sms_consent_at.isoformat() if u.sms_consent_at else None,
            "sms_optin_sent_at": u.sms_optin_sent_at.isoformat() if u.sms_optin_sent_at else None,
            "is_active": u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "totp_enabled": u.totp_enabled, "passkeys": passkeys, "sessions": signed_in,
            "invited": u.is_invited, "invited_at": u.invited_at.isoformat() if u.invited_at else None,
            "invite_email": invite}


@router.get("/users")
async def list_users(user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                     db: AsyncSession = Depends(get_db)):
    from app import sessions
    from app.models import WebAuthnCredential
    tid = _one_company(scope).id
    rows = (await db.scalars(select(User).where(User.tenant_id == tid, User.deleted_at.is_(None)).order_by(User.email))).all()
    ids = [u.id for u in rows]
    keys = dict((await db.execute(select(WebAuthnCredential.user_id, func.count()).where(WebAuthnCredential.user_id.in_(ids))
                                  .group_by(WebAuthnCredential.user_id))).all()) if ids else {}
    live: dict[int, int] = {}
    for r in await sessions.active(db, ids):
        live[r.user_id] = live.get(r.user_id, 0) + 1
    from app.models import EmailLog
    mails: dict[int, dict] = {}
    invited = [u.id for u in rows if u.is_invited]
    if invited:
        for e in (await db.scalars(select(EmailLog).where(EmailLog.user_id.in_(invited), EmailLog.purpose == "invite")
                                   .order_by(EmailLog.ts))).all():
            mails[e.user_id] = {"outcome": e.outcome, "ts": e.ts.isoformat(), "error": e.error}
    from app.models import SmsLog
    texts: dict[int, dict] = {}
    texted = [u.id for u in rows if u.phone]
    if texted:
        last = (select(SmsLog.user_id, func.max(SmsLog.id).label("id")).where(SmsLog.user_id.in_(texted))
                .group_by(SmsLog.user_id).subquery())
        for t in (await db.scalars(select(SmsLog).join(last, SmsLog.id == last.c.id))).all():
            texts[t.user_id] = {"outcome": t.outcome, "ts": t.ts.isoformat(), "error": t.error, "purpose": t.purpose}
    return [_user_dict(u, keys.get(u.id, 0), live.get(u.id, 0), mails.get(u.id), texts.get(u.id)) for u in rows]


@router.post("/users")
async def create_user(body: UserIn, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    from app import invites, sessions
    tid = scope.write_tenant()
    if body.password:
        _check_password(body.password)
    now = sessions.utcnow()
    phone = _phone(body.phone)
    sites = await _site_choice(db, tid, body.sms_sites)
    new = User(tenant_id=tid, email=body.email.strip().lower(), display_name=body.display_name.strip(),
               phone=phone, sms_level=body.sms_level if phone else "off", sms_sites=sites, sms_when=body.sms_when, monitoring_station=body.monitoring_station,
               password_hash=hash_password(body.password) if body.password else "", role=body.role,
               password_changed_at=now if body.password else None, invited_at=None if body.password else now)
    db.add(new)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "A user with that email already exists") from exc
    out = {}
    if not body.password:
        out = await invites.invite(db, new, await db.get(Tenant, tid), user)
    ask = _ask_consent(new)
    audit(db, tid, "user.created", user_id=user.id, ip=client_ip(request), email=new.email, role=new.role,
          invited=not body.password)
    await db.commit()
    if ask:
        from app import sms
        await sms.request_opt_in(new.id, user.label)
    return {**_user_dict(new), **out}


@router.post("/users/{user_id}/invite")
async def reinvite_user(user_id: int, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """A fresh setup link for someone who hasn't set up their account (the old link stops working)."""
    from app import invites
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if not target.is_invited:
        raise HTTPException(400, "They've already set up their account. Use Reset password, or they can use Forgot password.")
    out = await invites.invite(db, target, await db.get(Tenant, target.tenant_id), user)
    audit(db, target.tenant_id, "user.invited", user_id=user.id, ip=client_ip(request), target=target.email)
    await db.commit()
    return out


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if target.id == user.id and (body.role == "operator" or body.is_active is False):
        raise HTTPException(400, "You can't demote or deactivate your own account")
    fields = []
    if body.phone is not None:
        body.phone = _phone(body.phone)
        if not body.phone:
            body.sms_level = "off"
    if body.sms_level not in (None, "off") and not (body.phone if body.phone is not None else target.phone):
        raise HTTPException(400, "Add a mobile number first")
    for name in ("display_name", "role", "is_active", "phone", "sms_level", "sms_when", "monitoring_station"):
        value = getattr(body, name)
        if value is not None and getattr(target, name) != value:
            setattr(target, name, value)
            fields.append(name)
    if body.sms_all_sites or body.sms_sites is not None:
        sites = None if body.sms_all_sites else await _site_choice(db, target.tenant_id, body.sms_sites)
        if sites != target.sms_sites:
            target.sms_sites = sites
            fields.append("sms_sites")
    if "phone" in fields:                     # consent belongs to a number: a new one is asked again
        target.sms_consent, target.sms_consent_at, target.sms_optin_sent_at = "none", None, None
    ask = bool({"phone", "sms_level"} & set(fields)) and _ask_consent(target)
    if body.password:
        from app import sessions
        _check_password(body.password)
        target.password_hash, target.password_changed_at = hash_password(body.password), sessions.utcnow()
        # A reset ends their sessions (yours stays if you reset your own).
        await sessions.revoke_all(db, target, "password reset by an admin",
                                  keep_sid=request.session.get("sid") if target.id == user.id else None)
        fields.append("password")
    audit(db, target.tenant_id, "user.updated", user_id=user.id, ip=client_ip(request), target=target.email, fields=fields)
    await db.commit()
    if ask:
        from app import sms
        await sms.request_opt_in(target.id, user.label)
    return _user_dict(target)


@router.post("/users/{user_id}/opt-in")
async def send_opt_in(user_id: int, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """(Re)send the "Reply YES to get alarm texts" request."""
    from app import platform_settings, sms
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if not target.phone:
        raise HTTPException(400, "Add a mobile number first")
    if not platform_settings.current().sms_ready:
        raise HTTPException(400, "Text messages are off (Platform page → Text messages)")
    if target.sms_consent == "yes":
        raise HTTPException(400, "They've already replied YES")
    if target.sms_consent == "stopped":
        raise HTTPException(400, "They replied STOP, so carriers block texts to them. They can text START or YES to the portal's number to sign up again.")
    sent = target.sms_optin_sent_at
    if sent is not None and (sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc) - timedelta(minutes=1):
        raise HTTPException(429, "An opt-in request went less than a minute ago")
    if target.sms_level == "off":
        target.sms_level = "alarm"
    target.sms_consent = "pending"
    audit(db, target.tenant_id, "sms.opt_in_requested", user_id=user.id, ip=client_ip(request), target=target.email)
    await db.commit()
    await sms.request_opt_in(target.id, user.label)
    return _user_dict(target)


@router.post("/users/{user_id}/test-text")
async def test_text(user_id: int, request: Request, user: User = Depends(require_admin),
                    scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Text this person a test message (checks the number before an alarm needs it)."""
    from app import sms
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if not target.phone:
        raise HTTPException(400, "Add a mobile number first")
    if target.sms_consent != "yes":
        raise HTTPException(400, "They haven't replied YES yet, so they can't be texted. Use Send opt-in.")
    audit(db, target.tenant_id, "user.test_text", user_id=user.id, ip=client_ip(request), target=target.email)
    await db.commit()
    outcome, error = await sms.send_test(target.phone, user.label, user=target)
    return {"outcome": outcome, "error": error}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Delete someone: they vanish from Users and groups, are signed out everywhere, lose their password,
    two-step and links, and get no more texts. Their email is freed so they can be added again. The row
    stays (deleted_at) so alarms they acknowledged and the audit log still show their name."""
    from app import sessions
    from app.models import MfaRecoveryCode, UserToken, WebAuthnCredential
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if target.id == user.id:
        raise HTTPException(400, "You can't delete your own account")
    if target.is_admin and target.is_active and not await db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == target.tenant_id, User.id != target.id, User.role == "admin",
            User.is_active.is_(True), User.deleted_at.is_(None))):
        raise HTTPException(400, "They're the company's only admin. Make someone else an admin first.")
    email, name = target.email, target.label
    await sessions.revoke_all(db, target, "account deleted")
    for model in (MfaRecoveryCode, UserToken, WebAuthnCredential, UserGroupMember):
        await db.execute(model.__table__.delete().where(model.user_id == target.id))
    target.deleted_at, target.is_active = sessions.utcnow(), False
    target.email, target.display_name = f"deleted-{target.id}@deleted.invalid", name[:200]
    target.password_hash, target.totp_secret_enc, target.totp_enabled = "!deleted", "", False
    target.phone, target.sms_level, target.sms_sites, target.sms_consent = "", "off", None, "none"
    audit(db, target.tenant_id, "user.deleted", user_id=user.id, ip=client_ip(request), target=email, name=name)
    await db.commit()
    return {"deleted": target.id}


# -------------------------------------------------------------------------- groups

class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: list[str] = Field(default_factory=list)
    member_ids: list[int] = Field(default_factory=list, max_length=1000)


async def _groups_payload(db: AsyncSession, tenant: Tenant) -> dict:
    groups = (await db.scalars(select(UserGroup).where(UserGroup.tenant_id == tenant.id).order_by(UserGroup.name))).all()
    members = (await db.execute(select(UserGroupMember.group_id, UserGroupMember.user_id)
                                .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
                                .where(UserGroup.tenant_id == tenant.id))).all()
    by_group: dict[int, list[int]] = {}
    for gid, uid in members:
        by_group.setdefault(gid, []).append(uid)
    return {"permissions": applicable(tenant),
            "groups": [{"id": g.id, "name": g.name, "permissions": g.permissions or [],
                        "member_ids": sorted(by_group.get(g.id, []))} for g in groups]}


async def _save_group(db: AsyncSession, tenant: Tenant, group: UserGroup, body: GroupIn) -> tuple[list[int], list[int]]:
    allowed = applicable(tenant)       # platform.* can't be granted inside a customer company
    bad = [p for p in body.permissions if p not in allowed]
    if bad:
        raise HTTPException(400, f"Unknown permission(s) for this company: {', '.join(bad)}")
    group.name, group.permissions = body.name.strip(), sorted(set(body.permissions))
    try:                       # first, so a duplicate name is a clean 409 (not an autoflush error below)
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, f"A group named '{body.name}' already exists") from exc
    valid = set((await db.scalars(select(User.id).where(User.tenant_id == tenant.id, User.deleted_at.is_(None),
                                                        User.id.in_(body.member_ids or [0])))).all())
    old = set((await db.scalars(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group.id))).all())
    for uid in old - valid:
        await db.delete(await db.get(UserGroupMember, (group.id, uid)))
    for uid in valid - old:
        db.add(UserGroupMember(group_id=group.id, user_id=uid))
    return sorted(valid - old), sorted(old - valid)


async def _get_group(db: AsyncSession, scope: Scope, group_id: int) -> tuple[UserGroup, Tenant]:
    tenant = _one_company(scope)
    group = await db.scalar(select(UserGroup).where(UserGroup.id == group_id, UserGroup.tenant_id == tenant.id))
    if group is None:
        raise HTTPException(404)
    scope.require_write(tenant.id)
    return group, tenant


@router.get("/groups")
async def list_groups(user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    return await _groups_payload(db, _one_company(scope))


@router.post("/groups")
async def create_group(body: GroupIn, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, scope.write_tenant())
    group = UserGroup(tenant_id=tenant.id, name=body.name.strip(), permissions=[])
    db.add(group)
    added, _ = await _save_group(db, tenant, group, body)
    audit(db, tenant.id, "group.created", user_id=user.id, ip=client_ip(request), name=group.name,
          permissions=group.permissions, added=added)
    await db.commit()
    return await _groups_payload(db, tenant)


@router.put("/groups/{group_id}")
async def update_group(group_id: int, body: GroupIn, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    group, tenant = await _get_group(db, scope, group_id)
    before = list(group.permissions or [])
    added, removed = await _save_group(db, tenant, group, body)
    audit(db, tenant.id, "group.updated", user_id=user.id, ip=client_ip(request), name=group.name,
          permissions=group.permissions, permissions_before=before, added=added, removed=removed)
    await db.commit()
    return await _groups_payload(db, tenant)


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    group, tenant = await _get_group(db, scope, group_id)
    await db.execute(UserGroupMember.__table__.delete().where(UserGroupMember.group_id == group.id))
    audit(db, tenant.id, "group.deleted", user_id=user.id, ip=client_ip(request), name=group.name)
    await db.delete(group)
    await db.commit()
    return await _groups_payload(db, tenant)

# ------------------------------------------------------------------------- geocode

_GEOCODE_CACHE: dict[str, list] = {}
_geocode_lock = asyncio.Lock()
_last_geocode = 0.0


@router.get("/geocode")
async def geocode(q: str = Query(min_length=3, max_length=300), user: User = Depends(current_user)):
    """Address -> coordinates via Nominatim, proxied so we can honour its usage policy
    (identifying User-Agent, at most one request per second, cache repeats)."""
    global _last_geocode
    key = q.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    settings = get_settings()
    async with _geocode_lock:
        wait = 1.0 - (time.monotonic() - _last_geocode)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": settings.geocoder_user_agent}) as c:
                r = await c.get(settings.geocoder_url, params={"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 0})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Address lookup service unavailable") from exc
        finally:
            _last_geocode = time.monotonic()
    results = [{"label": x.get("display_name", ""), "lat": float(x["lat"]), "lng": float(x["lon"])}
               for x in r.json() if "lat" in x and "lon" in x]
    if len(_GEOCODE_CACHE) > 500:
        _GEOCODE_CACHE.clear()
    _GEOCODE_CACHE[key] = results
    return results

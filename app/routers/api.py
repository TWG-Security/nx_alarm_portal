"""JSON API used by the portal's own pages."""

import asyncio
import time
from datetime import datetime, timezone

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user, require_admin
from app.models import Alarm, AuditLog, Site, Tenant, User
from app.services.alarm_filter import CHOICES, EVENT_TYPES, LEVELS, Policy
from app.security import decrypt, encrypt, hash_password
from app.services import ack as ack_service
from app.services import arming
from app.services.bus import bus
from app.services.poller import manager, now_ms
from app.services.serialize import alarm_dict, audit_dict, site_dict
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

    @field_validator("name", "host", "nx_user")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ConnTest(BaseModel):
    host: str
    nx_user: str
    nx_pass: str = ""
    site_id: int | None = None


async def _open_counts(db: AsyncSession, tenant_id: int) -> dict[int, dict[int, int]]:
    """{site_id: {priority: open_count}}"""
    rows = await db.execute(
        select(Alarm.site_id, Alarm.priority, func.count())
        .where(Alarm.tenant_id == tenant_id, Alarm.state == "new")
        .group_by(Alarm.site_id, Alarm.priority)
    )
    out: dict[int, dict[int, int]] = {}
    for site_id, priority, n in rows.all():
        out.setdefault(site_id, {})[priority] = n
    return out


async def _get_site(db: AsyncSession, user: User, site_id: int) -> Site:
    site = await db.scalar(select(Site).where(Site.id == site_id, Site.tenant_id == user.tenant_id))
    if site is None:
        raise HTTPException(404, "Site not found")
    return site


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
async def list_sites(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sites = (await db.scalars(
        select(Site).where(Site.tenant_id == user.tenant_id, Site.archived_at.is_(None)).order_by(Site.name)
    )).all()
    counts = await _open_counts(db, user.tenant_id)
    return [{**site_dict(s, counts.get(s.id)), "push": manager.push_state(s.id), **manager.rule_info(s.id)} for s in sites]


@router.post("/sites/test")
async def test_connection(body: ConnTest, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    try:
        host, _ = resolve_host(body.host)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc
    password = body.nx_pass
    if not password and body.site_id:
        password = decrypt((await _get_site(db, user, body.site_id)).nx_pass_enc)
    info = await _probe(host, body.nx_user, password)
    info.pop("devices", None)
    return {"host": host, **info}


@router.post("/sites")
async def create_site(body: SiteIn, request: Request, user: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    try:
        host, cloud_id = resolve_host(body.host)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc
    if not body.nx_pass:
        raise HTTPException(400, {"message": "NX password is required"})
    site = Site(tenant_id=user.tenant_id, name=body.name, host=host, cloud_id=cloud_id, nx_user=body.nx_user,
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
    audit(db, user.tenant_id, "site.created", user_id=user.id, site_id=site.id, ip=client_ip(request),
          name=site.name, host=host, connected=body.connect)
    await db.commit()
    manager.start(site)
    data = site_dict(site)
    bus.publish(user.tenant_id, "site.updated", data)
    return data


@router.put("/sites/{site_id}")
async def update_site(site_id: int, body: SiteIn, request: Request, user: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, user, site_id)
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
    audit(db, user.tenant_id, "site.updated", user_id=user.id, site_id=site.id, ip=client_ip(request), fields=changed)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, {"message": f"A site named '{body.name}' already exists"}) from exc
    if creds_changed:
        await manager.restart(site)
    counts = await _open_counts(db, user.tenant_id)
    data = site_dict(site, counts.get(site.id))
    bus.publish(user.tenant_id, "site.updated", data)
    return data


class ArmIn(BaseModel):
    note: str = Field(default="", max_length=500)
    minutes: int | None = Field(default=None, ge=1, le=arming.MAX_DISARM_MINUTES)   # disarm only: auto re-arm after


@router.post("/sites/{site_id}/arm")
@router.post("/sites/{site_id}/disarm")
async def arm_site(site_id: int, body: ArmIn, request: Request, user: User = Depends(current_user),
                   db: AsyncSession = Depends(get_db)):
    """Operators arm/disarm by hand. It holds until the next scheduled change (or the disarm timer)."""
    site = await _get_site(db, user, site_id)
    if site.archived_at is not None:
        raise HTTPException(400, "Site is archived")
    armed = request.url.path.endswith("/arm")
    t = now_ms()
    until = t + body.minutes * 60_000 if body.minutes and not armed else None
    site.arm_override = {"armed": armed, "at_ms": t, "until_ms": until, "by": user.label, "user_id": user.id,
                         "note": body.note.strip()}
    site.armed = armed
    audit(db, user.tenant_id, "site.armed" if armed else "site.disarmed", user_id=user.id, site_id=site.id,
          ip=client_ip(request), source="manual", note=body.note.strip(), until_ms=until)
    await db.commit()
    counts = await _open_counts(db, user.tenant_id)
    data = {**site_dict(site, counts.get(site.id)), "push": manager.push_state(site.id), **manager.rule_info(site.id)}
    bus.publish(user.tenant_id, "site.updated", data)
    return data


@router.post("/sites/{site_id}/{op}")
async def site_op(site_id: int, op: str, request: Request, user: User = Depends(require_admin),
                  db: AsyncSession = Depends(get_db)):
    if op not in ("enable", "disable", "archive"):
        raise HTTPException(404)
    site = await _get_site(db, user, site_id)
    if op == "enable":
        site.enabled = True
    elif op == "disable":
        site.enabled = False
    else:
        site.enabled = False
        site.archived_at = datetime.now(timezone.utc)
    audit(db, user.tenant_id, f"site.{op}d" if op != "archive" else "site.archived",
          user_id=user.id, site_id=site.id, ip=client_ip(request))
    await db.commit()
    await manager.restart(site)
    data = site_dict(site)
    bus.publish(user.tenant_id, "site.removed" if op == "archive" else "site.updated", data)
    return data


# -------------------------------------------------------------------------- alarms

class AckIn(BaseModel):
    note: str = Field(default="", max_length=4000)


@router.get("/alarms")
async def list_alarms(user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                      state: str = Query("open", pattern="^(open|acknowledged|disarmed|all)$"),
                      site_id: int | None = None, category: str | None = Query(None, pattern="^(security|system)$"),
                      priority: int | None = Query(None, ge=1, le=3),
                      q: str | None = Query(None, max_length=200),
                      before_id: int | None = None, limit: int = Query(100, ge=1, le=500)):
    stmt = select(Alarm).where(Alarm.tenant_id == user.tenant_id)
    if state == "open":
        stmt = stmt.where(Alarm.state == "new")
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
    if before_id:
        stmt = stmt.where(Alarm.id < before_id)
    rows = (await db.scalars(stmt.order_by(Alarm.id.desc()).limit(limit))).unique().all()
    return [alarm_dict(a) for a in rows]


async def _get_alarm(db: AsyncSession, user: User, alarm_id: int) -> Alarm:
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, Alarm.tenant_id == user.tenant_id))
    if alarm is None:
        raise HTTPException(404, "Alarm not found")
    return alarm


@router.get("/alarms/{alarm_id}")
async def get_alarm(alarm_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return alarm_dict(await _get_alarm(db, user, alarm_id))


@router.post("/alarms/{alarm_id}/ack")
async def ack_alarm(alarm_id: int, body: AckIn, request: Request, user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    alarm = await _get_alarm(db, user, alarm_id)
    try:
        alarm = await ack_service.acknowledge(db, alarm, user, body.note, ip=client_ip(request))
    except ack_service.AlreadyAcknowledged as exc:
        raise HTTPException(409, "Alarm was already acknowledged") from exc
    return alarm_dict(alarm)


@router.get("/summary")
async def summary(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    counts = await _open_counts(db, user.tenant_id)
    total = lambda p: sum(c.get(p, 0) for c in counts.values())  # noqa: E731
    return {"open_critical": total(1), "open_alarm": total(2), "open_warning": total(3)}


# --------------------------------------------------------------------------- audit

@router.get("/audit")
async def list_audit(user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                     action: str | None = Query(None, max_length=64), site_id: int | None = None,
                     alarm_id: int | None = None, before_id: int | None = None,
                     limit: int = Query(100, ge=1, le=500)):
    stmt = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
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


def _policy_payload(policy: Policy) -> dict:
    return {
        "force_ack_critical": policy.force_ack_critical,
        "types": [{"type": t, "label": label, "category": cat, "default": default, "level": policy.level_for(t)}
                  for t, (label, cat, default) in EVENT_TYPES.items()],
    }


@router.get("/settings/alarm-levels")
async def get_alarm_levels(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, user.tenant_id)
    return _policy_payload(Policy.from_settings(tenant.settings))


@router.put("/settings/alarm-levels")
async def put_alarm_levels(body: AlarmPolicyIn, request: Request, user: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)):
    bad = {t: lv for t, lv in body.levels.items() if lv not in CHOICES or t not in EVENT_TYPES}
    if bad:
        raise HTTPException(400, f"Unknown event type or level: {bad}")
    tenant = await db.get(Tenant, user.tenant_id)
    old = Policy.from_settings(tenant.settings)
    # Store only differences from the defaults so future default changes still apply.
    overrides = {t: lv for t, lv in body.levels.items() if lv != EVENT_TYPES[t][2]}
    policy = Policy(levels=overrides, force_ack_critical=body.force_ack_critical)
    tenant.settings = {**(tenant.settings or {}), "alarm_policy": policy.to_settings()}

    # Re-level alarms that are still open so the console reflects the change right away.
    for t in EVENT_TYPES:
        level = policy.level_for(t)
        if level == "ignore":
            continue
        stmt = update(Alarm).where(Alarm.tenant_id == user.tenant_id, Alarm.state == "new", Alarm.event_type == t)
        if policy.force_ack_critical:
            stmt = stmt.where(Alarm.nx_ack_required.is_(False))
        await db.execute(stmt.values(priority=LEVELS[level]))
    if policy.force_ack_critical:
        await db.execute(update(Alarm).where(Alarm.tenant_id == user.tenant_id, Alarm.state == "new",
                                             Alarm.nx_ack_required.is_(True)).values(priority=1))

    changed = {t: [old.level_for(t), policy.level_for(t)] for t in EVENT_TYPES if old.level_for(t) != policy.level_for(t)}
    audit(db, user.tenant_id, "settings.alarm_levels", user_id=user.id, ip=client_ip(request), changed=changed,
          force_ack_critical=policy.force_ack_critical)
    await db.commit()
    bus.publish(user.tenant_id, "alarms.reload", {})
    return _policy_payload(policy)


# --------------------------------------------------------------------------- users

class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=12, max_length=200)
    role: str = Field(default="operator", pattern="^(admin|operator)$")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, pattern="^(admin|operator)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)


def _user_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


@router.get("/users")
async def list_users(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.email))).all()
    return [_user_dict(u) for u in rows]


@router.post("/users")
async def create_user(body: UserIn, request: Request, user: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    new = User(tenant_id=user.tenant_id, email=body.email.strip().lower(), display_name=body.display_name.strip(),
               password_hash=hash_password(body.password), role=body.role)
    db.add(new)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "A user with that email already exists") from exc
    audit(db, user.tenant_id, "user.created", user_id=user.id, ip=client_ip(request), email=new.email, role=new.role)
    await db.commit()
    return _user_dict(new)


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, request: Request, user: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    target = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == user.tenant_id))
    if target is None:
        raise HTTPException(404)
    if target.id == user.id and (body.role == "operator" or body.is_active is False):
        raise HTTPException(400, "You can't demote or deactivate your own account")
    fields = []
    for name in ("display_name", "role", "is_active"):
        value = getattr(body, name)
        if value is not None and getattr(target, name) != value:
            setattr(target, name, value)
            fields.append(name)
    if body.password:
        target.password_hash = hash_password(body.password)
        fields.append("password")
    audit(db, user.tenant_id, "user.updated", user_id=user.id, ip=client_ip(request), target=target.email, fields=fields)
    await db.commit()
    return _user_dict(target)


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

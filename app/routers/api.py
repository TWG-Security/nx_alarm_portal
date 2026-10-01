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

    @field_validator("name", "host", "nx_user")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


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
    verdict: str = Field(default="", pattern="^(|real|false)$")     # optional so pages opened before verdicts still ack


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
    verdict: str = Field(pattern="^(real|false)$")
    note: str = Field(default="", max_length=4000)


@router.get("/alarms")
async def list_alarms(scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                      state: str = Query("open", pattern="^(open|acknowledged|disarmed|all)$"),
                      site_id: int | None = None, category: str | None = Query(None, pattern="^(security|system)$"),
                      priority: int | None = Query(None, ge=1, le=3),
                      q: str | None = Query(None, max_length=200),
                      verdict: str | None = Query(None, pattern="^(real|false|none)$"),
                      tenant_id: int | None = None,
                      alerting: bool = False,
                      before_id: int | None = None, limit: int = Query(100, ge=1, le=500)):
    """alerting=1 (the page's open-alarm store) also includes the user's own company while they view
    another one, so their own alarms keep sounding."""
    stmt = select(Alarm).where(scope.alerting_where(Alarm.tenant_id) if alerting else scope.where(Alarm.tenant_id))
    if tenant_id:
        stmt = stmt.where(Alarm.tenant_id == tenant_id)
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
    if verdict:
        stmt = stmt.where(Alarm.verdict == ("" if verdict == "none" else verdict))
    if before_id:
        stmt = stmt.where(Alarm.id < before_id)
    rows = (await db.scalars(stmt.order_by(Alarm.id.desc()).limit(limit))).unique().all()
    return [alarm_dict(a) for a in rows]


@router.post("/alarms/bulk")
async def bulk_edit(body: BulkIn, request: Request, user: User = Depends(require("alarms.bulk_edit")),
                    scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Admin override: mark many alarms real/false. Open ones are acknowledged with that verdict
    (and the note); closed ones get their verdict changed. Every change is audit-logged."""
    ids = sorted(set(body.ids))
    ip, n = client_ip(request), len(ids)
    acked, changed, unchanged = [], [], []
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
                continue
            except ack_service.AlreadyAcknowledged:     # someone acked it meanwhile: fall through to the verdict
                alarm = await db.scalar(select(Alarm).where(Alarm.id == aid).execution_options(populate_existing=True))
        if await ack_service.set_verdict(db, alarm, user, body.verdict, body.note, ip=ip, bulk=n):
            await db.commit()
            await db.refresh(alarm)
            bus.publish(alarm.tenant_id, "alarm.updated", alarm_dict(alarm))
            changed.append(aid)
        else:
            unchanged.append(aid)
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


def _policy_payload(policy: Policy) -> dict:
    return {
        "force_ack_critical": policy.force_ack_critical,
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
    policy = Policy(levels=overrides, force_ack_critical=body.force_ack_critical)
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
          force_ack_critical=policy.force_ack_critical)
    await db.commit()
    bus.publish(tid, "alarms.reload", {})
    return _policy_payload(policy)


# --------------------------------------------------------------------------- users

class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)          # "" = invite by email (a setup link)
    role: str = Field(default="operator", pattern="^(admin|operator)$")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, pattern="^(admin|operator)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=200)


def _check_password(pw: str) -> None:
    from app import passwords
    problem = passwords.check(pw)
    if problem:
        raise HTTPException(400, problem)


def _user_dict(u: User, passkeys: int = 0, signed_in: int = 0, invite: dict | None = None) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role,
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
    rows = (await db.scalars(select(User).where(User.tenant_id == tid).order_by(User.email))).all()
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
    return [_user_dict(u, keys.get(u.id, 0), live.get(u.id, 0), mails.get(u.id)) for u in rows]


@router.post("/users")
async def create_user(body: UserIn, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    from app import invites, sessions
    tid = scope.write_tenant()
    if body.password:
        _check_password(body.password)
    now = sessions.utcnow()
    new = User(tenant_id=tid, email=body.email.strip().lower(), display_name=body.display_name.strip(),
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
    audit(db, tid, "user.created", user_id=user.id, ip=client_ip(request), email=new.email, role=new.role,
          invited=not body.password)
    await db.commit()
    return {**_user_dict(new), **out}


@router.post("/users/{user_id}/invite")
async def reinvite_user(user_id: int, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """A fresh setup link for someone who hasn't set up their account (the old link stops working)."""
    from app import invites
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id)))
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
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id)))
    if target is None:
        raise HTTPException(404)
    scope.require_write(target.tenant_id)
    if target.id == user.id and (body.role == "operator" or body.is_active is False):
        raise HTTPException(400, "You can't demote or deactivate your own account")
    fields = []
    for name in ("display_name", "role", "is_active"):
        value = getattr(body, name)
        if value is not None and getattr(target, name) != value:
            setattr(target, name, value)
            fields.append(name)
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
    return _user_dict(target)


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
    valid = set((await db.scalars(select(User.id).where(User.tenant_id == tenant.id,
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

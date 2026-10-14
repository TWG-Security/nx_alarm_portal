"""Dispatch: which guard is closest to an alarm, and the text that sends them there.

- candidates(db, alarm): the company's tracked guards and vehicles, closest first. A guard is "usable" with a
  position under STALE_S old from a router / phone that isn't offline; one can be texted when a person with a
  phone, who agreed to texts, is linked to it (Sites → Guards & vehicles).
- dispatch(...): texts that person (TWG DISPATCH, with the alarm and directions links; guards tracked by their
  phone's browser also get their location-sharing link), adds a note to the alarm, audit-logged.
- On confirm (sms._handle_confirmed): with the company's auto_dispatch on (the default), guards set to get
  Confirmed texts aren't all texted: only the closest usable one is dispatched. If none has a usable position they
  all get the confirmed text as before, so a confirmed alarm never reaches nobody.

Alarm safety: nothing here runs on the alarm's way in. Texts go out in background tasks (app/sms.py).
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import alarm_links, guard_share, sms
from app.audit import audit
from app.db import sessionmaker
from app.models import Alarm, AlarmNote, AuditLog, SmsLog, Tenant, Tracker, User, utcnow
from app.services.bus import bus
from app.services.trackers import aware, metres

log = logging.getLogger("portal.dispatch")

STALE_S = 900                 # a position older than this ranks after the fresh ones (and isn't auto-dispatched)


class DispatchError(Exception):
    """Shown to the operator as is."""


def target(alarm: Alarm) -> tuple[float, float, str] | None:
    """Where the alarm is: its camera's placed position, else the site's pin."""
    site = alarm.site
    if site is None:
        return None
    place = (site.camera_places or {}).get(alarm.device_id) if alarm.device_id else None
    if place and place.get("lat") is not None and place.get("lng") is not None:
        return float(place["lat"]), float(place["lng"]), "camera"
    if site.lat is not None and site.lng is not None:
        return site.lat, site.lng, "site"
    return None


def why_not(t: Tracker, u: User | None) -> str:
    if u is None or u.deleted_at is not None:
        return "No person linked to text (Sites → Guards & vehicles)"
    if not u.is_active:
        return f"{u.label} is deactivated"
    if not u.phone:
        return f"{u.label} has no phone number (Users)"
    if u.sms_consent != "yes":
        return f"{u.label} hasn't agreed to texts yet (they must reply YES)" if u.sms_consent != "stopped" \
            else f"{u.label} replied STOP to texts"
    return ""


async def candidates(db: AsyncSession, alarm: Alarm) -> list[dict]:
    rows = (await db.scalars(select(Tracker).where(Tracker.tenant_id == alarm.tenant_id, Tracker.enabled.is_(True)))).all()
    users = {u.id: u for u in (await db.scalars(select(User).where(User.id.in_([t.user_id for t in rows if t.user_id] or [0])))).all()}
    where = target(alarm)
    now = utcnow()
    out = []
    for t in rows:
        u = users.get(t.user_id) if t.user_id else None
        age = (now - aware(t.fix_at)).total_seconds() if t.fix_at else None
        dist = metres(where[0], where[1], t.lat, t.lng) if where and t.lat is not None else None
        usable = dist is not None and t.online is not False and age is not None and age < STALE_S
        reason = why_not(t, u)
        out.append({"tracker_id": t.id, "name": t.name, "kind": t.kind, "source": t.source or "peplink",
                    "person": u.label if u else "", "user_id": u.id if u else None,
                    "distance_m": round(dist) if dist is not None else None, "distance": sms.distance_words(dist),
                    "fix_at": aware(t.fix_at).isoformat() if t.fix_at else None, "online": t.online,
                    "usable": usable, "can_text": not reason, "reason": reason, "closest": False})
    out.sort(key=lambda c: (not c["usable"], c["distance_m"] is None, c["distance_m"] or 0, c["name"].lower()))
    for c in out:
        if c["usable"] and c["can_text"]:
            c["closest"] = True
            break
    return out


STATUSES = ("en_route", "on_scene", "clear")


async def dispatched(db: AsyncSession, alarm_id: int) -> list[dict]:
    """Everyone sent to this alarm, with how far they've got, like an MDT board: the dispatch text (sent /
    delivered / failed), when they opened the link, and their last status (en route / on scene / clear)."""
    audits = (await db.scalars(select(AuditLog).where(
        AuditLog.alarm_id == alarm_id, AuditLog.action.in_(("alarm.dispatched", "alarm.link_opened", "guard.status")))
        .order_by(AuditLog.id))).all()
    texts = (await db.scalars(select(SmsLog).where(SmsLog.alarm_id == alarm_id, SmsLog.purpose == "dispatch")
                              .order_by(SmsLog.id))).all()
    people: dict[int | str, dict] = {}
    iso = lambda dt: aware(dt).isoformat()  # noqa: E731
    for a in audits:
        d = a.detail or {}
        if a.action == "alarm.dispatched":
            key = d.get("to_user_id") or d.get("to")
            p = people.setdefault(key, {"user_id": d.get("to_user_id"), "to": d.get("to", ""), "guard": d.get("guard", ""),
                                        "opened_at": None, "status": "dispatched", "status_at": None})
            p.update(at=iso(a.ts), auto=bool(d.get("auto")), by=a.user.label if a.user else "", distance=d.get("distance"))
    for a in audits:
        p = people.get(a.user_id)
        if p is None or a.action == "alarm.dispatched":
            continue
        if a.action == "alarm.link_opened":
            p["opened_at"] = p["opened_at"] or iso(a.ts)
        elif (a.detail or {}).get("status") in STATUSES:
            p["status"], p["status_at"] = a.detail["status"], iso(a.ts)
    for t in texts:
        p = people.get(t.user_id)
        if p is not None:
            p["text"], p["text_error"] = t.outcome, t.error
    return sorted(people.values(), key=lambda p: p["at"])


def auto_on(tenant: Tenant | None) -> bool:
    return bool((tenant.settings or {}).get("auto_dispatch", True)) if tenant else True


async def dispatch(alarm_id: int, tracker_id: int, by: User | None, *, auto: bool = False, ip: str = "") -> dict:
    """Text the guard on `tracker_id` to go to the alarm. Returns {to, distance, phone}. Raises DispatchError."""
    arrived = time.time()
    async with sessionmaker()() as db:
        cfg = await sms.config(db)
        if cfg is None:
            raise DispatchError("Text messages are switched off or not set up (Platform → Text messages)")
        by = await db.get(User, by.id) if by is not None else None          # in this session
        alarm = await db.get(Alarm, alarm_id)
        t = await db.get(Tracker, tracker_id)
        if alarm is None or t is None or t.tenant_id != alarm.tenant_id or not t.enabled:
            raise DispatchError("That guard isn't tracked for this alarm's company")
        u = await db.get(User, t.user_id) if t.user_id else None
        reason = why_not(t, u)
        if reason:
            raise DispatchError(reason)
        where = target(alarm)
        dist = metres(where[0], where[1], t.lat, t.lng) if where and t.lat is not None else None
        token = await alarm_links.mint(db, alarm, u, cfg.link_hours)
        share = guard_share.share_url(t) if (t.source or "peplink") == "phone" else ""
        by_name = by.label if by else ""
        body = sms.dispatch_text(alarm, alarm_links.url(token), by_name, dist, share, auto=auto)
        away = sms.distance_words(dist)
        text = (f"Dispatched {t.name}" + (f" ({u.label})" if u.label != t.name else "")
                + (f", {away} away" if away else ", position unknown") + (" (automatically, on confirm)" if auto else ""))
        from app.services import calls
        calls.start(alarm)                       # a dispatched alarm is a call until it's ended
        note = AlarmNote(tenant_id=alarm.tenant_id, alarm_id=alarm.id, user_id=by.id if by else None, text=text)
        note.user = by
        db.add(note)
        await db.flush()
        audit(db, alarm.tenant_id, "alarm.dispatched", user_id=by.id if by else None, site_id=alarm.site_id, alarm_id=alarm.id,
              ip=ip, guard=t.name, to=u.label, to_user_id=u.id, distance_m=round(dist) if dist is not None else None,
              distance=away, auto=auto)
        await db.commit()
        from app.services.serialize import note_dict
        note_data = note_dict(note)
    bus.publish(alarm.tenant_id, "alarm.note", note_data)
    sms._spawn(_send(cfg, u, body, alarm, arrived, token))
    log.info("alarm %s: dispatched %s (%s)%s", alarm_id, t.name, away or "no position", " automatically" if auto else "")
    return {"to": t.name, "person": u.label, "distance": away, "phone": sms.masked(u.phone)}


async def _send(cfg, u: User, body: str, alarm: Alarm, arrived: float, token: str) -> None:
    media = sms.media_url(await sms.verified_media(alarm), token) if cfg.pictures else ""     # GIF, else the still
    await sms._send(cfg, u, body, "dispatch", alarm, arrived, media)


async def pick_closest(db: AsyncSession, alarm: Alarm, user_ids: set[int]) -> dict | None:
    """The closest usable guard among these people (auto-dispatch)."""
    for c in await candidates(db, alarm):
        if c["usable"] and c["can_text"] and c["user_id"] in user_ids:
            return c
    return None


async def already(db: AsyncSession, alarm_id: int) -> bool:
    """Someone was dispatched to it (the audit row is written at once; the text log only once Twilio answers)."""
    return await db.scalar(select(AuditLog.id).where(AuditLog.alarm_id == alarm_id, AuditLog.action == "alarm.dispatched")
                           .limit(1)) is not None

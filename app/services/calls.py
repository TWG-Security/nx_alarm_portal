"""Calls: once an alarm is confirmed a Real event, or a guard is dispatched, the alarm is a call (the user, 2026-10-19:
"when a real event happens it just clears it, this needs to turn into a call"). A Real event stays on screen (map page → Active calls)
until someone presses End call; False alarm or Dismissed end it on their own. (The user, 2026-10-13: "if a guard is
dispatched and its a real alarm we still want the alarm open so we need a end of dispatch or end of call".)

Ending a call texts every dispatched guard who hasn't tapped Clear: CALL ENDED (End call) or STAND DOWN (false
alarm / dismissed). Every step is on the alarm's timeline and in the incident report.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.models import Alarm, User

log = logging.getLogger("portal.calls")
STAND_DOWN = {"false": "a false alarm", "dismissed": "dismissed"}


class CallError(Exception):
    """Shown to the operator as is."""


def start(alarm: Alarm) -> bool:
    """A guard was dispatched: the alarm becomes (or is again) an active call, unless it's already known false."""
    if alarm.call_state == "active" or alarm.verdict in STAND_DOWN:
        return False
    alarm.call_state, alarm.call_ended_at, alarm.call_ended_by_id, alarm.call_end_note = "active", None, None, ""
    return True


def start_real(db: AsyncSession, alarm: Alarm, user: User | None, ip: str = "") -> bool:
    """A Real event verdict (acknowledge, or a verdict changed to real): the alarm stays on screen as an active call
    (map → Active calls, the app's dispatches) until End call, dispatched or not. The caller commits."""
    if not start(alarm):
        return False
    audit(db, alarm.tenant_id, "alarm.call_started", user_id=user.id if user else None, site_id=alarm.site_id,
          alarm_id=alarm.id, ip=ip, reason="real")
    return True


def mark_ended(db: AsyncSession, alarm: Alarm, user: User | None, *, note: str = "", verdict: str = "", ip: str = "") -> bool:
    """End the call (the caller commits). verdict "false" / "dismissed": ended by that verdict (STAND DOWN texts).
    The texts go once this session commits."""
    if alarm.call_state != "active":
        return False
    alarm.call_state, alarm.call_ended_at = "ended", datetime.now(timezone.utc)
    alarm.call_ended_by_id, alarm.call_end_note = (user.id if user else None), (note or "").strip()[:4000]
    audit(db, alarm.tenant_id, "alarm.call_ended", user_id=user.id if user else None, site_id=alarm.site_id,
          alarm_id=alarm.id, ip=ip, note=alarm.call_end_note, **({"verdict": verdict} if verdict else {}))
    _after_commit(db, alarm.id, user.label if user else "", verdict)
    return True


async def end(db: AsyncSession, alarm: Alarm, user: User, note: str = "", ip: str = "") -> Alarm:
    """The End call button."""
    if alarm.call_state != "active":
        raise CallError("This alarm has no call in progress")
    if alarm.state == "new":
        raise CallError("Acknowledge the alarm first (Real event, False alarm or Dismiss), then end the call")
    mark_ended(db, alarm, user, note=note, ip=ip)
    await db.commit()
    await db.refresh(alarm)
    from app.services.bus import bus
    from app.services.serialize import alarm_dict
    bus.publish(alarm.tenant_id, "alarm.updated", alarm_dict(alarm))
    log.info("alarm %s: call ended by %s", alarm.id, user.label)
    return alarm


def _after_commit(db: AsyncSession, alarm_id: int, by: str, verdict: str) -> None:
    from sqlalchemy import event
    jobs = db.info.setdefault("call_texts", [])
    jobs.append((alarm_id, by, verdict))
    if len(jobs) == 1:
        def _go(_session):
            from app import sms
            for aid, who, v in db.info.pop("call_texts", []):
                sms._spawn(_text_guards(aid, who, v))
                sms._spawn(_tell_apps(aid))
                from app import apns
                apns.call_ended(aid, who, v)
        event.listen(db.sync_session, "after_commit", _go, once=True)


async def _tell_apps(alarm_id: int) -> None:
    """`call_ended` {alarm_id} to the iPhone apps of everyone dispatched to it (their streams only)."""
    from sqlalchemy import select
    from app.db import sessionmaker
    from app.models import Alarm, AlarmLink
    from app.services.bus import bus
    async with sessionmaker()() as db:
        alarm = await db.get(Alarm, alarm_id)
        users = set((await db.scalars(select(AlarmLink.user_id).where(
            AlarmLink.alarm_id == alarm_id, AlarmLink.purpose == "dispatch", AlarmLink.user_id.is_not(None)))).all())
    if alarm is not None and users:
        bus.publish(alarm.tenant_id, "call_ended", {"alarm_id": alarm_id, "user_ids": sorted(users)})


async def _text_guards(alarm_id: int, by: str, verdict: str) -> None:
    """CALL ENDED / STAND DOWN to every dispatched guard who hasn't tapped Clear (and agreed to texts)."""
    import time
    from app import sms
    from app.db import sessionmaker
    from app.services import dispatch
    try:
        async with sessionmaker()() as db:
            cfg = await sms.config(db)
            alarm = await db.get(Alarm, alarm_id)
            if cfg is None or alarm is None:
                return
            plan = []
            for d in await dispatch.dispatched(db, alarm_id):
                u = await db.get(User, d["user_id"]) if d.get("user_id") else None
                if u is None or d["status"] == "clear" or not u.is_active or not u.phone or u.sms_consent != "yes":
                    continue
                plan.append(u)
            body = sms.call_ended_text(alarm, by, verdict)
        for u in plan:
            sms._spawn(sms._send(cfg, u, body, "call_ended", alarm, time.time()))
    except Exception:  # noqa: BLE001
        log.exception("calls: texts for the end of alarm %s's call failed", alarm_id)


# ---------------------------------------------------------------- alarms during an active call join it
# The user, 2026-10-19: "when an active call is going on, put all subsequent alarms into that call", option "Join the
# call": a new alarm at a site with an active call needs no acknowledgement of its own and adds no live-feed card; it's
# on the call's timeline and report, the Active calls card flashes with the update chime (alarm.joined_call is a
# GUARD_ACTION), and dispatched guards are told (text + app). Critical alarms, NX forced-acknowledge events and system
# events (site offline, storage) still raise on their own as well (linked to the call), so nothing critical is quiet.
def joins_quietly(alarm: Alarm) -> bool:
    return alarm.priority >= 2 and alarm.category == "security" and not alarm.nx_ack_required


async def join_active_call(db: AsyncSession, site_id: int, tenant_id: int, alarms: list[Alarm]) -> list[Alarm]:
    """Link new alarms (state "new") at this site to its active call, if it has one; the quiet ones are closed into it.
    Returns the alarms still to raise. The caller has flushed (ids exist) and commits."""
    from sqlalchemy import select
    if not alarms:
        return alarms
    ids = {a.id for a in alarms}
    call = await db.scalar(select(Alarm).where(Alarm.site_id == site_id, Alarm.tenant_id == tenant_id,
                                               Alarm.call_state == "active", Alarm.id.not_in(ids))
                           .order_by(Alarm.id.desc()).limit(1))
    if call is None:
        return alarms
    now, raise_, joined = datetime.now(timezone.utc), [], []
    for a in alarms:
        a.call_id = call.id
        quiet = joins_quietly(a)
        if quiet:
            a.state, a.acked_at, a.ack_note = "acknowledged", now, f"Joined the active call (alarm {call.id})"
        else:
            raise_.append(a)
        audit(db, tenant_id, "alarm.joined_call", site_id=site_id, alarm_id=call.id, joined_id=a.id, caption=a.caption,
              camera=a.source_name or "", priority=a.priority, raised=not quiet)
        audit(db, tenant_id, "alarm.added_to_call", site_id=site_id, alarm_id=a.id, call_id=a.call_id, raised=not quiet)
        joined.append((a.id, quiet))
    log.info("alarm call %s: joined %s", call.id, ", ".join(f"{i}{'' if q else ' (raised too)'}" for i, q in joined))
    _after_join(db, call.id, [i for i, _ in joined])
    return raise_


def _after_join(db: AsyncSession, call_id: int, joined_ids: list[int]) -> None:
    """Once committed: the joined alarms' new state to screens, and an update to the dispatched guards."""
    from sqlalchemy import event
    jobs = db.info.setdefault("call_joins", [])
    jobs.append((call_id, joined_ids))
    if len(jobs) == 1:
        def _go(_session):
            from app import sms
            for cid, aids in db.info.pop("call_joins", []):
                sms._spawn(_announce_joined(cid, aids))
        event.listen(db.sync_session, "after_commit", _go, once=True)


async def _announce_joined(call_id: int, joined_ids: list[int]) -> None:
    from sqlalchemy import select
    from app import apns, sms
    from app.db import sessionmaker
    from app.services import dispatch
    from app.services.bus import bus
    from app.services.serialize import alarm_dict
    async with sessionmaker()() as db:
        call = await db.get(Alarm, call_id)
        joined = (await db.scalars(select(Alarm).where(Alarm.id.in_(joined_ids)))).unique().all()
        for a in joined:
            if a.state != "new":
                bus.publish(a.tenant_id, "alarm.updated", alarm_dict(a))
        if call is None or call.call_state != "active":
            return
        people = [d["user_id"] for d in await dispatch.dispatched(db, call_id) if d.get("user_id") and d["status"] != "clear"]
        users = [u for u in [await db.get(User, uid) for uid in dict.fromkeys(people)] if u is not None and u.is_active]
        newest = max(joined, key=lambda a: a.id) if joined else None
        plan = []
        if newest is not None:
            cfg = await sms.config(db)
            for u in users:
                if cfg is not None and u.phone and u.sms_consent == "yes" and sms._allow(u.id, cfg.max_per_window):
                    from app import alarm_links
                    token = await alarm_links.mint(db, newest, u, cfg.link_hours)
                    plan.append((u, sms.call_update_text(call, newest, len(joined), alarm_links.url(token))))
            await db.commit()
    if newest is None:
        return
    for u, body in plan:
        sms._spawn(sms._send(cfg, u, body, "joined", newest, __import__("time").time()))
    for u in users:
        apns.call_update(call, newest, u.id, len(joined))

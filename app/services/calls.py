"""Calls: once a guard is dispatched, the alarm is a call. A Real event stays on screen (map page → Active calls)
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

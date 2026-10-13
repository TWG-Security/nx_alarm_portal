"""Alarm acknowledgement: write back to NX, then record locally and broadcast.

The local acknowledgement always goes through, even if the NX write-back fails:
the operator has handled the alarm either way. A failed write-back is stored in
nx_ack_result and shown in the UI so it can be followed up.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.models import Alarm, User
from app.services.bus import bus
from app.services.poller import manager
from app.services.serialize import alarm_dict
from app.services.sites import describe_http_error

log = logging.getLogger("portal.ack")

BOOKMARK_PRE_MS = 5_000
BOOKMARK_DURATION_MS = 30_000


class AlreadyAcknowledged(Exception):
    pass


async def write_back(alarm: Alarm, user: User, note: str, bookmark: bool = True) -> dict:
    """bookmark=False (a dismissal) skips the NX bookmark, but a forced-ack event is still cleared in NX
    so its notification doesn't stay pending in the NX Desktop client."""
    forced = bool(alarm.nx_ack_required and alarm.nx_action_id and alarm.nx_action_server_id and alarm.device_id)
    if not bookmark and not forced:
        return {"method": "skipped", "ok": True, "reason": "dismissed: no NX bookmark"}
    client = manager.client_for(alarm.site)
    name = f"Alarm ack: {alarm.caption or alarm.event_type}"[:200]
    description = f"Acknowledged in TWG Alarm Portal by {user.label}.\n{note}".strip()
    start = max(0, alarm.event_ts_ms - BOOKMARK_PRE_MS)
    if forced:
        method = "nx_acknowledge"
        call = client.acknowledge_event(alarm.device_id, alarm.nx_action_id, alarm.nx_action_server_id,
                                        start_time_ms=start, duration_ms=BOOKMARK_DURATION_MS, name=name,
                                        description=description, tags=["alarm-portal"])
    elif alarm.device_id:
        method = "bookmark"
        call = client.create_bookmark(alarm.device_id, name=name, duration_ms=BOOKMARK_DURATION_MS,
                                      start_time_ms=start, description=description, tags=["alarm-portal"])
    else:
        return {"method": "local_only", "ok": True, "reason": "event has no camera to bookmark"}
    try:
        resp = await call
        return {"method": method, "ok": True, "bookmark_id": (resp or {}).get("id") if isinstance(resp, dict) else None}
    except Exception as exc:  # noqa: BLE001
        log.warning("NX write-back failed for alarm %s: %s", alarm.id, exc)
        return {"method": method, "ok": False, "error": str(describe_http_error(exc))}


VERDICTS = {"real": "Real event", "false": "False alarm", "dismissed": "Dismissed"}


async def acknowledge(db: AsyncSession, alarm: Alarm, user: User, note: str, ip: str = "", verdict: str = "",
                      bulk: int = 0) -> Alarm:
    """verdict: "real" | "false" | "dismissed" | "" (pages opened before verdicts existed send none).
    "dismissed" closes the alarm without an NX bookmark (forced-ack events are still cleared in NX)."""
    note = (note or "").strip()[:4000]
    verdict = verdict if verdict in VERDICTS else ""
    # Atomic claim: only one operator can move an alarm out of "new", even if two click at once.
    acked_at = datetime.now(timezone.utc)
    values = dict(state="acknowledged", acked_at=acked_at, acked_by_id=user.id, ack_note=note)
    if verdict:
        values.update(verdict=verdict, verdict_by_id=user.id, verdict_at=acked_at)
    claimed = await db.execute(update(Alarm).where(Alarm.id == alarm.id, Alarm.state == "new").values(**values))
    if claimed.rowcount == 0:
        await db.rollback()
        raise AlreadyAcknowledged()
    await db.commit()

    result = await write_back(alarm, user, note, bookmark=verdict != "dismissed")
    await db.refresh(alarm)
    alarm.nx_ack_result = result
    audit(db, alarm.tenant_id, "alarm.acknowledged", user_id=user.id, site_id=alarm.site_id,
          alarm_id=alarm.id, ip=ip, note=note, nx=result, verdict=verdict, **({"bulk": bulk} if bulk else {}))
    if verdict in ("false", "dismissed"):           # a guard was sent: the call ends, they're told to stand down
        from app.services import calls
        calls.mark_ended(db, alarm, user, verdict=verdict, ip=ip)
    await db.commit()
    await db.refresh(alarm)
    bus.publish(alarm.tenant_id, "alarm.acked", alarm_dict(alarm))
    if verdict == "real" and not bulk:          # bulk edits text once for the batch (api.bulk_edit)
        from app import sms
        sms.notify_verdicts([alarm.id], [], user.id)
    return alarm


async def set_verdict(db: AsyncSession, alarm: Alarm, user: User, verdict: str, note: str = "", ip: str = "",
                      bulk: int = 0) -> bool:
    """Change the verdict on an alarm that is no longer open (acknowledged, or recorded while disarmed).
    Callers check the alarms.bulk_edit permission. Returns False if nothing changed."""
    if verdict not in VERDICTS or alarm.state == "new" or alarm.verdict == verdict:
        return False
    old = alarm.verdict
    alarm.verdict, alarm.verdict_by_id, alarm.verdict_at = verdict, user.id, datetime.now(timezone.utc)
    audit(db, alarm.tenant_id, "alarm.verdict", user_id=user.id, site_id=alarm.site_id, alarm_id=alarm.id, ip=ip,
          old=old, new=verdict, note=(note or "").strip()[:4000], **({"bulk": bulk} if bulk else {}))
    if verdict in ("false", "dismissed"):
        from app.services import calls
        calls.mark_ended(db, alarm, user, verdict=verdict, ip=ip)
    return True

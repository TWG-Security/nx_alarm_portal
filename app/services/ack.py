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


async def write_back(alarm: Alarm, user: User, note: str) -> dict:
    client = manager.client_for(alarm.site)
    name = f"Alarm ack: {alarm.caption or alarm.event_type}"[:200]
    description = f"Acknowledged in TWG Alarm Portal by {user.label}.\n{note}".strip()
    start = max(0, alarm.event_ts_ms - BOOKMARK_PRE_MS)
    if alarm.nx_ack_required and alarm.nx_action_id and alarm.nx_action_server_id and alarm.device_id:
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


async def acknowledge(db: AsyncSession, alarm: Alarm, user: User, note: str, ip: str = "") -> Alarm:
    note = (note or "").strip()[:4000]
    # Atomic claim: only one operator can move an alarm out of "new", even if two click at once.
    acked_at = datetime.now(timezone.utc)
    claimed = await db.execute(
        update(Alarm)
        .where(Alarm.id == alarm.id, Alarm.state == "new")
        .values(state="acknowledged", acked_at=acked_at, acked_by_id=user.id, ack_note=note)
    )
    if claimed.rowcount == 0:
        await db.rollback()
        raise AlreadyAcknowledged()
    await db.commit()

    result = await write_back(alarm, user, note)
    await db.refresh(alarm)
    alarm.nx_ack_result = result
    audit(db, alarm.tenant_id, "alarm.acknowledged", user_id=user.id, site_id=alarm.site_id,
          alarm_id=alarm.id, ip=ip, note=note, nx=result)
    await db.commit()
    await db.refresh(alarm)
    bus.publish(alarm.tenant_id, "alarm.acked", alarm_dict(alarm))
    return alarm

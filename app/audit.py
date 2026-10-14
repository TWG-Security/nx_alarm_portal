"""Helper for writing audit rows.

Rows about an alarm are its timeline (app/services/report.py `timeline`: the alarm details' Timeline and the
incident report). Once such a row is committed, open screens get "alarm.timeline" {alarm_id} so a dispatcher
watches the guard open the link, go en route and arrive, live.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

QUIET = {"alarm.received"}           # every alarm has one; screens already get the alarm itself


def audit(db: AsyncSession, tenant_id: int, action: str, *, user_id: int | None = None,
          site_id: int | None = None, alarm_id: int | None = None, ip: str = "", **detail) -> AuditLog:
    row = AuditLog(tenant_id=tenant_id, action=action, user_id=user_id, site_id=site_id,
                   alarm_id=alarm_id, ip=ip, detail=detail)
    db.add(row)
    if alarm_id is not None and action not in QUIET:
        timeline_changed(db, tenant_id, alarm_id)
    return row


def timeline_changed(db: AsyncSession, tenant_id: int, alarm_id: int) -> None:
    """Tell open screens the alarm's timeline grew, once this session commits (never before: they re-read it)."""
    from sqlalchemy import event
    pending = db.info.setdefault("timeline", set())
    if not pending:
        def _go(_session):
            from app.services.bus import bus
            for tid, aid in db.info.pop("timeline", set()):
                bus.publish(tid, "alarm.timeline", {"alarm_id": aid})
        event.listen(db.sync_session, "after_commit", _go, once=True)
    pending.add((tenant_id, alarm_id))

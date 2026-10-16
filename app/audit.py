"""Helper for writing audit rows.

Rows about an alarm are its timeline (app/services/report.py `timeline`: the alarm details' Timeline and the
incident report). Once such a row is committed, open screens get "alarm.timeline" {alarm_id} so a dispatcher
watches the guard open the link, go en route and arrive, live.
"""

import contextvars

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

QUIET = {"alarm.received"}           # every alarm has one; screens already get the alarm itself
# What the guard did from their alarm link (report._audit_row kind "guard"): the event says {"guard": true}, so an
# active call's dispatcher hears the update chime (the user, 2026-10-14: "if the guard says enroute it makes a noise").
GUARD_ACTIONS = {"alarm.link_opened", "alarm.link_cameras", "tracker.share_from_alarm", "guard.status"}


# Set while an MCP tool call runs (app/mcp_server.py): every audit row it causes says so, e.g. "MCP (Claude desk)".
VIA: contextvars.ContextVar[str] = contextvars.ContextVar("audit_via", default="")


def audit(db: AsyncSession, tenant_id: int, action: str, *, user_id: int | None = None,
          site_id: int | None = None, alarm_id: int | None = None, ip: str = "", **detail) -> AuditLog:
    if VIA.get() and "via" not in detail:
        detail["via"] = VIA.get()
    row = AuditLog(tenant_id=tenant_id, action=action, user_id=user_id, site_id=site_id,
                   alarm_id=alarm_id, ip=ip, detail=detail)
    db.add(row)
    if alarm_id is not None and action not in QUIET:
        timeline_changed(db, tenant_id, alarm_id, guard=action in GUARD_ACTIONS or (action == "alarm.note" and bool(detail.get("guard"))))
    return row


def timeline_changed(db: AsyncSession, tenant_id: int, alarm_id: int, guard: bool = False) -> None:
    """Tell open screens the alarm's timeline grew, once this session commits (never before: they re-read it)."""
    from sqlalchemy import event
    pending = db.info.setdefault("timeline", {})
    if not pending:
        def _go(_session):
            from app.services.bus import bus
            for (tid, aid), by_guard in db.info.pop("timeline", {}).items():
                bus.publish(tid, "alarm.timeline", {"alarm_id": aid, "guard": by_guard})
        event.listen(db.sync_session, "after_commit", _go, once=True)
    pending[(tenant_id, alarm_id)] = pending.get((tenant_id, alarm_id), False) or guard

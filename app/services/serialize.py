"""JSON shapes shared by the API and the SSE stream."""

import time
from datetime import timezone

from app.models import Alarm, AuditLog, Site
from app.services import arming
from app.services.alarm_filter import LEVEL_NAMES


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite drops the offset; values are always stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def alarm_dict(a: Alarm) -> dict:
    return {
        "id": a.id,
        "site_id": a.site_id,
        "site_name": a.site.name if a.site else "",
        "site_address": a.site.address if a.site else "",
        "level": LEVEL_NAMES.get(a.priority, "warning"),
        "level_source": a.level_source,
        "event_type": a.event_type,
        "event_subtype": a.event_subtype,
        "category": a.category,
        "priority": a.priority,
        "caption": a.caption,
        "description": a.description,
        "source_name": a.source_name,
        "device_id": a.device_id,
        "has_snapshot": bool(a.device_id),
        "event_ts_ms": a.event_ts_ms,
        "state": a.state,
        "nx_ack_required": a.nx_ack_required,
        "received_at": _iso(a.received_at),
        "acked_at": _iso(a.acked_at),
        "acked_by": a.acked_by.label if a.acked_by else None,
        "ack_note": a.ack_note,
        "verdict": a.verdict or "",
        "verdict_by": a.verdict_by.label if a.verdict_by else None,
        "verdict_at": _iso(a.verdict_at),
        "nx_ack_result": a.nx_ack_result,
    }


def site_dict(s: Site, open_counts: dict | None = None) -> dict:
    """open_counts: {priority: n} of unacknowledged alarms. Never includes credentials."""
    open_counts = open_counts or {}
    critical, alarm, warning = (open_counts.get(p, 0) for p in (1, 2, 3))
    if s.status in ("offline", "auth_error") or not s.enabled:
        marker = "offline"
    elif critical or alarm:
        marker = "alarm"
    elif warning:
        marker = "warn"
    else:
        marker = "ok"
    return {
        "id": s.id,
        "name": s.name,
        "cloud_id": s.cloud_id,
        "host": s.host,
        "nx_user": s.nx_user,
        "address": s.address,
        "lat": s.lat,
        "lng": s.lng,
        "notes": s.notes,
        "enabled": s.enabled,
        "status": s.status,
        "status_detail": s.status_detail,
        "last_seen_at": _iso(s.last_seen_at),
        "nx_site_name": s.nx_site_name,
        "nx_version": s.nx_version,
        "camera_count": s.camera_count,
        "open_critical": critical,
        "open_alarm": alarm,
        "open_warning": warning,
        "marker": marker,
        "timezone": s.timezone or "",
        "timezone_effective": arming.zone(s.timezone).key,
        "arm_schedule": (s.arm_schedule or {}).get("entries", []),
        "arming": arming.site_state(s, int(time.time() * 1000)).as_dict(),
    }


def audit_dict(r: AuditLog) -> dict:
    return {
        "id": r.id,
        "ts": _iso(r.ts),
        "action": r.action,
        "user": r.user.label if r.user else "system",
        "site_name": r.site.name if r.site else "",
        "site_id": r.site_id,
        "alarm_id": r.alarm_id,
        "ip": r.ip,
        "detail": r.detail,
    }

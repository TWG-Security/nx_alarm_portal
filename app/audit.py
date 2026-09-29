"""Helper for writing audit rows."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def audit(db: AsyncSession, tenant_id: int, action: str, *, user_id: int | None = None,
          site_id: int | None = None, alarm_id: int | None = None, ip: str = "", **detail) -> AuditLog:
    row = AuditLog(tenant_id=tenant_id, action=action, user_id=user_id, site_id=site_id,
                   alarm_id=alarm_id, ip=ip, detail=detail)
    db.add(row)
    return row

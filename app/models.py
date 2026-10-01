"""ORM models. Every table carries tenant_id so the portal can go multi-tenant later."""

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    """A security company using the portal. Exactly one is the platform (TWG Security): its staff can
    view every company, and with the right permissions support or manage them (app/scope.py)."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="customer")        # platform | customer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)           # False: its users can't sign in (sites stay monitored)
    display_name: Mapped[str] = mapped_column(String(200), default="")       # shown in their portal; "" = name
    logo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)   # PNG/JPEG/WebP, re-encoded on upload
    logo_type: Mapped[str] = mapped_column(String(50), default="")
    settings: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # e.g. {"alarm_policy": {...}}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def is_platform(self) -> bool:
        return self.kind == "platform"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="operator")  # admin | operator
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.display_name or self.email

    @property
    def permissions(self) -> set[str]:
        """Loaded per request by deps.current_user (admins: every permission their company can hold)."""
        if hasattr(self, "_perms"):
            return set(self._perms)
        from app.permissions import PLATFORM_PERMISSIONS, PERMISSIONS
        return set(PERMISSIONS) - PLATFORM_PERMISSIONS if self.is_admin else set()

    def can(self, perm: str) -> bool:
        return perm in self.permissions


class UserGroup(Base):
    """A named set of users with extra permissions (app/permissions.py), e.g. "Supervisors"."""

    __tablename__ = "user_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserGroupMember(Base):
    __tablename__ = "user_group_members"

    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)


class Site(Base):
    """One NX Witness deployment (an NX "site"), usually reached through the vmsproxy relay."""

    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_sites_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    cloud_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host: Mapped[str] = mapped_column(String(500))
    nx_user: Mapped[str] = mapped_column(String(200))
    nx_pass_enc: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(String(500), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    alarm_types: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # per-site {eventType: level} override

    # Arming (app/services/arming.py). The live state is computed from these; `armed` is only the
    # last state announced to browsers / the audit log, so the scheduler can spot changes.
    timezone: Mapped[str] = mapped_column(String(64), default="")                 # IANA name; "" = settings default
    arm_schedule: Mapped[dict | None] = mapped_column(JSONType, nullable=True)     # {"entries": [...], "since_ms": ...}
    arm_override: Mapped[dict | None] = mapped_column(JSONType, nullable=True)     # last manual arm/disarm
    armed: Mapped[bool] = mapped_column(Boolean, default=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|online|offline|auth_error
    status_detail: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_cursor_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    nx_site_name: Mapped[str] = mapped_column(String(200), default="")
    nx_version: Mapped[str] = mapped_column(String(50), default="")
    camera_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tenant: Mapped[Tenant] = relationship(lazy="joined")


class Alarm(Base):
    __tablename__ = "alarms"
    __table_args__ = (
        # One NX event can produce several event-log rows (one per rule action);
        # event_key collapses them so the operator sees a single alarm.
        UniqueConstraint("site_id", "event_key", name="uq_alarms_site_event"),
        Index("ix_alarms_tenant_state", "tenant_id", "state"),
        Index("ix_alarms_site_state", "site_id", "state"),
        Index("ix_alarms_event_ts", "event_ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    event_key: Mapped[str] = mapped_column(String(64))

    nx_action_id: Mapped[str] = mapped_column(String(64), default="")
    nx_action_server_id: Mapped[str] = mapped_column(String(64), default="")
    nx_ack_required: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_id: Mapped[str] = mapped_column(String(64), default="")

    event_type: Mapped[str] = mapped_column(String(100))
    event_subtype: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(20), default="security")  # security | system
    priority: Mapped[int] = mapped_column(Integer, default=2)              # 1 critical, 2 alarm, 3 warning
    level_source: Mapped[str] = mapped_column(String(20), default="")       # rule_tag | force_ack | site | tenant | default
    caption: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    source_name: Mapped[str] = mapped_column(String(300), default="")
    device_id: Mapped[str] = mapped_column(String(64), default="")
    event_ts_ms: Mapped[int] = mapped_column(BigInteger)

    state: Mapped[str] = mapped_column(String(20), default="new")  # new | acknowledged | disarmed (stored, not raised)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_note: Mapped[str] = mapped_column(Text, default="")
    # Operator's call on the event: "real" (a genuine incident) or "false" (false alarm); "" = not marked.
    verdict: Mapped[str] = mapped_column(String(10), default="")
    verdict_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nx_ack_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    raw: Mapped[dict] = mapped_column(JSONType, default=dict)

    site: Mapped[Site] = relationship(lazy="joined")
    acked_by: Mapped[User | None] = relationship(foreign_keys=[acked_by_id], lazy="joined")
    verdict_by: Mapped[User | None] = relationship(foreign_keys=[verdict_by_id], lazy="joined")


class AlarmNote(Base):
    """Follow-up notes on an alarm (e.g. "police on scene 11:45"). Append-only: never edited or deleted."""

    __tablename__ = "alarm_notes"
    __table_args__ = (Index("ix_alarm_notes_alarm", "alarm_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarms.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship(lazy="joined")


class AuditLog(Base):
    """Append-only record of everything operators and the system did."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_ts", "tenant_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True)
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarms.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship(lazy="joined")
    site: Mapped[Site | None] = relationship(lazy="joined")

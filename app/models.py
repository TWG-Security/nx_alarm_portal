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
    # Two-factor (app/mfa.py) and sessions (app/sessions.py)
    totp_secret_enc: Mapped[str] = mapped_column(Text, default="")              # Fernet; kept while enrolling
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # a code's step can't be reused
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)              # bump = sign out everywhere
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Deleted by an admin (api.delete_user): hidden, can't sign in or be texted; the row stays so history keeps the name
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Text messages (app/sms.py): E.164 mobile number and which alarms text them (off | critical | alarm)
    phone: Mapped[str] = mapped_column(String(20), default="")
    sms_level: Mapped[str] = mapped_column(String(10), default="off")
    sms_sites: Mapped[list | None] = mapped_column(JSONType, nullable=True)     # site ids that text them; None = every site
    # When they're texted: raised (the alarm goes off) | confirmed (an operator marks it Real event) | both
    sms_when: Mapped[str] = mapped_column(String(10), default="raised", server_default="raised")
    # Consent (carrier rules): none | pending (opt-in request sent) | yes (replied YES) | stopped (replied STOP)
    sms_consent: Mapped[str] = mapped_column(String(10), default="none")
    sms_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sms_optin_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_invited(self) -> bool:
        """Invited by email and hasn't set a password yet (password_hash is "")."""
        return not self.password_hash

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


class MfaRecoveryCode(Base):
    """Single-use recovery codes (SHA-256 of the normalised code), shown to the user once."""

    __tablename__ = "mfa_recovery_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserToken(Base):
    """A one-time link: "setup" (invite, 7 days) or "reset" (forgot password, 30 min). Only the SHA-256 is stored."""

    __tablename__ = "user_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(10))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class EmailLog(Base):
    """Every email the portal tried to send (never the body)."""

    __tablename__ = "email_log"
    __table_args__ = (Index("ix_email_log_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    to: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    purpose: Mapped[str] = mapped_column(String(20))          # invite | reset | test
    outcome: Mapped[str] = mapped_column(String(20))          # sent | failed | disabled
    error: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class SmsLog(Base):
    """Every text the portal tried to send (never the link: it opens the alarm without signing in)."""

    __tablename__ = "sms_log"
    __table_args__ = (Index("ix_sms_log_ts", "ts"), Index("ix_sms_log_user", "user_id", "ts"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    to: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(10))          # alarm | summary | test | optin | confirm | help | inbound
    outcome: Mapped[str] = mapped_column(String(20))          # sent | failed | disabled
    error: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    twilio_sid: Mapped[str] = mapped_column(String(64), default="")
    delay_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)       # alarm reached the portal -> Twilio accepted
    alarm_id: Mapped[int | None] = mapped_column(ForeignKey("alarms.id", ondelete="SET NULL"), nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class AlarmLink(Base):
    """A no-sign-in link to one alarm, sent by text. Only the SHA-256 of the token is stored."""

    __tablename__ = "alarm_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarms.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)


class WebAuthnCredential(Base):
    """A passkey. credential_id and public_key are base64url."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[str] = mapped_column(String(1400), unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    transports: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="Passkey")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Base):
    """One signed-in browser. The session cookie carries its id (sid); revoking the row signs it out."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user", "user_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    method: Mapped[str] = mapped_column(String(20), default="password")   # password | totp | recovery | passkey | sso | legacy
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(100), default="")


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
    # Where each camera is on the 3D view: {"<NX device id>": {name, lat, lng, heading, fov, range_m}}
    camera_places: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # 3D model shown in the 3D view (app/routers/site_models.py): {ready, job, next_version}
    model: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

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


# ---------------------------------------------------------------- platform & sign-in protection
# These are TWG-wide (not per company), so they carry no tenant_id. Edited on the Platform page.

class PlatformSettings(Base):
    """One row (id=1) of TWG-wide settings. Secrets are Fernet-encrypted (*_enc) and never sent back to browsers."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Sign-in protection (app/security_guard.py)
    ban_max_fails: Mapped[int] = mapped_column(Integer, default=5)            # failures from one IP within the window...
    ban_window_min: Mapped[int] = mapped_column(Integer, default=10)
    ban_first_min: Mapped[int] = mapped_column(Integer, default=15)           # ...ban it this long; each later ban x4
    ban_max_min: Mapped[int] = mapped_column(Integer, default=10080)          # longest temporary ban (7 days)
    ban_permanent_after: Mapped[int] = mapped_column(Integer, default=5)      # ban number N is permanent (0 = never)
    account_lock_max: Mapped[int] = mapped_column(Integer, default=10)        # failures for one email, any IP (0 = off)
    trusted_proxies: Mapped[str] = mapped_column(Text, default="")            # may send CF-Connecting-IP (app/net.py)
    # Cloudflare edge bans (app/cloudflare_edge.py)
    cf_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cf_zone_id: Mapped[str] = mapped_column(String(64), default="")
    cf_api_token_enc: Mapped[str] = mapped_column(Text, default="")
    cf_last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cf_last_error: Mapped[str] = mapped_column(Text, default="")
    cf_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Two-factor (app/mfa.py)
    mfa_require_twg: Mapped[bool] = mapped_column(Boolean, default=False)      # TWG's own users must use 2FA
    mfa_customers: Mapped[str] = mapped_column(String(20), default="company")  # company (each decides) | all
    passkeys_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sessions (app/sessions.py). A monitoring screen makes requests all the time, so it never goes idle.
    session_closed_h: Mapped[int] = mapped_column(Integer, default=12)         # signed out after this long with no requests
    session_max_h: Mapped[int] = mapped_column(Integer, default=0)             # absolute limit; 0 = none
    idle_timeout_min: Mapped[int] = mapped_column(Integer, default=0)          # no keyboard/mouse; 0 = off
    # Passwords (app/passwords.py)
    pw_min_length: Mapped[int] = mapped_column(Integer, default=12)
    pw_upper: Mapped[bool] = mapped_column(Boolean, default=True)
    pw_lower: Mapped[bool] = mapped_column(Boolean, default=True)
    pw_number: Mapped[bool] = mapped_column(Boolean, default=True)
    pw_symbol: Mapped[bool] = mapped_column(Boolean, default=True)
    pw_expiry_days: Mapped[int] = mapped_column(Integer, default=0)            # 0 = never
    # Email (app/mail.py)
    portal_url: Mapped[str] = mapped_column(String(300), default="https://alarmportal.twgsecurity.net")
    smtp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str] = mapped_column(String(200), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_tls: Mapped[str] = mapped_column(String(10), default="starttls")      # starttls | ssl | none
    smtp_user: Mapped[str] = mapped_column(String(320), default="")
    smtp_password_enc: Mapped[str] = mapped_column(Text, default="")
    smtp_from: Mapped[str] = mapped_column(String(320), default="")
    smtp_last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    smtp_last_error: Mapped[str] = mapped_column(Text, default="")
    smtp_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Google sign-in (app/routers/sso.py)
    google_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    google_client_id: Mapped[str] = mapped_column(String(300), default="")
    google_client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    google_domains: Mapped[str] = mapped_column(String(500), default="")       # "" = any domain
    # Text messages through Twilio (app/sms.py)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    twilio_sid: Mapped[str] = mapped_column(String(64), default="")
    twilio_token_enc: Mapped[str] = mapped_column(Text, default="")
    twilio_from: Mapped[str] = mapped_column(String(64), default="")           # +15705551234 or a Messaging Service SID (MG...)
    sms_link_hours: Mapped[int] = mapped_column(Integer, default=24)           # alarm links work this long
    sms_max_per_10min: Mapped[int] = mapped_column(Integer, default=10)        # per person; the rest go in one summary text
    sms_last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sms_last_error: Mapped[str] = mapped_column(Text, default="")
    sms_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sms_help_contact: Mapped[str] = mapped_column(String(300), default="")    # in HELP replies and on /sms-terms
    sms_last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sms_pictures: Mapped[bool] = mapped_column(Boolean, default=True)          # alarm texts go as MMS with the frame + boxes
    # Guard tracking through Peplink InControl2 (app/peplink.py, app/services/trackers.py)
    peplink_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    peplink_client_id: Mapped[str] = mapped_column(String(200), default="")
    peplink_secret_enc: Mapped[str] = mapped_column(Text, default="")
    peplink_poll_s: Mapped[int] = mapped_column(Integer, default=30)
    peplink_last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    peplink_last_error: Mapped[str] = mapped_column(Text, default="")
    peplink_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class AuthEvent(Base):
    """Every sign-in attempt (and admin clears). Failures drive IP bans and account locks."""

    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_ip_ts", "ip", "ts"), Index("ix_auth_events_email_ts", "email", "ts"),
                      Index("ix_auth_events_ts", "ts"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(320), default="")             # lower-cased as typed; "" if none
    kind: Mapped[str] = mapped_column(String(20))       # login | totp | recovery | passkey | reset | sso | admin
    outcome: Mapped[str] = mapped_column(String(20))    # success | failure | locked | denied | blocked | cleared
    reason: Mapped[str] = mapped_column(String(300), default="")
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class IpBan(Base):
    """A banned client IP. Rows outlive their ban (expires_at in the past) to remember repeat offenders."""

    __tablename__ = "ip_bans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), unique=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    ban_count: Mapped[int] = mapped_column(Integer, default=1)               # episodes; drives escalation
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    permanent: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    cf_rule_id: Mapped[str] = mapped_column(String(64), default="")         # the Cloudflare access rule we created
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IpAllow(Base):
    """IPs or ranges that are never banned (e.g. the TWG office)."""

    __tablename__ = "ip_allowlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), unique=True)                 # an address or a CIDR range
    label: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class TrackerSource(Base):
    """A company's InControl2 API client (its admins enter it on the Sites page). Its trackers are read with it."""

    __tablename__ = "tracker_sources"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", name="uq_tracker_sources_tenant_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="peplink")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    client_id: Mapped[str] = mapped_column(String(200), default="")
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    poll_s: Mapped[int] = mapped_column(Integer, default=30)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Tracker(Base):
    """Something whose live position shows on the map: a guard or patrol vehicle. Source today: a Peplink
    router with GPS, read through InControl2 (org / group / device ids)."""

    __tablename__ = "trackers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20), default="vehicle")          # vehicle | guard
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="peplink")
    ic2_org: Mapped[str] = mapped_column(String(32), default="")
    ic2_group: Mapped[int] = mapped_column(Integer, default=0)
    ic2_device: Mapped[int] = mapped_column(Integer, default=0)
    serial: Mapped[str] = mapped_column(String(40), default="")
    device_name: Mapped[str] = mapped_column(String(200), default="")         # the router's name in InControl2
    tz: Mapped[str] = mapped_column(String(64), default="")                   # the IC2 group's time zone (its timestamps are local)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)         # m/s
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)       # degrees, from the last two fixes
    fix_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)   # when the router got that position
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # The person carrying it: dispatch texts go to their phone (app/services/dispatch.py).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Router online in InControl2 / phone page sharing. None = not known yet. The last position is kept when offline.
    online: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    offline_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # source "phone": the guard's browser shares its location from /g/<token> (app/routers/share.py). The token's
    # SHA-256 finds the tracker; the token itself is kept encrypted so the same link can be texted again.
    share_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    share_enc: Mapped[str] = mapped_column(Text, default="")
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    share_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrackerPoint(Base):
    """Position history (breadcrumb trail), kept TRAIL_KEEP_DAYS."""

    __tablename__ = "tracker_points"
    __table_args__ = (Index("ix_tracker_points_tracker_ts", "tracker_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tracker_id: Mapped[int] = mapped_column(ForeignKey("trackers.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)

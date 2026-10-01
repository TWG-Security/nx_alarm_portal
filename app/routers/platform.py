"""Platform settings and sign-in security (TWG only). Viewing needs platform.view, changing platform.manage.

- GET  /platform                          the page
- GET  /api/platform/settings             all settings (secrets only as "set"/"not set")
- PUT  /api/platform/settings/sign-in     ban and account-lock rules
- PUT  /api/platform/settings/proxies     trusted tunnel connectors (private addresses only)
- PUT  /api/platform/settings/cloudflare  Cloudflare edge bans: token, zone, on/off
- POST /api/platform/cloudflare/test      read-only check of the saved token and zone
- PUT  /api/platform/settings/two-factor  require 2FA for TWG / every company (or each decides); passkeys on/off
- PUT  /api/platform/settings/sessions    how long a closed browser stays signed in; optional max age and idle sign-out
- PUT  /api/platform/settings/passwords   password rules and expiry
- PUT  /api/platform/settings/email       SMTP server (password encrypted, never returned), From, portal address
- POST /api/platform/email/test, GET /api/platform/email-log
- PUT  /api/platform/settings/google      Google sign-in: client ID, secret (encrypted), allowed domains, on/off
- GET  /api/platform/security             your IP, bans, allowlist, locked accounts, untrusted connectors
- GET  /api/platform/auth-events          recent sign-in attempts (filter by ip, email, outcome)
- POST /api/platform/bans, DELETE /api/platform/bans/{id}
- POST /api/platform/allowlist, DELETE /api/platform/allowlist/{id}
- POST /api/platform/locks/clear
Every change is audit-logged in TWG's own log (setting names only, never values).
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cloudflare_edge, net, platform_settings
from app import security_guard as guard
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.deps import client_ip, csrf_protect, render
from app.models import AuthEvent, IpAllow, IpBan, Tenant, User
from app.permissions import require
from app.scope import Scope, get_scope
from app.security import encrypt

router = APIRouter(dependencies=[Depends(csrf_protect)])
VIEW, MANAGE = require("platform.view"), require("platform.manage")


def _iso(dt):
    dt = guard.aware(dt)
    return dt.isoformat() if dt else None


@router.get("/platform")
async def platform_page(request: Request, user: User = Depends(VIEW), scope: Scope = Depends(get_scope)):
    return render(request, "platform.html", user, scope=scope, page="platform")


# ---------------------------------------------------------------- settings
def _settings_dict(row) -> dict:
    snap = platform_settings.current()
    env_trusted = platform_settings.parse_env(get_settings().trusted_proxy_ips)
    return {
        "sign_in": {"max_fails": row.ban_max_fails, "window_min": row.ban_window_min, "first_min": row.ban_first_min,
                    "max_min": row.ban_max_min, "permanent_after": row.ban_permanent_after,
                    "account_lock_max": row.account_lock_max},
        "proxies": {"trusted": row.trusted_proxies, "from_env": [str(n) for n in env_trusted]},
        "cloudflare": {"enabled": row.cf_enabled, "zone_id": row.cf_zone_id, "token_set": bool(row.cf_api_token_enc),
                       "token_readable": bool(snap.cf_token) or not row.cf_api_token_enc,
                       "last_ok_at": _iso(row.cf_last_ok_at), "last_error": row.cf_last_error,
                       "last_error_at": _iso(row.cf_last_error_at)},
        "two_factor": {"require_twg": row.mfa_require_twg, "customers": row.mfa_customers,
                       "passkeys_enabled": row.passkeys_enabled},
        "sessions": {"closed_h": row.session_closed_h, "max_h": row.session_max_h, "idle_min": row.idle_timeout_min},
        "passwords": {"min_length": row.pw_min_length, "upper": row.pw_upper, "lower": row.pw_lower,
                      "number": row.pw_number, "symbol": row.pw_symbol, "expiry_days": row.pw_expiry_days},
        "email": {"enabled": row.smtp_enabled, "host": row.smtp_host, "port": row.smtp_port, "tls": row.smtp_tls,
                  "user": row.smtp_user, "password_set": bool(row.smtp_password_enc), "sender": row.smtp_from,
                  "portal_url": row.portal_url, "last_ok_at": _iso(row.smtp_last_ok_at),
                  "last_error": row.smtp_last_error, "last_error_at": _iso(row.smtp_last_error_at)},
        "google": {"enabled": row.google_enabled, "client_id": row.google_client_id,
                   "secret_set": bool(row.google_client_secret_enc), "domains": row.google_domains,
                   "redirect_uri": f"{(row.portal_url or '').rstrip('/')}/auth/google/callback",
                   "origin": (row.portal_url or "").rstrip("/")},
        "updated_at": _iso(row.updated_at),
    }


@router.get("/api/platform/settings")
async def get_settings_(user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    row = await platform_settings.get_row(db)
    await db.commit()
    return {**_settings_dict(row), "can_manage": user.can("platform.manage")}


async def _saved(db, request, user, row, section: str, fields: list[str]):
    """Commit a settings change, audit the field names (never values) and reload the snapshot."""
    if fields:
        row.updated_by_id = user.id
        audit(db, user.tenant_id, "platform.settings", user_id=user.id, ip=client_ip(request), section=section,
              fields=fields)
    await db.commit()
    await platform_settings.refresh()
    return {**_settings_dict(row), "can_manage": True, "changed": fields}


class SignInRules(BaseModel):
    max_fails: int = Field(ge=2, le=100)
    window_min: int = Field(ge=1, le=1440)
    first_min: int = Field(ge=1, le=10080)
    max_min: int = Field(ge=1, le=525600)
    permanent_after: int = Field(ge=0, le=50)
    account_lock_max: int = Field(ge=0, le=1000)


_RULE_COLS = {"max_fails": "ban_max_fails", "window_min": "ban_window_min", "first_min": "ban_first_min",
              "max_min": "ban_max_min", "permanent_after": "ban_permanent_after", "account_lock_max": "account_lock_max"}


@router.put("/api/platform/settings/sign-in")
async def put_sign_in(body: SignInRules, request: Request, user: User = Depends(MANAGE),
                      db: AsyncSession = Depends(get_db)):
    if body.max_min < body.first_min:
        raise HTTPException(400, "The longest ban can't be shorter than the first ban.")
    if 0 < body.account_lock_max < body.max_fails:
        raise HTTPException(400, "The account lock should allow at least as many failures as an IP ban, "
                                 "or one person's typos lock their account before anything else happens.")
    row = await platform_settings.get_row(db)
    changed = []
    for k, col in _RULE_COLS.items():
        if getattr(row, col) != getattr(body, k):
            setattr(row, col, getattr(body, k))
            changed.append(k)
    return await _saved(db, request, user, row, "sign_in", changed)


class Proxies(BaseModel):
    trusted: str = Field(default="", max_length=2000)


@router.put("/api/platform/settings/proxies")
async def put_proxies(body: Proxies, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    try:
        nets = platform_settings.parse_networks(body.trusted)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for n in nets:
        if not net.is_internal(n.network_address) or not net.is_internal(n.broadcast_address):
            raise HTTPException(400, f"{n} is a public address. Only the tunnel connector on your own network "
                                     "(a private address like 10.0.2.58) should be trusted.")
        if n.num_addresses > 65536:
            raise HTTPException(400, f"{n} is too wide. Trust the connector's own address (or a small range).")
    text = ", ".join(str(n.network_address) if n.num_addresses == 1 else str(n) for n in nets)
    row = await platform_settings.get_row(db)
    changed = ["trusted_proxies"] if row.trusted_proxies != text else []
    row.trusted_proxies = text
    return await _saved(db, request, user, row, "proxies", changed)


class CloudflareIn(BaseModel):
    enabled: bool
    zone_id: str = Field(default="", max_length=64)
    api_token: str | None = Field(default=None, max_length=200)   # None = keep the saved one, "" = remove it


@router.put("/api/platform/settings/cloudflare")
async def put_cloudflare(body: CloudflareIn, request: Request, user: User = Depends(MANAGE),
                         db: AsyncSession = Depends(get_db)):
    zone = body.zone_id.strip().lower()
    if zone and not re.fullmatch(r"[0-9a-f]{32}", zone):
        raise HTTPException(400, "The Zone ID is 32 letters and digits (Cloudflare dashboard → your domain → "
                                 "Overview, right-hand column).")
    token = body.api_token.strip() if body.api_token is not None else None
    if token and not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", token):
        raise HTTPException(400, "That doesn't look like a Cloudflare API token.")
    row = await platform_settings.get_row(db)
    has_token = bool(token) if token is not None else bool(row.cf_api_token_enc)
    if body.enabled and not (zone and has_token):
        raise HTTPException(400, "Enter the API token and Zone ID before switching edge bans on.")
    changed = []
    if row.cf_enabled != body.enabled:
        row.cf_enabled = body.enabled
        changed.append("cf_enabled")
    if row.cf_zone_id != zone:
        row.cf_zone_id = zone
        changed.append("cf_zone_id")
    if token is not None:
        row.cf_api_token_enc = encrypt(token) if token else ""
        changed.append("cf_api_token")
    if {"cf_zone_id", "cf_api_token"} & set(changed):
        row.cf_last_ok_at, row.cf_last_error, row.cf_last_error_at = None, "", None
    return await _saved(db, request, user, row, "cloudflare", changed)


class TwoFactorIn(BaseModel):
    require_twg: bool
    customers: str = Field(pattern="^(company|all)$")
    passkeys_enabled: bool


@router.put("/api/platform/settings/two-factor")
async def put_two_factor(body: TwoFactorIn, request: Request, user: User = Depends(MANAGE),
                         db: AsyncSession = Depends(get_db)):
    row = await platform_settings.get_row(db)
    changed = []
    for k, col in (("require_twg", "mfa_require_twg"), ("customers", "mfa_customers"), ("passkeys_enabled", "passkeys_enabled")):
        if getattr(row, col) != getattr(body, k):
            setattr(row, col, getattr(body, k))
            changed.append(k)
    return await _saved(db, request, user, row, "two_factor", changed)


class SessionsIn(BaseModel):
    closed_h: int = Field(ge=1, le=24 * 90)
    max_h: int = Field(ge=0, le=24 * 365)
    idle_min: int = Field(ge=0, le=24 * 60)


@router.put("/api/platform/settings/sessions")
async def put_sessions(body: SessionsIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    row = await platform_settings.get_row(db)
    changed = []
    for k, col in (("closed_h", "session_closed_h"), ("max_h", "session_max_h"), ("idle_min", "idle_timeout_min")):
        if getattr(row, col) != getattr(body, k):
            setattr(row, col, getattr(body, k))
            changed.append(k)
    return await _saved(db, request, user, row, "sessions", changed)


class PasswordsIn(BaseModel):
    min_length: int = Field(ge=8, le=128)
    upper: bool
    lower: bool
    number: bool
    symbol: bool
    expiry_days: int = Field(ge=0, le=3650)


@router.put("/api/platform/settings/passwords")
async def put_passwords(body: PasswordsIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    row = await platform_settings.get_row(db)
    changed = []
    for k, col in (("min_length", "pw_min_length"), ("upper", "pw_upper"), ("lower", "pw_lower"),
                   ("number", "pw_number"), ("symbol", "pw_symbol"), ("expiry_days", "pw_expiry_days")):
        if getattr(row, col) != getattr(body, k):
            setattr(row, col, getattr(body, k))
            changed.append(k)
    return await _saved(db, request, user, row, "passwords", changed)


class EmailIn(BaseModel):
    enabled: bool
    host: str = Field(default="", max_length=200)
    port: int = Field(default=587, ge=1, le=65535)
    tls: str = Field(default="starttls", pattern="^(starttls|ssl|none)$")
    user: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=500)       # None = keep, "" = remove
    sender: str = Field(default="", max_length=320)
    portal_url: str = Field(default="https://alarmportal.twgsecurity.net", max_length=300)


@router.put("/api/platform/settings/email")
async def put_email(body: EmailIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    from email.utils import parseaddr
    url = body.portal_url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", url):
        raise HTTPException(400, "The portal address is like https://alarmportal.twgsecurity.net (no path).")
    sender = body.sender.strip()
    if sender and "@" not in parseaddr(sender)[1]:
        raise HTTPException(400, "The From address needs an email, e.g. TWG Alarm Portal <alerts@twgsecurity.com>.")
    if body.enabled and not (body.host.strip() and sender):
        raise HTTPException(400, "Enter the SMTP server and the From address before switching email on.")
    row = await platform_settings.get_row(db)
    changed = []
    for k, col, v in (("enabled", "smtp_enabled", body.enabled), ("host", "smtp_host", body.host.strip()),
                      ("port", "smtp_port", body.port), ("tls", "smtp_tls", body.tls), ("user", "smtp_user", body.user.strip()),
                      ("sender", "smtp_from", sender), ("portal_url", "portal_url", url)):
        if getattr(row, col) != v:
            setattr(row, col, v)
            changed.append(k)
    if body.password is not None:
        row.smtp_password_enc = encrypt(body.password) if body.password else ""
        changed.append("password")
    if {"host", "port", "tls", "user", "password"} & set(changed):
        row.smtp_last_ok_at, row.smtp_last_error, row.smtp_last_error_at = None, "", None
    return await _saved(db, request, user, row, "email", changed)


class TestEmailIn(BaseModel):
    to: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")


@router.post("/api/platform/email/test")
async def test_email(body: TestEmailIn, request: Request, user: User = Depends(MANAGE)):
    from app import mail
    if await mail.config() is None:
        return {"outcome": "disabled", "error": "Email is switched off or not set up yet."}
    subject, html, text = mail.test_message(user.label)
    outcome, error = await mail.send_now(body.to.strip(), subject, html, text, purpose="test", retry=False)
    return {"outcome": outcome, "error": error}


@router.get("/api/platform/email-log")
async def email_log(limit: int = 100, user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    from app.models import EmailLog
    rows = (await db.scalars(select(EmailLog).order_by(EmailLog.ts.desc(), EmailLog.id.desc()).limit(max(1, min(limit, 500))))).all()
    names = {t.id: t.label for t in (await db.scalars(select(Tenant))).all()}
    return [{"ts": _iso(e.ts), "to": e.to, "subject": e.subject, "purpose": e.purpose, "outcome": e.outcome,
             "error": e.error, "attempts": e.attempts, "company": names.get(e.tenant_id, "")} for e in rows]


class GoogleIn(BaseModel):
    enabled: bool
    client_id: str = Field(default="", max_length=300)
    client_secret: str | None = Field(default=None, max_length=300)    # None = keep, "" = remove
    domains: str = Field(default="", max_length=500)


@router.put("/api/platform/settings/google")
async def put_google(body: GoogleIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    client_id = body.client_id.strip()
    if client_id and not client_id.endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "A Google client ID ends in .apps.googleusercontent.com.")
    domains = []
    for d in body.domains.replace(" ", ",").split(","):
        d = d.strip().lower().lstrip("@")
        if d and not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", d):
            raise HTTPException(400, f"'{d}' isn't a domain like twgsecurity.com.")
        if d:
            domains.append(d)
    row = await platform_settings.get_row(db)
    has_secret = bool(body.client_secret) if body.client_secret is not None else bool(row.google_client_secret_enc)
    if body.enabled and not (client_id and has_secret):
        raise HTTPException(400, "Enter the client ID and client secret before switching Google sign-in on.")
    changed = []
    for k, col, v in (("enabled", "google_enabled", body.enabled), ("client_id", "google_client_id", client_id),
                      ("domains", "google_domains", ", ".join(domains))):
        if getattr(row, col) != v:
            setattr(row, col, v)
            changed.append(k)
    if body.client_secret is not None:
        row.google_client_secret_enc = encrypt(body.client_secret.strip()) if body.client_secret.strip() else ""
        changed.append("client_secret")
    return await _saved(db, request, user, row, "google", changed)


@router.post("/api/platform/cloudflare/test")
async def test_cloudflare(user: User = Depends(MANAGE)):
    await platform_settings.refresh()
    return await cloudflare_edge.test_connection()


# ---------------------------------------------------------------- security overview
def _ban_dict(b: IpBan) -> dict:
    return {"id": b.id, "ip": b.ip, "reason": b.reason, "fail_count": b.fail_count, "ban_count": b.ban_count,
            "permanent": b.permanent, "manual": b.manual, "expires_at": None if b.permanent else _iso(b.expires_at),
            "at_cloudflare": bool(b.cf_rule_id), "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at)}


@router.get("/api/platform/security")
async def security_overview(request: Request, user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    await platform_settings.fresh()
    ip, how = net.resolve(request)
    now = datetime.now(timezone.utc)
    bans = (await db.scalars(select(IpBan).where(IpBan.expires_at > now).order_by(IpBan.updated_at.desc()))).all()
    stale = (await db.scalars(select(IpBan).where(IpBan.expires_at <= now, IpBan.cf_rule_id != ""))).all()
    allow = (await db.scalars(select(IpAllow).order_by(IpAllow.created_at.desc()))).all()
    env_allow = [{"id": None, "ip": str(n), "label": label, "from_env": True}
                 for n, label in platform_settings.current().allow if label == "from IP_ALLOWLIST"]
    untrusted = [{"peer": p, "count": v["count"], "last_at": datetime.fromtimestamp(v["last"], timezone.utc).isoformat()}
                 for p, v in net.untrusted_cf_peers.items() if not net.is_trusted_proxy(p)]
    return {
        "my_ip": ip, "my_ip_via": how, "my_ip_protection": guard.protection(ip),
        "peer": request.client.host if request.client else "",
        "bans": [_ban_dict(b) for b in bans],
        "edge_cleanup_pending": len(stale),
        "allowlist": env_allow + [{"id": a.id, "ip": a.ip, "label": a.label, "from_env": False,
                                   "created_at": _iso(a.created_at)} for a in allow],
        "locked": await guard.locked_accounts(db),
        "untrusted_connectors": untrusted,
        "edge": {"push": cloudflare_edge.can_push(), "configured": cloudflare_edge.can_remove()},
    }


@router.get("/api/platform/auth-events")
async def auth_events(ip: str = "", email: str = "", outcome: str = "", limit: int = 200,
                      user: User = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    q = select(AuthEvent).order_by(AuthEvent.ts.desc(), AuthEvent.id.desc()).limit(max(1, min(limit, 1000)))
    if ip.strip():
        q = q.where(AuthEvent.ip == ip.strip())
    if email.strip():
        q = q.where(AuthEvent.email.contains(email.strip().lower()))
    if outcome.strip():
        q = q.where(AuthEvent.outcome == outcome.strip())
    rows = (await db.scalars(q)).all()
    names = {t.id: t.label for t in (await db.scalars(select(Tenant))).all()}
    return [{"id": e.id, "ts": _iso(e.ts), "ip": e.ip, "email": e.email, "kind": e.kind, "outcome": e.outcome,
             "reason": e.reason, "company": names.get(e.tenant_id, "")} for e in rows]


# ---------------------------------------------------------------- bans, allowlist, locks
class BanIn(BaseModel):
    ip: str = Field(min_length=3, max_length=64)
    reason: str = Field(default="", max_length=200)
    minutes: int = Field(default=0, ge=0, le=525600)      # 0 = permanent


@router.post("/api/platform/bans")
async def add_ban(body: BanIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    ip = net.parse_ip(body.ip)
    if ip is None:
        raise HTTPException(400, "Enter a single IP address (no ranges).")
    ip = str(ip)
    if ip == client_ip(request):
        raise HTTPException(400, "That's your own address. Banning it would lock you out, at Cloudflare's edge too.")
    why = guard.protection(ip, sessions=False)
    if why:
        raise HTTPException(400, f"{ip} can't be banned: {why}.")
    row = await guard.ban(db, ip, reason=body.reason, permanent=body.minutes == 0, minutes=body.minutes)
    audit(db, user.tenant_id, "security.ban", user_id=user.id, ip=client_ip(request), target=ip,
          minutes=body.minutes or None, reason=body.reason.strip())
    await db.commit()
    return _ban_dict(row)


@router.delete("/api/platform/bans/{ban_id}")
async def remove_ban(ban_id: int, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    row = await db.get(IpBan, ban_id)
    if row is None:
        raise HTTPException(404, "No such ban")
    await guard.unban(db, row.ip, by=user.email)
    audit(db, user.tenant_id, "security.unban", user_id=user.id, ip=client_ip(request), target=row.ip)
    await db.commit()
    return {"unbanned": row.ip}


class AllowIn(BaseModel):
    ip: str = Field(min_length=3, max_length=64)
    label: str = Field(default="", max_length=100)


@router.post("/api/platform/allowlist")
async def add_allow(body: AllowIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    try:
        [n] = platform_settings.parse_networks(body.ip.strip())
    except ValueError as e:
        raise HTTPException(400, str(e) if "not an IP" in str(e) else "Enter one IP address or range.")
    if n.num_addresses > 65536:
        raise HTTPException(400, f"{n} is too wide to allowlist.")
    entry = await guard.allow(db, body.ip.strip(), body.label, user_id=user.id, by=user.email)
    audit(db, user.tenant_id, "security.allowlist_add", user_id=user.id, ip=client_ip(request), target=entry.ip,
          label=entry.label)
    await db.commit()
    await platform_settings.refresh()
    return {"id": entry.id, "ip": entry.ip, "label": entry.label}


@router.delete("/api/platform/allowlist/{entry_id}")
async def remove_allow(entry_id: int, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    entry = await db.get(IpAllow, entry_id)
    if entry is None:
        raise HTTPException(404, "No such entry")
    await db.delete(entry)
    audit(db, user.tenant_id, "security.allowlist_remove", user_id=user.id, ip=client_ip(request), target=entry.ip)
    await db.commit()
    await platform_settings.refresh()
    return {"removed": entry.ip}


class LockIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@router.post("/api/platform/locks/clear")
async def clear_lock(body: LockIn, request: Request, user: User = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    await guard.clear_lock(db, email, by=user.email)
    audit(db, user.tenant_id, "security.lock_cleared", user_id=user.id, ip=client_ip(request), target=email)
    await db.commit()
    return {"cleared": email}

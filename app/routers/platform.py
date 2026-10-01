"""Platform settings and sign-in security (TWG only). Viewing needs platform.view, changing platform.manage.

- GET  /platform                          the page
- GET  /api/platform/settings             all settings (secrets only as "set"/"not set")
- PUT  /api/platform/settings/sign-in     ban and account-lock rules
- PUT  /api/platform/settings/proxies     trusted tunnel connectors (private addresses only)
- PUT  /api/platform/settings/cloudflare  Cloudflare edge bans: token, zone, on/off
- POST /api/platform/cloudflare/test      read-only check of the saved token and zone
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

"""API tokens for the portal's MCP server (app/mcp_server.py).

A token is "twgp_" + 43 random characters, shown once; only its SHA-256 is stored. It acts as the person who made
it (their company, sites, permissions), so the AI can never do more than they could in the portal. It stops working
when it's revoked, it expires, the person is disabled or deleted, their company's sign-in is switched off, or their
sessions are ended everywhere (the user's token_version moved on).
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, Tenant, User

PREFIX = "twgp_"
MAX_PER_USER = 10
USED_EVERY_S = 60                 # last_used_at is written at most once a minute


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def token_dict(t: ApiToken) -> dict:
    iso = lambda d: _aware(d).isoformat() if d else None  # noqa: E731
    return {"id": t.id, "name": t.name, "hint": t.hint, "created_at": iso(t.created_at), "expires_at": iso(t.expires_at),
            "last_used_at": iso(t.last_used_at), "last_used_ip": t.last_used_ip}


async def active(db: AsyncSession, user_id: int) -> list[ApiToken]:
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
                             .order_by(ApiToken.id))).all()
    return [t for t in rows if t.expires_at is None or _aware(t.expires_at) > now]


async def create(db: AsyncSession, user: User, name: str, days: int | None) -> tuple[ApiToken, str]:
    """A new token for this person (the caller commits). Returns (row, the token: shown once)."""
    if len(await active(db, user.id)) >= MAX_PER_USER:
        raise ValueError(f"You already have {MAX_PER_USER} tokens. Revoke one you no longer use first.")
    token = PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = ApiToken(user_id=user.id, tenant_id=user.tenant_id, name=name.strip()[:100] or "MCP", token_hash=_hash(token),
                   hint=f"{PREFIX}…{token[-4:]}", token_version=user.token_version or 0, created_at=now,
                   expires_at=now + timedelta(days=days) if days else None)
    db.add(row)
    await db.flush()
    return row, token


async def revoke(row: ApiToken, reason: str) -> None:
    if row.revoked_at is None:
        row.revoked_at, row.revoked_reason = datetime.now(timezone.utc), reason[:100]


async def verify(db: AsyncSession, token: str, ip: str) -> tuple[ApiToken, User, Tenant] | None:
    """The token's row, person and company if it may be used now, else None. Notes when it was last used."""
    if not token.startswith(PREFIX) or len(token) > 100:
        return None
    row = await db.scalar(select(ApiToken).where(ApiToken.token_hash == _hash(token)))
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if row.expires_at is not None and _aware(row.expires_at) <= now:
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active or user.deleted_at is not None or (user.token_version or 0) != row.token_version:
        return None
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    if row.last_used_at is None or (now - _aware(row.last_used_at)).total_seconds() >= USED_EVERY_S or row.last_used_ip != ip:
        row.last_used_at, row.last_used_ip = now, ip[:64]
        await db.commit()
    return row, user, tenant

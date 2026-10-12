"""One-time links: "setup" (an invite, 7 days) and "reset" (forgot password, 30 minutes).

The link carries 32 random bytes; only their SHA-256 is stored. Minting a new token of a purpose
retires the user's older unused ones of that purpose. A token is consumed only when the form is
submitted successfully; opening the link just checks it.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings
from app.models import User, UserToken, utcnow

TTL = {"setup": timedelta(days=7), "reset": timedelta(minutes=30)}
PATH = {"setup": "/setup", "reset": "/reset"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link(purpose: str, token: str) -> str:
    return f"{platform_settings.current().portal_url}{PATH[purpose]}?token={token}"


async def mint(db: AsyncSession, user: User, purpose: str, *, by: int | None = None) -> tuple[str, UserToken]:
    now = utcnow()
    await db.execute(update(UserToken).where(UserToken.user_id == user.id, UserToken.purpose == purpose,
                                             UserToken.used_at.is_(None)).values(used_at=now))
    token = secrets.token_urlsafe(32)
    row = UserToken(user_id=user.id, purpose=purpose, token_hash=_hash(token), created_at=now,
                    expires_at=now + TTL[purpose], created_by_id=by)
    db.add(row)
    await db.flush()
    return token, row


async def look_up(db: AsyncSession, purpose: str, token: str) -> tuple[str, UserToken | None, User | None]:
    """("ok" | "invalid" | "used" | "expired", row, user). Doesn't consume anything."""
    if not token or len(token) > 100:
        return "invalid", None, None
    row = await db.scalar(select(UserToken).where(UserToken.token_hash == _hash(token), UserToken.purpose == purpose))
    if row is None:
        return "invalid", None, None
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return "invalid", row, None
    if row.used_at is not None:
        return "used", row, user
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=utcnow().tzinfo)
    if exp < utcnow():
        return "expired", row, user
    return "ok", row, user


async def consume(db: AsyncSession, row: UserToken) -> None:
    """Use this token and retire every other open one of the same purpose."""
    now = utcnow()
    await db.execute(update(UserToken).where(UserToken.user_id == row.user_id, UserToken.purpose == row.purpose,
                                             UserToken.used_at.is_(None)).values(used_at=now))
    row.used_at = now

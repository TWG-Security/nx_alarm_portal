"""Location-sharing links for guards without a GPS router: /g/<token> (app/routers/share.py).

The guard opens the link on their phone, taps Start, and the page sends the browser's location to the portal,
where it moves the guard's marker exactly like a Peplink router's GPS does (trackers.phone_fix). One link per
tracker, valid SHARE_DAYS; texted with every dispatch. The token is 18 random bytes; its SHA-256 finds the
tracker, and the token is kept encrypted so the same link can be texted again (a new link replaces the old).
"""
import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings
from app.models import Tracker, User, utcnow
from app.security import decrypt, encrypt

PATH = "/g/"
SHARE_DAYS = 30


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def url_for(token: str) -> str:
    return f"{platform_settings.current().portal_url}{PATH}{token}/"       # "/" last: see alarm_links.url


def share_url(t: Tracker, renew: bool = False) -> str:
    """The tracker's link, made (or replaced, renew=True) if needed. The caller commits."""
    token = ""
    if t.share_enc and not renew and t.share_expires_at and _aware(t.share_expires_at) > utcnow() + timedelta(days=1):
        try:
            token = decrypt(t.share_enc)
        except Exception:  # noqa: BLE001 - a key change: make a new one
            token = ""
    if not token:
        token = secrets.token_urlsafe(18)
        t.share_hash, t.share_enc = _hash(token), encrypt(token)
        t.share_expires_at = utcnow() + timedelta(days=SHARE_DAYS)
    return url_for(token)


async def look_up(db: AsyncSession, token: str) -> tuple[str, Tracker | None]:
    """("ok" | "invalid" | "expired", tracker). Invalid also covers removed / switched-off trackers and people
    who were disabled or deleted."""
    if not token or len(token) > 64:
        return "invalid", None
    t = await db.scalar(select(Tracker).where(Tracker.share_hash == _hash(token)))
    if t is None or not t.enabled or t.source != "phone":
        return "invalid", None
    if t.user_id is not None:
        u = await db.get(User, t.user_id)
        if u is None or not u.is_active or u.deleted_at is not None:
            return "invalid", None
    if t.share_expires_at is None or _aware(t.share_expires_at) <= utcnow():
        return "expired", t
    return "ok", t

"""No-sign-in links to one alarm, sent by text message (app/sms.py): /a/<token>.

The token is 18 random bytes (144 bits, 24 characters, so the text stays short); only its SHA-256 is
stored. A link shows that one alarm (details, snapshot, clip, live video of its camera) and nothing else,
until it expires (Platform page, default 24 h). It stops working at once if the person it was sent to is
disabled or removed, or the site is archived. Unknown tokens count as failed sign-ins, so guessing gets
the address banned like any other.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings
from app.models import Alarm, AlarmLink, User, utcnow

PATH = "/a/"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def url(token: str) -> str:
    # Ends in "/": iPhones have been seen to leave a link's last character out of the tappable part
    # (2026-10-13, MMS alarm text); with the slash last, the link still works if that happens.
    return f"{platform_settings.current().portal_url}{PATH}{token}/"


def picture_url(token: str) -> str:
    """The alarm picture Twilio fetches for the MMS (routers/links.py)."""
    return f"{platform_settings.current().portal_url}/media/a/{token}/alarm.jpg"


def gif_url(token: str) -> str:
    """The moving alarm picture for the Confirmed / Dispatch MMS (routers/links.py, services/alarm_gif.py)."""
    return f"{platform_settings.current().portal_url}/media/a/{token}/alarm.gif"


async def mint(db: AsyncSession, alarm: Alarm, user: User | None, hours: int) -> str:
    token = secrets.token_urlsafe(18)
    now = utcnow()
    db.add(AlarmLink(token_hash=_hash(token), alarm_id=alarm.id, tenant_id=alarm.tenant_id,
                     user_id=user.id if user else None, created_at=now, expires_at=now + timedelta(hours=hours)))
    await db.flush()
    return token


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


async def look_up(db: AsyncSession, token: str) -> tuple[str, AlarmLink | None, Alarm | None]:
    """("ok" | "invalid" | "expired", link, alarm). "invalid" also covers revoked links, disabled people
    and archived sites: nothing about the alarm is revealed."""
    if not token or len(token) > 64:
        return "invalid", None, None
    link = await db.scalar(select(AlarmLink).where(AlarmLink.token_hash == _hash(token)))
    if link is None or link.revoked_at is not None:
        return "invalid", link, None
    if link.user_id is not None:
        user = await db.get(User, link.user_id)
        if user is None or not user.is_active:
            return "invalid", link, None
    alarm = await db.get(Alarm, link.alarm_id)
    if alarm is None or alarm.site is None or alarm.site.archived_at is not None:
        return "invalid", link, None
    if _aware(link.expires_at) < utcnow():
        return "expired", link, alarm
    return "ok", link, alarm

"""Server-side sessions: every signed-in browser is a user_sessions row, and the cookie carries its id.

- start(): a new row and fresh cookie contents (uid, tid, sid, tv, csrf).
- check() (deps.current_user, every request): the row exists and isn't revoked, the token version
  matches (bumped by "sign out everywhere"), and the browser wasn't closed longer than the limit.
  last_seen_at is written at most once a minute, and the cookie is re-signed then too: its signature carries
  its own age, so a cookie never renewed would expire counted from sign-in, open page or not.
- Monitoring stations (User.monitoring_station) skip the max-age limit (and the page skips idle sign-out).
- The iPhone app (app/routers/mobile.py) signs in to a row too, method "mobile": its id is the SHA-256 of the
  device token ("twgm_…", sent as Bearer), so the Account page lists the phone, and revoking, sign out everywhere
  and the stream's SIGNED OUT work as for a browser. A phone is signed out after MOBILE_IDLE_DAYS unused (or the
  closed-browser limit if that's longer), not after the browser's 12 h. A mobile row never works as a cookie.
- Sessions from before this existed (no sid in the cookie) are adopted, so a deploy never signs out
  the monitoring screens.
- Revoking marks the row and an in-memory registry; the browser's live stream (SSE) checks it on
  every 5 s heartbeat and tells the page, which goes loudly "SIGNED OUT" (common.js), never quietly.
"""

import secrets
from datetime import timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app import platform_settings
from app.models import User, UserSession, utcnow
from app.net import client_ip
from app.security import new_csrf_token

SEEN_EVERY_S = 60
MOBILE_PREFIX = "twgm_"
MOBILE_IDLE_DAYS = 30                # a phone that hasn't used the app for this long signs in again
KEEP_DAYS = 30                       # revoked/expired rows are pruned after this

_revoked: set[str] = set()           # sids revoked in this process (checked by the live stream)
_min_tv: dict[int, int] = {}         # user id -> lowest token version still valid


def aware(dt):
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def stream_revoked(uid: int | None, sid: str | None, tv: int | None) -> bool:
    """Cheap check for the live stream: was this browser signed out since it connected?"""
    return bool(sid and sid in _revoked) or (uid is not None and (tv or 0) < _min_tv.get(uid, 0))


def _note_revoked(sids, tenant_id: int | None = None) -> None:
    _revoked.update(sids)
    if len(_revoked) > 20000:
        _revoked.clear()             # memory bound; the database check on the next request still applies
    if tenant_id is not None:
        # Wake that company's open live streams so a revoked one hears about it now, not at the next heartbeat.
        from app.services.bus import bus
        bus.publish(tenant_id, "session.revoked", {})


async def start(request: Request, db: AsyncSession, user: User, method: str = "password") -> UserSession:
    """Sign this browser in (the caller commits)."""
    now = utcnow()
    await db.execute(delete(UserSession).where(UserSession.user_id == user.id, or_(
        UserSession.revoked_at < now - timedelta(days=KEEP_DAYS), UserSession.last_seen_at < now - timedelta(days=KEEP_DAYS))))
    row = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, tenant_id=user.tenant_id, method=method,
                      ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
                      created_at=now, last_seen_at=now)
    db.add(row)
    request.session.clear()
    request.session.update({"uid": user.id, "tid": user.tenant_id, "sid": row.id, "tv": user.token_version or 0,
                            "csrf": new_csrf_token()})
    user.last_login_at = now
    await db.flush()
    return row


def mobile_id(token: str) -> str:
    """The user_sessions id of a device token: its SHA-256 (the token itself is never stored)."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def expired_reason(row: UserSession, now=None, station: bool = False) -> str:
    snap = platform_settings.current()
    now = now or utcnow()
    idle_h = snap.session_closed_h
    if row.method == "mobile" and idle_h > 0:
        idle_h = max(idle_h, MOBILE_IDLE_DAYS * 24)
    if idle_h > 0 and now - aware(row.last_seen_at) > timedelta(hours=idle_h):
        return "inactive"
    if snap.session_max_h > 0 and not station and now - aware(row.created_at) > timedelta(hours=snap.session_max_h):
        return "max_age"
    return ""


async def check(request: Request, db: AsyncSession, user: User) -> str:
    """"" if this browser's session is valid, else why not. Adopts sessions without a sid."""
    sess = request.session
    now = utcnow()
    sid = sess.get("sid")
    if not sid:                                          # signed in before server-side sessions existed
        row = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, tenant_id=user.tenant_id, method="legacy",
                          ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
                          created_at=now, last_seen_at=now)
        db.add(row)
        await db.commit()
        sess["sid"], sess["tv"] = row.id, user.token_version or 0
        return ""
    if (sess.get("tv") or 0) != (user.token_version or 0):
        return "signed out everywhere"
    row = await db.get(UserSession, sid)
    if row is None or row.user_id != user.id or row.method == "mobile":
        return "unknown session"
    if row.revoked_at is not None:
        return row.revoked_reason or "revoked"
    why = expired_reason(row, now, bool(user.monitoring_station))
    if why:
        row.revoked_at, row.revoked_reason = now, why
        await db.commit()
        return why
    if (now - aware(row.last_seen_at)).total_seconds() >= SEEN_EVERY_S:
        row.last_seen_at, row.ip = now, client_ip(request)
        sess["seen"] = int(now.timestamp())             # changed -> the cookie goes out again, freshly signed
        await db.commit()
    return ""


async def start_mobile(request: Request, db: AsyncSession, user: User, device_name: str) -> tuple[UserSession, str]:
    """Sign the iPhone app in (the caller commits). Returns (row, device token: given to the app once)."""
    now = utcnow()
    token = MOBILE_PREFIX + secrets.token_urlsafe(32)
    row = UserSession(id=mobile_id(token), user_id=user.id, tenant_id=user.tenant_id, method="mobile",
                      ip=client_ip(request), user_agent=(device_name.strip() or "iPhone app")[:300],
                      created_at=now, last_seen_at=now)
    db.add(row)
    user.last_login_at = now
    await db.flush()
    return row, token


async def check_mobile(request: Request, db: AsyncSession, token: str):
    """(user, tenant, row) for a valid device token, else None. Notes when it was last used (once a minute)."""
    from app.models import Tenant
    if not token.startswith(MOBILE_PREFIX) or len(token) > 100:
        return None
    row = await db.get(UserSession, mobile_id(token))
    if row is None or row.method != "mobile" or row.revoked_at is not None:
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        return None
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    now = utcnow()
    why = expired_reason(row, now, bool(user.monitoring_station))
    if why:
        row.revoked_at, row.revoked_reason = now, why
        from app import apns
        await apns.drop_for_sessions(db, [row.id])
        await db.commit()
        _note_revoked([row.id], row.tenant_id)
        return None
    ip = client_ip(request)
    if (now - aware(row.last_seen_at)).total_seconds() >= SEEN_EVERY_S or row.ip != ip:
        row.last_seen_at, row.ip = now, ip[:64]
        await db.commit()
    return user, tenant, row


async def revoke(db: AsyncSession, row: UserSession, reason: str) -> None:
    if row.revoked_at is None:
        row.revoked_at, row.revoked_reason = utcnow(), reason[:100]
    from app import apns
    await apns.drop_for_sessions(db, [row.id])       # an iPhone signed out stops getting notifications
    _note_revoked([row.id], row.tenant_id)


async def revoke_all(db: AsyncSession, user: User, reason: str, *, keep_sid: str | None = None) -> int:
    """Revoke the user's sessions (except keep_sid). Without keep_sid the token version is bumped too,
    which also ends any session the table doesn't know about."""
    q = select(UserSession.id).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    if keep_sid:
        q = q.where(UserSession.id != keep_sid)
    sids = list((await db.scalars(q)).all())
    if sids:
        await db.execute(update(UserSession).where(UserSession.id.in_(sids))
                         .values(revoked_at=utcnow(), revoked_reason=reason[:100]))
        from app import apns
        await apns.drop_for_sessions(db, sids)
    if keep_sid is None:
        user.token_version = (user.token_version or 0) + 1
        _min_tv[user.id] = user.token_version
    _note_revoked(sids, user.tenant_id)
    return len(sids)


def session_dict(row: UserSession, current_sid: str | None = None) -> dict:
    return {"id": row.id, "current": row.id == current_sid, "method": row.method, "ip": row.ip,
            "user_agent": row.user_agent, "created_at": aware(row.created_at).isoformat(),
            "last_seen_at": aware(row.last_seen_at).isoformat()}


async def active(db: AsyncSession, user_ids) -> list[UserSession]:
    rows = (await db.scalars(select(UserSession).where(UserSession.user_id.in_(list(user_ids)),
                                                       UserSession.revoked_at.is_(None))
                             .order_by(UserSession.last_seen_at.desc()))).all()
    return [r for r in rows if not expired_reason(r)]

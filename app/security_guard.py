"""Sign-in protection: IP bans with escalation, per-account locks, an allowlist, Cloudflare edge sync.

Ported from MCP-Control-Platform apps/api/src/security/intrusion.ts.
- Every sign-in attempt is an auth_events row. maxFails failures from one IP inside the window ban it
  (first ban 15 min, each later one x4, capped, then permanent). A banned IP gets a "blocked" page on
  the sign-in pages (app.main BanGate); signed-in sessions are never cut off by a ban.
- Account lock: too many failures for one email from any IPs inside the window refuse that email
  (generic "invalid" answer) until the window passes. Fails open on database errors.
- Never banned (alarm safety): internal addresses (the LAN, the tunnel connector when untrusted),
  trusted proxies, the allowlist, and any IP a signed-in user was active from in the last 15 minutes
  (a colleague mistyping on the same office connection must not block the monitoring screens,
  locally or at Cloudflare). Failures from those still count toward the account lock.
- An admin "unban" or "clear lock" writes a `cleared` auth event; only failures after it count.
- Cloudflare pushes run in the background after the local ban is stored; the sweeper retries them
  and removes edge rules of expired bans.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cloudflare_edge, platform_settings
from app.db import sessionmaker
from app.models import AuthEvent, IpAllow, IpBan
from app.net import in_networks, is_internal, parse_ip

log = logging.getLogger(__name__)

PERMANENT_UNTIL = datetime(9999, 1, 1, tzinfo=timezone.utc)
SESSION_IP_TTL_S = 15 * 60
EVENT_RETENTION_DAYS = 90
KINDS = ("login", "totp", "recovery", "passkey", "reset", "setup", "sso", "admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops the offset; values are always stored as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ---------------------------------------------------------------- who may never be banned
_session_ips: dict[str, float] = {}


def note_session_ip(ip: str) -> None:
    """Called for every signed-in request (deps.current_user)."""
    now = time.monotonic()
    _session_ips[ip] = now
    if len(_session_ips) > 2000:
        for k in [k for k, t in _session_ips.items() if now - t > SESSION_IP_TTL_S]:
            del _session_ips[k]


def has_live_session(ip: str) -> bool:
    t = _session_ips.get(ip)
    return t is not None and time.monotonic() - t <= SESSION_IP_TTL_S


def allow_label(ip: str) -> str | None:
    for net, label in platform_settings.current().allow:
        if in_networks(ip, (net,)):
            return label or str(net)
    return None


def protection(ip: str, *, sessions: bool = True) -> str:
    """Why this IP can't be banned ("" = it can)."""
    p = parse_ip(ip)
    if p is None:
        return "unreadable address"
    if is_internal(p):
        return "internal network address (LAN, VPN or the tunnel connector)"
    if in_networks(p, platform_settings.current().trusted):
        return "trusted proxy"
    label = allow_label(ip)
    if label is not None:
        return f"allowlisted ({label})"
    if sessions and has_live_session(str(p)):
        return "a signed-in user is active from this address"
    return ""


# ---------------------------------------------------------------- bans: queries
async def active_ban(ip: str) -> IpBan | None:
    """The IP's ban if it's in force. Own session; fails open (None) on any error."""
    if not ip or is_internal(ip):
        return None
    try:
        async with sessionmaker()() as db:
            return await db.scalar(select(IpBan).where(IpBan.ip == ip, IpBan.expires_at > utcnow()))
    except Exception as e:  # noqa: BLE001 - never block sign-in on a database problem
        log.warning("ban check for %s failed (%s); letting the request through", ip, e)
        return None


async def _failures(db: AsyncSession, col, value: str, window_min: int) -> int:
    """Failures for this IP/email in the window, counting only those after an admin's last clear."""
    since = utcnow() - timedelta(minutes=window_min)
    cleared = aware(await db.scalar(select(func.max(AuthEvent.ts)).where(col == value, AuthEvent.outcome == "cleared")))
    if cleared and cleared > since:
        since = cleared
    return await db.scalar(select(func.count()).select_from(AuthEvent)
                           .where(col == value, AuthEvent.outcome == "failure", AuthEvent.ts >= since)) or 0


async def is_account_locked(db: AsyncSession, email: str) -> bool:
    rules = platform_settings.current().rules
    if not email or rules.account_lock_max <= 0:
        return False
    try:
        return await _failures(db, AuthEvent.email, email, rules.window_min) >= rules.account_lock_max
    except Exception as e:  # noqa: BLE001 - fail open: never lock everyone out on an infra error
        log.warning("account lock check failed (%s); allowing", e)
        await db.rollback()
        return False


def _human(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def ban_minutes(rules, ban_count: int) -> int:
    return min(rules.first_min * 4 ** (ban_count - 1), rules.max_min)


# ---------------------------------------------------------------- recording attempts
async def record(db: AsyncSession, ip: str, kind: str, outcome: str, *, email: str = "", reason: str = "",
                 user=None) -> IpBan | None:
    """Store a sign-in attempt in the caller's session (the caller commits). On a failure, ban the IP if
    it crossed the threshold; returns the new ban. Never raises into the sign-in path."""
    try:
        db.add(AuthEvent(ip=ip, email=(email or "").strip().lower()[:320], kind=kind, outcome=outcome,
                         reason=reason[:300], tenant_id=getattr(user, "tenant_id", None),
                         user_id=getattr(user, "id", None)))
        await db.flush()
        level = log.warning if outcome == "failure" else log.info
        level("auth %s %s ip=%s email=%s %s", kind, outcome, ip, email, reason)
        if outcome == "failure":
            return await _maybe_ban(db, ip, kind)
    except Exception as e:  # noqa: BLE001
        log.error("recording a %s %s for %s failed: %s", kind, outcome, ip, e)
        await db.rollback()
    return None


async def _maybe_ban(db: AsyncSession, ip: str, kind: str) -> IpBan | None:
    if protection(ip):
        return None
    rules = platform_settings.current().rules
    fails = await _failures(db, AuthEvent.ip, ip, rules.window_min)
    if fails < rules.max_fails:
        return None
    now = utcnow()
    ban = await db.scalar(select(IpBan).where(IpBan.ip == ip))
    if ban is not None and ban.permanent and aware(ban.expires_at) > now:
        return None
    # Coming back after a served ban is a new episode (x4 longer); more failures during a ban only slide it.
    new_episode = ban is None or aware(ban.expires_at) <= now
    count = (ban.ban_count if ban else 0) + (1 if new_episode else 0)
    count = max(count, 1)
    permanent = rules.permanent_after > 0 and count >= rules.permanent_after
    minutes = ban_minutes(rules, count)
    reason = f"{fails} failed {kind} attempts in {rules.window_min} min"
    if permanent:
        reason += f" (ban #{count}: permanent, repeat offender)"
    elif count > 1:
        reason += f" (ban #{count}: repeat offender, {_human(minutes)})"
    expires = PERMANENT_UNTIL if permanent else now + timedelta(minutes=minutes)
    if ban is None:
        ban = IpBan(ip=ip, cf_rule_id="")
        db.add(ban)
    ban.reason, ban.fail_count, ban.ban_count, ban.permanent, ban.expires_at, ban.manual = (
        reason, fails, count, permanent, expires, False)
    await db.flush()
    log.warning("IP BANNED: %s until %s (%s)", ip, "forever" if permanent else expires.isoformat(), reason)
    after_commit(db, _edge_push, ip)
    return ban


# ---------------------------------------------------------------- admin actions (the caller commits)
async def ban(db: AsyncSession, ip: str, *, reason: str = "", permanent: bool = True, minutes: int = 0) -> IpBan:
    """Manual ban. The caller checks protection(ip, sessions=False) and that it isn't the admin's own IP."""
    now = utcnow()
    row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
    if row is None:
        row = IpBan(ip=ip, ban_count=1, fail_count=0, cf_rule_id="")
        db.add(row)
    row.permanent = permanent or minutes <= 0
    row.expires_at = PERMANENT_UNTIL if row.permanent else now + timedelta(minutes=minutes)
    row.reason = (reason.strip() or ("banned by an admin (permanent)" if row.permanent else "banned by an admin"))[:300]
    row.manual = True
    await db.flush()
    after_commit(db, _edge_push, ip)
    return row


async def unban(db: AsyncSession, ip: str, *, by: str = "") -> bool:
    """Lift a ban and forgive its history: the next ban starts at #1, and older failures stop counting."""
    row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
    was_active = row is not None and aware(row.expires_at) > utcnow()
    if row is not None:
        row.expires_at, row.permanent, row.ban_count = utcnow(), False, 0
    db.add(AuthEvent(ip=ip, kind="admin", outcome="cleared", reason=f"unbanned{' by ' + by if by else ''}"[:300]))
    await db.flush()
    if row is not None and row.cf_rule_id:
        after_commit(db, _edge_remove, ip)
    return was_active


async def clear_lock(db: AsyncSession, email: str, *, by: str = "") -> None:
    db.add(AuthEvent(email=email.strip().lower(), kind="admin", outcome="cleared",
                     reason=f"account lock cleared{' by ' + by if by else ''}"[:300]))
    await db.flush()


async def allow(db: AsyncSession, value: str, label: str = "", *, user_id: int | None = None, by: str = "") -> IpAllow:
    """Allowlist an IP or range; bans inside it are lifted (and removed at Cloudflare)."""
    from app.platform_settings import parse_networks
    [net] = parse_networks(value)
    entry = await db.scalar(select(IpAllow).where(IpAllow.ip == str(net if net.num_addresses > 1 else net.network_address)))
    if entry is None:
        entry = IpAllow(ip=str(net if net.num_addresses > 1 else net.network_address), created_by_id=user_id)
        db.add(entry)
    entry.label = label.strip()[:100]
    for b in (await db.scalars(select(IpBan).where(IpBan.expires_at > utcnow()))).all():
        if in_networks(b.ip, (net,)):
            await unban(db, b.ip, by=by or "allowlist")
    await db.flush()
    return entry


async def locked_accounts(db: AsyncSession) -> list[dict]:
    rules = platform_settings.current().rules
    if rules.account_lock_max <= 0:
        return []
    since = utcnow() - timedelta(minutes=rules.window_min)
    rows = (await db.execute(select(AuthEvent.email, func.count(), func.max(AuthEvent.ts))
                             .where(AuthEvent.outcome == "failure", AuthEvent.ts >= since, AuthEvent.email != "")
                             .group_by(AuthEvent.email)
                             .having(func.count() >= rules.account_lock_max))).all()
    out = []
    for email, _n, last in rows:
        n = await _failures(db, AuthEvent.email, email, rules.window_min)    # honours clears
        if n >= rules.account_lock_max:
            out.append({"email": email, "failures": n, "last_at": aware(last).isoformat(),
                        "until": (aware(last) + timedelta(minutes=rules.window_min)).isoformat()})
    return out


# ---------------------------------------------------------------- Cloudflare (background, best-effort)
_tasks: set[asyncio.Task] = set()


def after_commit(db: AsyncSession, fn, ip: str) -> None:
    """Run fn(ip) in the background once the caller's transaction commits (never before: the edge
    rule id is stored on the ban row, which must exist)."""
    from sqlalchemy import event
    pending = db.info.setdefault("edge_jobs", [])
    pending.append((fn, ip))
    if len(pending) == 1:
        def _go(_session):
            jobs = db.info.pop("edge_jobs", [])
            for f, addr in jobs:
                spawn(f(addr))
        event.listen(db.sync_session, "after_commit", _go, once=True)


def spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:            # no loop (CLI teardown): the sweeper catches up
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for background Cloudflare work (tests, CLI)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _edge_push(ip: str) -> None:
    if not cloudflare_edge.can_push():
        return
    try:
        async with sessionmaker()() as db:
            row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
            if row is None or row.cf_rule_id or aware(row.expires_at) <= utcnow() or protection(ip, sessions=not row.manual):
                return
            reason = row.reason
        rule_id = await cloudflare_edge.push_ban(ip, reason)
        if rule_id:
            async with sessionmaker()() as db:
                row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
                if row is not None:
                    row.cf_rule_id = rule_id
                    await db.commit()
                    log.info("edge ban pushed to Cloudflare: %s (rule %s)", ip, rule_id)
                    if aware(row.expires_at) <= utcnow():          # unbanned meanwhile
                        await _edge_remove(ip)
    except Exception as e:  # noqa: BLE001
        log.warning("edge push for %s failed: %s", ip, e)


async def _edge_remove(ip: str) -> None:
    try:
        async with sessionmaker()() as db:
            row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
            if row is None or not row.cf_rule_id or aware(row.expires_at) > utcnow():
                return
            rule_id = row.cf_rule_id
        if await cloudflare_edge.remove_ban(ip, rule_id):
            async with sessionmaker()() as db:
                row = await db.scalar(select(IpBan).where(IpBan.ip == ip))
                if row is not None and row.cf_rule_id == rule_id:
                    row.cf_rule_id = ""
                    await db.commit()
                    log.info("edge ban removed at Cloudflare: %s", ip)
    except Exception as e:  # noqa: BLE001
        log.warning("edge removal for %s failed: %s", ip, e)


_last_prune = 0.0


async def sweep() -> None:
    """Push bans Cloudflare doesn't have yet, remove edge rules of expired bans, prune old events."""
    global _last_prune
    await platform_settings.fresh()
    now = utcnow()
    async with sessionmaker()() as db:
        expired = (await db.scalars(select(IpBan.ip).where(IpBan.cf_rule_id != "", IpBan.expires_at <= now))).all()
        pending = (await db.scalars(select(IpBan.ip).where(IpBan.cf_rule_id == "", IpBan.expires_at > now))).all()
    if cloudflare_edge.can_remove():
        for ip in expired:
            await _edge_remove(ip)
    if cloudflare_edge.can_push():
        for ip in pending:
            await _edge_push(ip)
    if time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        async with sessionmaker()() as db:
            await db.execute(delete(AuthEvent).where(AuthEvent.ts < now - timedelta(days=EVENT_RETENTION_DAYS)))
            await db.commit()


async def run_sweeper(interval_s: float = 60.0) -> None:
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep sweeping
            log.warning("security sweep failed: %s", e)
        await asyncio.sleep(interval_s)

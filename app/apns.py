"""Push notifications for the iPhone app (docs/IOS_APP_BRIEF.md 6.2), working like the alarm texts in app/sms.py.

The user, 2026-10-19: "working exactly like the alarm texts: same triggers, same per-person choices, same flood
protection, sent in the background so ingest never waits". Texts stay on as the backup: nothing here changes them.

- Who: users.push_level off | critical | alarm | warning (None = not chosen yet = critical + alarm, as a newly added
  number texts), users.push_when raised | confirmed | both, sms_sites for the sites; only people with a registered
  iPhone (push_devices) whose app sign-in is still valid. A device whose sign-in ended is dropped when found.
- When (each where sms.py sends the matching text): a new alarm (notify, from poller._announce; warnings too for
  push_level "warning"); Real event (notify_verdicts, from ack / bulk edit); Real event withdrawn (STAND DOWN); a
  dispatch (dispatch(), from services/dispatch.py, with the same link token as the text); the call ended (call_ended(),
  from services/calls.py: CALL ENDED / STAND DOWN to dispatched guards who haven't tapped Clear); an alarm closed by
  someone (notify_closed, from ack): same apns-collapse-id as its alarm push, so it replaces that notification.
- Flood control: the texts' limit (Platform → Text messages, per 10 minutes) counted separately for pushes; past it,
  one summary push when the window has room. Confirmed / dispatch / call pushes are never held (as texts).
- How: token-based APNs auth (ES256 JWT, kid = APNS_KEY_ID, iss = APNS_TEAM_ID, made again every JWT_EVERY_S), HTTP/2
  to api.push.apple.com or api.sandbox.push.apple.com per device (APNS_API_BASE overrides both: dev and tests). Every
  send is a background task (sms._spawn: the same hard time limits, drain and shutdown); a DB slot of its own.
  APNs 410, or 400 BadDeviceToken, or Unregistered: the device is removed. Each push goes to push_log (the alarm's
  timeline: "Push sent / NOT sent").
"""
import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import sessionmaker
from app.models import Alarm, PlatformSettings, PushDevice, PushLog, User, UserSession, utcnow

log = logging.getLogger("portal.apns")

LEVELS = {"off": 0, "critical": 1, "alarm": 2, "warning": 3}   # the quietest alarm priority that pushes
DEFAULT_LEVEL = "alarm"                                          # push_level not chosen yet
WHEN = ("raised", "confirmed", "both")
APPLE = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
JWT_EVERY_S = 50 * 60            # Apple refuses a provider token older than an hour and more than one new one per 20 min
TIMEOUT_S = 10.0
SEND_SLOTS = 8                   # APNs requests at once (HTTP/2 multiplexes them on one connection)
DB_SLOTS = 2
ALARM_TTL_S = 3600               # APNs keeps an undelivered alarm push this long (a phone that was off)
DEAD = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
WORD = {1: "CRITICAL", 2: "ALARM", 3: "WARNING"}
# The app installs each person's chosen sound per level as Library/Sounds/<file>; iOS plays its default sound if the
# file is missing. Alarm-type pushes use the alarm's level; dispatch is always critical; CLOSED stays silent.
SOUND = {1: "twg-critical.wav", 2: "twg-alarm.wav", 3: "twg-warning.wav"}


def sound_for(priority: int) -> str:
    return SOUND.get(priority, SOUND[3])


def level_of(user: User) -> str:
    return user.push_level or DEFAULT_LEVEL


# ---------------------------------------------------------------- APNs
@dataclass
class _Key:
    key_id: str
    team_id: str
    topic: str
    pem: bytes


def _key() -> _Key | None:
    s = get_settings()
    if not (s.apns_key_path and s.apns_key_id and s.apns_team_id and s.apns_topic):
        return None
    try:
        pem = Path(s.apns_key_path).read_bytes()
    except OSError as e:
        log.error("apns: can't read the APNs key %s: %s", s.apns_key_path, e)
        return None
    return _Key(s.apns_key_id.strip(), s.apns_team_id.strip(), s.apns_topic.strip(), pem)


def configured() -> bool:
    return _key() is not None


_jwt: tuple[str, float, str] | None = None          # (token, made at, key id)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def provider_token(force: bool = False) -> str:
    """The ES256 JWT APNs wants, made again every JWT_EVERY_S (or when APNs says it expired)."""
    global _jwt
    k = _key()
    if k is None:
        raise RuntimeError("APNs isn't configured")
    if not force and _jwt and _jwt[2] == k.key_id and time.time() - _jwt[1] < JWT_EVERY_S:
        return _jwt[0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    now = int(time.time())
    head = _b64(json.dumps({"alg": "ES256", "kid": k.key_id}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iss": k.team_id, "iat": now}, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(k.pem, password=None)
    r, s_ = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s_.to_bytes(32, 'big'))}"
    _jwt = (token, time.time(), k.key_id)
    return token


_clients: dict[str, httpx.AsyncClient] = {}
_send_slots: asyncio.Semaphore | None = None
_db_slots: asyncio.Semaphore | None = None


def _slots():
    global _send_slots, _db_slots
    if _send_slots is None:
        _send_slots, _db_slots = asyncio.Semaphore(SEND_SLOTS), asyncio.Semaphore(DB_SLOTS)
    return _send_slots, _db_slots


def _base(environment: str) -> str:
    return (get_settings().apns_api_base or APPLE.get(environment, APPLE["production"])).rstrip("/")


def _client(base: str) -> httpx.AsyncClient:
    c = _clients.get(base)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(http2=base.startswith("https"), timeout=httpx.Timeout(TIMEOUT_S, connect=5.0))
        _clients[base] = c
    return c


async def _post(device_token: str, environment: str, payload: dict, headers: dict) -> tuple[int, str, str]:
    """(HTTP status, APNs reason, apns-id). One retry: a new provider token on ExpiredProviderToken, or after 1 s on a
    connection problem / 429 / 5xx. Never raises."""
    k = _key()
    if k is None:
        return 0, "APNs isn't configured", ""
    base = _base(environment)
    body = json.dumps(payload, separators=(",", ":")).encode()
    status, reason, apns_id = 0, "", ""
    for attempt in range(2):
        try:
            send, _ = _slots()
            async with send:
                r = await asyncio.wait_for(_client(base).post(
                    f"{base}/3/device/{device_token}", content=body,
                    headers={"authorization": f"bearer {provider_token(force=attempt > 0 and reason == 'ExpiredProviderToken')}",
                             "apns-topic": k.topic, **headers}), TIMEOUT_S + 2)
            status, apns_id = r.status_code, r.headers.get("apns-id", "")
            reason = "" if status == 200 else (r.json().get("reason", "") if r.content else "") or f"HTTP {status}"
        except Exception as e:  # noqa: BLE001 - network, HTTP/2, timeouts
            status, reason = 0, f"{type(e).__name__}: {e}"[:200]
        if status == 200 or not (status in (0, 429) or status >= 500 or reason == "ExpiredProviderToken"):
            break
        if attempt == 0 and reason != "ExpiredProviderToken":
            await asyncio.sleep(1.0)
    return status, reason, apns_id


# ---------------------------------------------------------------- devices
async def live_devices(db, user_ids) -> dict[int, list[PushDevice]]:
    """{user id: devices} whose app sign-in is still valid; the others are removed (sign-in ended)."""
    from app import sessions
    rows = (await db.execute(select(PushDevice, UserSession).outerjoin(UserSession, UserSession.id == PushDevice.session_id)
                             .where(PushDevice.user_id.in_(list(user_ids))))).all()
    out, dead = {}, []
    for d, s in rows:
        if s is None or s.revoked_at is not None or s.method != "mobile" or sessions.expired_reason(s):
            dead.append(d.id)
        else:
            out.setdefault(d.user_id, []).append(d)
    if dead:
        await db.execute(delete(PushDevice).where(PushDevice.id.in_(dead)))
        await db.commit()
        log.info("apns: %d device(s) removed: their app sign-in ended", len(dead))
    return out


async def drop_for_sessions(db, session_ids) -> int:
    """A sign-in ended (sign out, revoke, sign out everywhere): its iPhone stops getting notifications (the caller commits)."""
    ids = [s for s in session_ids if s]
    if not ids:
        return 0
    r = await db.execute(delete(PushDevice).where(PushDevice.session_id.in_(ids)))
    return r.rowcount or 0


# ---------------------------------------------------------------- sending
async def _send(user_id: int, purpose: str, payload: dict, headers: dict, alarm: Alarm | None, arrived: float | None) -> None:
    send_slot, db_slots = _slots()
    async with db_slots, sessionmaker()() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active or user.deleted_at is not None:
            await db.execute(delete(PushDevice).where(PushDevice.user_id == user_id))     # disabled / deleted: drop
            await db.commit()
            return
        devices = (await live_devices(db, [user_id])).get(user_id, [])
        targets = [(d.id, d.apns_token, d.environment, d.app_version, d.ios_version) for d in devices]
    if not targets:
        return
    results = await asyncio.gather(*(_post(t[1], t[2], payload, headers) for t in targets))
    async with db_slots, sessionmaker()() as db:
        for (dev_id, token, env, app_v, ios_v), (status, reason, apns_id) in zip(targets, results):
            ok = status == 200
            dead = status == 410 or reason in DEAD
            if dead:
                await db.execute(delete(PushDevice).where(PushDevice.id == dev_id))
            elif ok:
                d = await db.get(PushDevice, dev_id)
                if d is not None:
                    d.last_push_at = utcnow()
            label = " · ".join(x for x in (f"iOS {ios_v}" if ios_v else "", f"app {app_v}" if app_v else "", env) if x)
            entry = PushLog(tenant_id=alarm.tenant_id if alarm else user.tenant_id, alarm_id=alarm.id if alarm else None,
                            user_id=user_id, device=label[:80], purpose=purpose,
                            outcome="sent" if ok else ("dropped" if dead else "failed"), status=status or None,
                            reason=reason[:200], apns_id=apns_id[:64],
                            delay_ms=int((time.time() - arrived) * 1000) if ok and arrived else None)
            db.add(entry)
            if alarm is not None:
                from app.audit import timeline_changed
                timeline_changed(db, alarm.tenant_id, alarm.id)
            (log.info if ok else log.warning)("apns: %s for alarm %s to user %s (%s): %s", purpose, alarm.id if alarm else "-",
                                              user_id, env, "accepted" if ok else f"{status} {reason}")
        await db.commit()


def _spawn(coro) -> None:
    from app import sms
    sms._spawn(coro)                  # the texts' background tasks: same time limit, drain and shutdown


def _headers(collapse: str, push_type: str = "alert", priority: int = 10, ttl_s: int = ALARM_TTL_S) -> dict:
    return {"apns-push-type": push_type, "apns-priority": str(priority), "apns-collapse-id": collapse[:64],
            "apns-expiration": str(int(time.time()) + ttl_s)}


def _site(alarm: Alarm) -> str:
    return alarm.site.name if alarm.site else "Site"


def _when(alarm: Alarm) -> str:
    from app import sms
    return sms._clock(alarm)


def alarm_payload(alarm: Alarm, word: str | None = None) -> dict:
    level = {1: "critical", 2: "alarm"}.get(alarm.priority, "warning")
    return {"aps": {"alert": {"title": f"{word or WORD.get(alarm.priority, 'WARNING')} · {_site(alarm)}",
                              "subtitle": alarm.source_name or "",
                              "body": f"{alarm.caption} · {_when(alarm)}"},
                    "sound": sound_for(alarm.priority), "interruption-level": "time-sensitive" if alarm.priority <= 2 else "active",
                    "thread-id": f"alarm-{alarm.id}"},
            "type": "alarm", "alarm_id": alarm.id, "level": level, "site_id": alarm.site_id}


# ---------------------------------------------------------------- flood control (the texts' limit, counted for pushes)
@dataclass
class _Held:
    count: int = 0
    first_id: int = 0
    newest_id: int = 0
    sites: list[str] = field(default_factory=list)
    flushing: bool = False


_sent: dict[int, deque] = {}
_held: dict[int, _Held] = {}


def _window() -> float:
    from app import sms
    return sms.WINDOW_S


def _allow(user_id: int, limit: int) -> bool:
    q = _sent.setdefault(user_id, deque())
    now = time.monotonic()
    while q and now - q[0] >= _window():
        q.popleft()
    if len(q) < max(1, limit):
        q.append(now)
        return True
    return False


async def _limit(db) -> int:
    row = await db.get(PlatformSettings, 1)
    return row.sms_max_per_10min if row is not None and row.sms_max_per_10min else 10


async def _flush(user_id: int) -> None:
    held = _held[user_id]
    try:
        while True:
            q = _sent.get(user_id)
            await asyncio.sleep((max(0.0, _window() - (time.monotonic() - q[0])) if q else 0.0) + 0.05)
            async with sessionmaker()() as db:
                limit = await _limit(db)
            if _allow(user_id, limit):
                break
        _held.pop(user_id, None)
        async with sessionmaker()() as db:
            newest = await db.get(Alarm, held.newest_id)
            if newest is None:
                return
            n, sites = held.count, ", ".join(held.sites[:3]) + (" …" if len(held.sites) > 3 else "")
            payload = {"aps": {"alert": {"title": f"{n} more alarm{'s' if n != 1 else ''}", "body": sites or _site(newest)},
                               "sound": sound_for(newest.priority), "interruption-level": "time-sensitive", "thread-id": "alarm-summary"},
                       "type": "alarm", "summary": True, "count": n, "alarm_id": newest.id,
                       "level": {1: "critical", 2: "alarm"}.get(newest.priority, "warning"), "site_id": newest.site_id}
        await _send(user_id, "summary", payload, _headers("alarm-summary"), newest, None)
    except Exception:  # noqa: BLE001
        log.exception("apns: summary for user %s failed", user_id)
    finally:
        held.flushing = False


# ---------------------------------------------------------------- triggers
def notify(alarms: list[Alarm]) -> None:
    """Every raised alarm (poller._announce, beside sms.notify). Never waits; nothing while APNs isn't set up."""
    if not alarms or not configured():
        return
    arrived = time.time()
    for a in alarms:
        _spawn(_handle(a.id, arrived))


async def _recipients(db, tenant_id: int, whens: tuple) -> list[User]:
    """Active people of the company with a registered iPhone and push_when in whens."""
    has = select(PushDevice.user_id).where(PushDevice.tenant_id == tenant_id)
    return list((await db.scalars(select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True),
                                                     User.deleted_at.is_(None), User.push_when.in_(whens),
                                                     User.id.in_(has)))).all())


async def _handle(alarm_id: int, arrived: float) -> None:
    try:
        _, db_slots = _slots()
        plan = []
        async with db_slots, sessionmaker()() as db:
            alarm = await db.get(Alarm, alarm_id)
            if alarm is None or alarm.state != "new":
                return
            limit = await _limit(db)
            for u in await _recipients(db, alarm.tenant_id, ("raised", "both")):
                if LEVELS.get(level_of(u), 0) < alarm.priority or (u.sms_sites is not None and alarm.site_id not in u.sms_sites):
                    continue
                if _allow(u.id, limit):
                    plan.append(u.id)
                    continue
                held = _held.setdefault(u.id, _Held(first_id=alarm.id))
                held.count += 1
                held.newest_id = max(held.newest_id, alarm.id)
                held.first_id = min(held.first_id, alarm.id)
                if alarm.site and alarm.site.name not in held.sites:
                    held.sites.append(alarm.site.name)
                if not held.flushing:
                    held.flushing = True
                    from app import sms
                    sms._spawn(_flush(u.id), sms._flushers, limit_s=_window() + sms.TASK_MAX_S)
                log.info("apns: alarm %s held back for user %s (over %d in 10 min)", alarm.id, u.id, limit)
            payload = alarm_payload(alarm)
        for uid in plan:
            _spawn(_send(uid, "alarm", payload, _headers(f"alarm-{alarm.id}"), alarm, arrived))
    except Exception:  # noqa: BLE001
        log.exception("apns: alarm %s could not be pushed", alarm_id)


def notify_verdicts(confirmed: list[int], withdrawn: list[tuple[int, str]], by_id: int) -> None:
    """Beside sms.notify_verdicts: Real event (CONFIRMED) and Real event withdrawn (STAND DOWN)."""
    if not configured():
        return
    arrived = time.time()
    if confirmed:
        _spawn(_handle_confirmed(list(confirmed), by_id, arrived))
    if withdrawn:
        _spawn(_handle_withdrawn(list(withdrawn), by_id))


async def _handle_confirmed(alarm_ids: list[int], by_id: int, arrived: float) -> None:
    from app import platform_settings, sms
    from app.models import Tenant, Tracker
    from app.services import dispatch
    try:
        _, db_slots = _slots()
        plan = []
        async with db_slots, sessionmaker()() as db:
            alarms = [a for a in (await db.scalars(select(Alarm).where(Alarm.id.in_(alarm_ids), Alarm.verdict == "real"))).unique().all()
                      if sms._recent(a)]
            by = await db.get(User, by_id)
            for tenant_id in sorted({a.tenant_id for a in alarms}):
                group = [a for a in alarms if a.tenant_id == tenant_id]
                users = [u for u in await _recipients(db, tenant_id, ("confirmed", "both")) if level_of(u) != "off"]
                # As the texts: guards who'll be auto-dispatched get the DISPATCH push instead (sms._handle_confirmed).
                if platform_settings.current().sms_ready and dispatch.auto_on(await db.get(Tenant, tenant_id)):
                    linked = set((await db.scalars(select(Tracker.user_id).where(
                        Tracker.tenant_id == tenant_id, Tracker.enabled.is_(True), Tracker.user_id.is_not(None)))).all())
                    newest = max(group, key=lambda a: a.id)
                    guards = {u.id for u in users if u.id in linked and (u.sms_sites is None or newest.site_id in u.sms_sites)}
                    if guards and (await dispatch.already(db, newest.id) or await dispatch.pick_closest(db, newest, guards)):
                        users = [u for u in users if u.id not in guards]
                for u in users:
                    if u.id == by_id:
                        continue
                    mine = [a for a in group if u.sms_sites is None or a.site_id in u.sms_sites]
                    if not mine:
                        continue
                    newest = max(mine, key=lambda a: a.id)
                    _sent.setdefault(u.id, deque()).append(time.monotonic())       # counts toward the window, never held
                    p = alarm_payload(newest, "CONFIRMED")
                    extra = f" · +{len(mine) - 1} more" if len(mine) > 1 else ""
                    p["aps"]["alert"]["body"] = f"{newest.caption} · {_when(newest)}{extra}" + (f" · by {by.label}" if by else "")
                    p["confirmed_by"] = by.label if by else ""
                    plan.append((u.id, p, newest))
        for uid, p, a in plan:
            _spawn(_send(uid, "confirmed", p, _headers(f"alarm-{a.id}"), a, arrived))
    except Exception:  # noqa: BLE001
        log.exception("apns: confirmed alarms %s could not be pushed", alarm_ids)


async def _handle_withdrawn(changes: list[tuple[int, str]], by_id: int) -> None:
    """Real event changed to False alarm / Dismissed: STAND DOWN to everyone pushed CONFIRMED for it."""
    try:
        verdicts = dict(changes)
        plan = []
        async with sessionmaker()() as db:
            rows = (await db.execute(select(PushLog.user_id, PushLog.alarm_id).where(
                PushLog.alarm_id.in_(list(verdicts)), PushLog.purpose == "confirmed", PushLog.outcome == "sent").distinct())).all()
            by = await db.get(User, by_id)
            for uid, aid in rows:
                a = await db.get(Alarm, aid)
                if uid is None or a is None or a.verdict == "real":
                    continue
                word = {"false": "False alarm", "dismissed": "Dismissed"}.get(verdicts.get(aid, a.verdict), "No longer real")
                plan.append((uid, a, {"aps": {"alert": {"title": f"STAND DOWN · {_site(a)}",
                                                        "body": f"{a.caption}: {word}" + (f" (by {by.label})" if by else "")},
                                              "sound": "default", "interruption-level": "time-sensitive", "thread-id": f"alarm-{a.id}"},
                                      "type": "call_ended", "alarm_id": a.id, "verdict": verdicts.get(aid, a.verdict)}))
        for uid, a, p in plan:
            _spawn(_send(uid, "stand_down", p, _headers(f"alarm-{a.id}"), a, None))
    except Exception:  # noqa: BLE001
        log.exception("apns: stand-down pushes for %s failed", changes)


def dispatch(alarm: Alarm, user_id: int, link_token: str, distance: str, by: str, auto: bool = False) -> None:
    """Beside the DISPATCH text (services/dispatch.py): the same link token, so the app opens that dispatch."""
    if not configured():
        return
    where = ", ".join(x for x in ((alarm.site.address if alarm.site else ""), f"{distance} away" if distance else "") if x)
    p = {"aps": {"alert": {"title": f"DISPATCH · {_site(alarm)}", "subtitle": alarm.caption,
                           "body": where or (f"Sent by {by}" if by else "Open for directions")},
                 "sound": SOUND[1], "interruption-level": "time-sensitive", "thread-id": f"alarm-{alarm.id}"},
         "type": "dispatch", "alarm_id": alarm.id, "link_token": link_token, "site_id": alarm.site_id,
         "level": {1: "critical", 2: "alarm"}.get(alarm.priority, "warning"), "auto": auto}
    _spawn(_send(user_id, "dispatch", p, _headers(f"dispatch-{alarm.id}"), alarm, time.time()))


def call_update(call: Alarm, alarm: Alarm, user_id: int, count: int = 1) -> None:
    """A new alarm joined the active call a guard was dispatched to (services/calls.py): tell their app."""
    if not configured():
        return
    more = f" (+{count - 1} more)" if count > 1 else ""
    p = {"aps": {"alert": {"title": f"UPDATE · {_site(call)}", "subtitle": alarm.source_name or "",
                           "body": f"{alarm.caption}{more} · {_when(alarm)}"},
                 "sound": sound_for(alarm.priority), "interruption-level": "time-sensitive", "thread-id": f"alarm-{call.id}"},
         "type": "call_update", "alarm_id": call.id, "joined_alarm_id": alarm.id, "count": count,
         "level": {1: "critical", 2: "alarm"}.get(alarm.priority, "warning"), "site_id": call.site_id}
    _spawn(_send(user_id, "call_update", p, _headers(f"update-{alarm.id}"), call, time.time()))


def call_ended(alarm_id: int, by: str, verdict: str = "") -> None:
    """Beside the CALL ENDED / STAND DOWN texts (services/calls.py): dispatched guards who haven't tapped Clear."""
    if configured():
        _spawn(_call_ended(alarm_id, by, verdict))


async def _call_ended(alarm_id: int, by: str, verdict: str) -> None:
    from app.services import dispatch
    async with sessionmaker()() as db:
        alarm = await db.get(Alarm, alarm_id)
        if alarm is None:
            return
        people = [d["user_id"] for d in await dispatch.dispatched(db, alarm_id) if d.get("user_id") and d["status"] != "clear"]
    stand_down = verdict in ("false", "dismissed")
    title = f"{'STAND DOWN' if stand_down else 'CALL ENDED'} · {_site(alarm)}"
    body = (f"{alarm.caption}: {'False alarm' if verdict == 'false' else 'Dismissed'}" if stand_down
            else f"{alarm.caption}: call ended") + (f" (by {by})" if by else "")
    if alarm.call_end_note and not stand_down:
        body += f" · {alarm.call_end_note[:120]}"
    p = {"aps": {"alert": {"title": title, "body": body}, "sound": "default", "interruption-level": "time-sensitive",
                 "thread-id": f"alarm-{alarm.id}"}, "type": "call_ended", "alarm_id": alarm.id}
    for uid in dict.fromkeys(people):
        _spawn(_send(uid, "call_ended", p, _headers(f"dispatch-{alarm.id}"), alarm, None))


def notify_closed(alarm_ids: list[int], by: str, verdict: str) -> None:
    """Someone closed alarms (ack, bulk): replace their alarm notification (same collapse id) on everyone's phone, quietly."""
    if alarm_ids and configured():
        _spawn(_closed(list(alarm_ids), by, verdict))


async def _closed(alarm_ids: list[int], by: str, verdict: str) -> None:
    async with sessionmaker()() as db:
        rows = (await db.execute(select(PushLog.user_id, PushLog.alarm_id).where(
            PushLog.alarm_id.in_(alarm_ids), PushLog.purpose.in_(("alarm", "summary")), PushLog.outcome == "sent").distinct())).all()
        alarms = {a.id: a for a in (await db.scalars(select(Alarm).where(Alarm.id.in_({aid for _, aid in rows})))).unique().all()}
    word = {"real": "Real event", "false": "False alarm", "dismissed": "Dismissed"}.get(verdict, "Closed")
    for uid, aid in rows:
        a = alarms.get(aid)
        if uid is None or a is None:
            continue
        p = {"aps": {"alert": {"title": f"CLOSED · {_site(a)}", "body": f"{a.caption}: {word}" + (f" (by {by})" if by else "")},
                     "interruption-level": "passive", "thread-id": f"alarm-{a.id}"},
             "type": "alarm_closed", "alarm_id": a.id, "verdict": verdict}
        _spawn(_send(uid, "closed", p, _headers(f"alarm-{a.id}", priority=5), a, None))


async def send_test(user_id: int) -> list[dict]:
    """The Account page's / app's "Send a test notification": to each of this person's iPhones, at once."""
    p = {"aps": {"alert": {"title": "TWG Alarm Portal", "body": "Test notification: alarms will look like this."},
                 "sound": "default", "interruption-level": "active"}, "type": "test"}
    async with sessionmaker()() as db:
        n = len((await live_devices(db, [user_id])).get(user_id, []))
    if not n:
        return []
    await _send(user_id, "test", p, _headers("test", ttl_s=300), None, None)
    async with sessionmaker()() as db:
        rows = (await db.scalars(select(PushLog).where(PushLog.user_id == user_id, PushLog.purpose == "test")
                                 .order_by(PushLog.id.desc()).limit(n))).all()
    return [{"outcome": r.outcome, "status": r.status, "reason": r.reason, "device": r.device} for r in rows]


def status() -> dict:
    s = get_settings()
    return {"configured": configured(), "key_id": s.apns_key_id, "team_id": s.apns_team_id, "topic": s.apns_topic,
            "api": s.apns_api_base or "Apple (production / sandbox per device)"}


async def close() -> None:
    for c in list(_clients.values()):
        await c.aclose()
    _clients.clear()


def reset() -> None:
    """Tests: forget flood state, provider token, connections and per-loop semaphores."""
    global _send_slots, _db_slots, _jwt
    _sent.clear()
    _held.clear()
    _clients.clear()
    _send_slots = _db_slots = None
    _jwt = None


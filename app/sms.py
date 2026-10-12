"""Text messages for alarms, through Twilio, each with a no-sign-in link to that alarm (app/alarm_links.py).

Alarm safety: poller._announce calls notify(), which never waits and never touches the database: with
texts off it returns at once, otherwise it starts a background task. The tasks take turns for the
database (DB_SLOTS, so a burst of hundreds of alarms can't empty the connection pool that ingest needs)
and never hold a connection while talking to Twilio (10 s timeout; 3 attempts on network errors, 429 and
5xx, 2 s and 6 s apart). A slow, failing or misconfigured Twilio never touches ingest or the live feed.

Who gets a text: active users of the alarm's own company with a mobile number and "Text alerts" set to
critical (critical alarms only) or alarm (critical and alarm). Warnings never text, and other companies'
alarms never text (the same rule as sound and pop-ups). Invited people who haven't signed in yet count:
someone can get texts without ever using the portal.

Flood control: at most sms_max_per_10min texts per person in any 10 minutes. Alarms past that are
counted, and one summary text ("12 more alarms at ...", with a link to the newest) goes out as soon as
the window allows. Nothing is dropped silently.

Every attempt is written to sms_log (number, outcome, Twilio's message id and error, and the delay from
the alarm reaching the portal to Twilio accepting the text), never the link. The last success or error
shows on the Platform page.
"""

import asyncio
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import alarm_links, platform_settings
from app.config import get_settings
from app.db import sessionmaker
from app.models import Alarm, PlatformSettings, SmsLog, User, utcnow
from app.security import decrypt
from app.services import arming

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (2, 6)
TIMEOUT_S = 10
WINDOW_S = 600
DB_SLOTS = 2                 # background tasks using the database at once (the pool is 5 + 10; ingest comes first)
SEND_SLOTS = 4               # Twilio requests at once
LEVELS = {"off": 0, "critical": 1, "alarm": 2}    # the loudest alarm priority that texts (1 critical, 2 alarm)
LEVEL_WORD = {1: "CRITICAL", 2: "ALARM", 3: "WARNING"}


# ---------------------------------------------------------------- phone numbers
def normalize_phone(raw: str) -> str:
    """'' stays ''. US numbers may be typed any way ((570) 555-1234, 570.555.1234, 1-570-...);
    other countries need the + and country code. Raises ValueError with a readable message."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        number = "+" + digits
    elif len(digits) == 10:
        number = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        number = "+" + digits
    else:
        raise ValueError(f"'{raw}' isn't a mobile number. Use 10 digits for the US, or + and the country code.")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", number) or (number.startswith("+1") and len(number) != 12):
        raise ValueError(f"'{raw}' isn't a mobile number. Use 10 digits for the US, or + and the country code.")
    return number


def masked(phone: str) -> str:
    """For logs: +1******1234."""
    return phone[:2] + "*" * (len(phone) - 6) + phone[-4:] if len(phone) > 6 else phone


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Twilio:
    sid: str
    token: str
    sender: str                 # +1... number, or a Messaging Service SID (MG...)
    link_hours: int
    max_per_window: int


async def config(db=None) -> Twilio | None:
    """The Twilio settings, or None when texts are off or incomplete."""
    if db is None:
        async with sessionmaker()() as s:
            row = await s.get(PlatformSettings, 1)
    else:
        row = await db.get(PlatformSettings, 1)
    if row is None or not row.sms_enabled or not (row.twilio_sid and row.twilio_token_enc and row.twilio_from):
        return None
    try:
        token = decrypt(row.twilio_token_enc)
    except Exception:  # noqa: BLE001 - FERNET_KEY changed
        log.error("sms: the Twilio auth token can't be decrypted; enter it again on the Platform page")
        return None
    return Twilio(row.twilio_sid, token, row.twilio_from, row.sms_link_hours, row.sms_max_per_10min)


# ---------------------------------------------------------------- the words
def _ascii(s: str, limit: int) -> str:
    """Plain GSM-friendly text: one curly quote or emoji would switch the whole text to 70-character parts."""
    s = (s or "").replace("—", "-").replace("–", "-").replace("’", "'").replace("‘", "'")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def _clock(alarm: Alarm) -> str:
    tz = arming.zone(alarm.site.timezone if alarm.site else None)
    t = datetime.fromtimestamp(alarm.event_ts_ms / 1000, timezone.utc).astimezone(tz)
    return t.strftime("%-I:%M:%S %p %Z")


def alarm_text(alarm: Alarm, link: str) -> str:
    what = _ascii(alarm.caption or alarm.event_type, 60)
    where = _ascii(alarm.site.name if alarm.site else "", 40)
    cam = _ascii(alarm.source_name, 40)
    parts = [f"TWG {LEVEL_WORD.get(alarm.priority, 'ALARM')}: {what}", where]
    if cam and cam.lower() not in what.lower():
        parts.append(cam)
    parts.append(_clock(alarm))
    return f"{', '.join(p for p in parts if p)}. View: {link}"


def summary_text(count: int, sites: list[str], since: Alarm, newest: Alarm, link: str) -> str:
    names = ", ".join(_ascii(s, 30) for s in sites[:3]) + (f" +{len(sites) - 3}" if len(sites) > 3 else "")
    return (f"TWG: {count} more alarm{'s' if count != 1 else ''} at {names} since {_clock(since)} "
            f"(too many to text one by one). Newest: {link}")


def test_text(by: str) -> str:
    return f"TWG Alarm Portal test text, sent by {_ascii(by, 60)}. Alarm texts will look like this, with a link to the alarm."


# ---------------------------------------------------------------- Twilio
async def _post(cfg: Twilio, to: str, body: str) -> tuple[int, dict]:
    base = get_settings().twilio_api_base.rstrip("/")
    data = {"To": to, "Body": body}
    data["MessagingServiceSid" if cfg.sender.startswith("MG") else "From"] = cfg.sender
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(f"{base}/2010-04-01/Accounts/{cfg.sid}/Messages.json", data=data, auth=(cfg.sid, cfg.token))
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload if isinstance(payload, dict) else {}


_send_slots: asyncio.Semaphore | None = None
_db_slots: asyncio.Semaphore | None = None


def _slots():
    global _send_slots, _db_slots
    if _send_slots is None:
        _send_slots, _db_slots = asyncio.Semaphore(SEND_SLOTS), asyncio.Semaphore(DB_SLOTS)
    return _send_slots, _db_slots


async def deliver(cfg: Twilio, to: str, body: str, *, retry: bool = True) -> tuple[str, str, str, int]:
    """(outcome sent|failed, error, Twilio message SID, attempts). Retries network errors, 429 and 5xx;
    a 4xx (bad number, unverified trial number, wrong token) fails straight away."""
    send_slots, _ = _slots()
    error, attempts = "", 0
    async with send_slots:
        for delay in (0, *RETRY_DELAYS_S) if retry else (0,):
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                status, payload = await _post(cfg, to, body)
            except httpx.HTTPError as e:
                error = f"Twilio unreachable: {type(e).__name__}: {e}"[:500]
                log.warning("sms to %s: attempt %d: %s", masked(to), attempts, error)
                continue
            if status in (200, 201):
                return "sent", "", str(payload.get("sid", ""))[:64], attempts
            error = f"Twilio {payload.get('code') or status}: {payload.get('message') or f'HTTP {status}'}"[:500]
            log.warning("sms to %s: attempt %d: %s", masked(to), attempts, error)
            if status != 429 and status < 500:
                break
    return "failed", error, "", attempts


async def _record(to: str, purpose: str, outcome: str, error: str, attempts: int, sid: str, *, alarm: Alarm | None = None,
                  user: User | None = None, tenant_id: int | None = None, delay_ms: int | None = None) -> None:
    _, db_slots = _slots()
    try:
        async with db_slots, sessionmaker()() as db:
            db.add(SmsLog(to=to, purpose=purpose, outcome=outcome, error=error, attempts=attempts, twilio_sid=sid,
                          delay_ms=delay_ms, alarm_id=alarm.id if alarm else None,
                          tenant_id=tenant_id if tenant_id is not None else (alarm.tenant_id if alarm else None),
                          user_id=user.id if user else None))
            row = await db.get(PlatformSettings, 1)
            if row is not None and outcome in ("sent", "failed"):
                if outcome == "sent":
                    row.sms_last_ok_at = utcnow()
                else:
                    row.sms_last_error, row.sms_last_error_at = f"{masked(to)}: {error}"[:500], utcnow()
            await db.commit()
    except Exception as e:  # noqa: BLE001
        log.error("sms: could not write the send log: %s", e)


async def _send(cfg: Twilio, user: User, body: str, purpose: str, alarm: Alarm, arrived: float) -> None:
    outcome, error, sid, attempts = await deliver(cfg, user.phone, body)
    delay_ms = int((time.time() - arrived) * 1000)
    (log.info if outcome == "sent" else log.error)("sms %s for alarm %s to %s: %s after %d ms%s", purpose, alarm.id,
                                                   masked(user.phone), outcome, delay_ms, f" ({error})" if error else "")
    await _record(user.phone, purpose, outcome, error, attempts, sid, alarm=alarm, user=user,
                  delay_ms=delay_ms if outcome == "sent" else None)


async def send_test(to: str, by: str, *, user: User | None = None) -> tuple[str, str]:
    cfg = await config()
    if cfg is None:
        return "disabled", "Text messages are switched off or not set up yet."
    outcome, error, sid, attempts = await deliver(cfg, to, test_text(by), retry=False)
    await _record(to, "test", outcome, error, attempts, sid, user=user, tenant_id=user.tenant_id if user else None)
    return outcome, error


# ---------------------------------------------------------------- flood control
@dataclass
class _Held:
    count: int = 0
    first_id: int = 0
    newest_id: int = 0
    sites: list[str] = field(default_factory=list)
    flushing: bool = False


_sent: dict[int, deque] = {}          # user id -> monotonic times of texts in the last WINDOW_S
_held: dict[int, _Held] = {}


def _allow(user_id: int, limit: int) -> bool:
    q = _sent.setdefault(user_id, deque())
    now = time.monotonic()
    while q and now - q[0] >= WINDOW_S:
        q.popleft()
    if len(q) < max(1, limit):
        q.append(now)
        return True
    return False


def _wait_s(user_id: int) -> float:
    q = _sent.get(user_id)
    return max(0.0, WINDOW_S - (time.monotonic() - q[0])) if q else 0.0


async def _flush(user_id: int) -> None:
    """Send one summary for the alarms held back for this person, once the window has room."""
    held = _held[user_id]
    try:
        while True:
            await asyncio.sleep(_wait_s(user_id) + 0.05)
            cfg = await config()
            if cfg is None:
                return
            if _allow(user_id, cfg.max_per_window):
                break
        _held.pop(user_id, None)
        _, db_slots = _slots()
        async with db_slots, sessionmaker()() as db:
            user = await db.get(User, user_id)
            first, newest = await db.get(Alarm, held.first_id), await db.get(Alarm, held.newest_id)
            if user is None or not user.is_active or not user.phone or newest is None:
                return
            token = await alarm_links.mint(db, newest, user, cfg.link_hours)
            await db.commit()
            body = summary_text(held.count, held.sites, first or newest, newest, alarm_links.url(token))
        await _send(cfg, user, body, "summary", newest, time.time())
    except Exception:  # noqa: BLE001
        log.exception("sms: summary for user %s failed", user_id)
    finally:
        held.flushing = False


# ---------------------------------------------------------------- alarms
_tasks: set[asyncio.Task] = set()
_flushers: set[asyncio.Task] = set()     # summaries waiting for the window (minutes): drain() doesn't wait for them


def _spawn(coro, tasks: set = _tasks) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def notify(alarms: list[Alarm]) -> None:
    """Called for every raised alarm (poller._announce). Never waits; does nothing while texts are off."""
    if not alarms or not platform_settings.current().sms_ready:
        return
    arrived = time.time()
    for a in alarms:
        if a.priority <= 2:
            _spawn(_handle(a.id, arrived))


async def _handle(alarm_id: int, arrived: float) -> None:
    try:
        _, db_slots = _slots()
        plan: list[tuple[User, str]] = []
        async with db_slots, sessionmaker()() as db:
            cfg = await config(db)
            alarm = await db.get(Alarm, alarm_id)
            if cfg is None or alarm is None or alarm.state != "new":
                return
            levels = [k for k, v in LEVELS.items() if v >= alarm.priority]
            users = (await db.scalars(select(User).where(User.tenant_id == alarm.tenant_id, User.is_active.is_(True),
                                                         User.phone != "", User.sms_level.in_(levels)))).all()
            for u in users:
                if _allow(u.id, cfg.max_per_window):
                    token = await alarm_links.mint(db, alarm, u, cfg.link_hours)
                    plan.append((u, alarm_text(alarm, alarm_links.url(token))))
                    continue
                held = _held.setdefault(u.id, _Held(first_id=alarm.id))
                held.count += 1
                held.newest_id = alarm.id
                if alarm.site and alarm.site.name not in held.sites:
                    held.sites.append(alarm.site.name)
                if not held.flushing:
                    held.flushing = True
                    _spawn(_flush(u.id), _flushers)
                log.info("sms: alarm %s held back for %s (over %d texts in 10 min)", alarm.id, masked(u.phone),
                         cfg.max_per_window)
            await db.commit()
        for u, body in plan:
            _spawn(_send(cfg, u, body, "alarm", alarm, arrived))
    except Exception:  # noqa: BLE001 - never let a text problem escape into the event loop's error log only
        log.exception("sms: alarm %s could not be texted", alarm_id)


async def drain() -> None:
    """Tests: wait for background texts (summaries waiting on the window are cancelled)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset() -> None:
    """Tests: forget flood-control state and per-loop semaphores."""
    global _send_slots, _db_slots
    for t in [*_tasks, *_flushers]:
        t.cancel()
    _tasks.clear()
    _flushers.clear()
    _sent.clear()
    _held.clear()
    _send_slots = _db_slots = None

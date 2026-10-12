"""Alarm text messages (Twilio, faked with respx) and the no-sign-in alarm links they carry."""
import asyncio
import time
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select

from app import alarm_links, platform_settings, sms
from app.models import Alarm, AlarmLink, AuditLog, AuthEvent, PlatformSettings, SmsLog, Tenant, User, utcnow
from app.security import encrypt, hash_password
from app.services.bus import bus
from tests.conftest import NX, PASSWORD, login, make_site, nx_row

SID = "AC" + "0" * 32
TWILIO = f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"


@pytest.fixture(autouse=True)
def fresh_sms(monkeypatch):
    sms.reset()
    monkeypatch.setattr(sms, "RETRY_DELAYS_S", (0.01, 0.02))
    yield
    sms.reset()


async def texts_on(session, **kw):
    row = await platform_settings.get_row(session)
    row.sms_enabled, row.twilio_sid, row.twilio_token_enc, row.twilio_from = True, SID, encrypt("tok-secret"), "+15705550000"
    row.portal_url = "https://alarmportal.twgsecurity.net"
    for k, v in kw.items():
        setattr(row, k, v)
    await session.commit()
    await platform_settings.refresh()


async def world(session):
    t = Tenant(name="TWG Security", kind="platform")
    other = Tenant(name="Acme")
    session.add_all([t, other])
    await session.flush()
    mk = lambda tid, email, **kw: User(tenant_id=tid, email=email, display_name=email.split("@")[0],  # noqa: E731
                                       password_hash=hash_password(PASSWORD), role="admin", **kw)
    people = {
        "admin": mk(t.id, "admin@twgsecurity.com"),
        "all": mk(t.id, "all@twgsecurity.com", phone="+15705551111", sms_level="alarm"),
        "crit": mk(t.id, "crit@twgsecurity.com", phone="+15705552222", sms_level="critical"),
        "off": mk(t.id, "off@twgsecurity.com", phone="+15705553333", sms_level="off"),
        "gone": mk(t.id, "gone@twgsecurity.com", phone="+15705554444", sms_level="alarm", is_active=False),
        "acme": mk(other.id, "x@acme.test", phone="+15705555555", sms_level="alarm"),
    }
    session.add_all(people.values())
    await session.commit()
    site = await make_site(session, t, name="TWG Security Office")
    site.timezone = "America/New_York"
    await session.commit()
    return t, site, people


n = 0


async def make_alarm(session, site, priority=2, caption="Panic button", **kw):
    global n
    n += 1
    a = Alarm(tenant_id=site.tenant_id, site_id=site.id, event_key=f"k{n}", event_type="softTrigger", priority=priority,
              caption=caption, source_name="Front Door Cam", device_id="dev-1", event_ts_ms=1_760_282_602_000, **kw)
    session.add(a)
    await session.commit()
    return await session.get(Alarm, a.id)


def sent_to(route):
    return [parse_qs(c.request.content.decode())["To"][0] for c in route.calls]


def body_of(call):
    return parse_qs(call.request.content.decode())["Body"][0]


# ---------------------------------------------------------------- numbers and words
def test_phone_numbers():
    assert sms.normalize_phone("(570) 555-1234") == "+15705551234"
    assert sms.normalize_phone("1-570-555-1234") == "+15705551234"
    assert sms.normalize_phone("+44 7700 900123") == "+447700900123"
    assert sms.normalize_phone("  ") == ""
    for bad in ("555-1234", "+1 570 555 12345", "hello", "+0123456789"):
        with pytest.raises(ValueError):
            sms.normalize_phone(bad)
    assert sms.masked("+15705551234") == "+1******1234"


async def test_alarm_text_is_plain_and_short(session):
    _, site, _ = await world(session)
    a = await make_alarm(session, site, priority=1, caption="Person — “loitering” \U0001F6A8 at gate")
    text = sms.alarm_text(a, "https://alarmportal.twgsecurity.net/a/" + "x" * 24)
    assert text.startswith("TWG CRITICAL: Person - loitering at gate, TWG Security Office, Front Door Cam, ")
    assert "11:23:22 AM EDT" in text and text.endswith("View: https://alarmportal.twgsecurity.net/a/" + "x" * 24)
    assert text.isascii() and len(text) <= 306           # plain text: at most 2 SMS segments


# ---------------------------------------------------------------- who gets texted
@respx.mock
async def test_alarm_texts_the_right_people_with_a_working_link(session, client):
    _, site, p = await world(session)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SM1", "status": "queued"}))

    alarm = await make_alarm(session, site, priority=2)
    sms.notify([alarm])
    await sms.drain()
    assert sent_to(twilio) == ["+15705551111"]          # alarm level: not critical-only, off, disabled or another company
    call = twilio.calls.last.request
    assert call.headers["authorization"].startswith("Basic ")
    assert parse_qs(call.content.decode())["From"] == ["+15705550000"]

    crit = await make_alarm(session, site, priority=1)
    warn = await make_alarm(session, site, priority=3)
    sms.notify([crit, warn])
    await sms.drain()
    assert sorted(sent_to(twilio)[1:]) == ["+15705551111", "+15705552222"]      # warnings never text

    log = (await session.scalars(select(SmsLog).order_by(SmsLog.id))).all()
    assert [r.outcome for r in log] == ["sent"] * 3 and log[0].twilio_sid == "SM1" and log[0].delay_ms is not None
    assert log[0].user_id == p["all"].id and log[0].alarm_id == alarm.id

    # The link opens that alarm without signing in, privately.
    link = body_of(twilio.calls[0]).rsplit(" ", 1)[-1]
    token = link.rsplit("/", 1)[-1]
    assert link.startswith("https://alarmportal.twgsecurity.net/a/") and len(token) == 24
    r = await client.get(f"/a/{token}")
    assert r.status_code == 200 and "Panic button" in r.text and "TWG Security Office" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in r.headers["x-robots-tag"]
    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "alarm.link_opened"))
    assert audit.alarm_id == alarm.id and audit.user_id == p["all"].id
    # Only that alarm: another alarm's clip name is refused.
    assert (await client.get(f"/media/a/{token}/clips/a{crit.id}_10_20_sd.mp4")).status_code == 404


async def test_texts_off_does_nothing(session):
    _, site, _ = await world(session)
    alarm = await make_alarm(session, site)
    sms.notify([alarm])
    assert not sms._tasks


# ---------------------------------------------------------------- links
async def test_link_states(session, client):
    _, site, p = await world(session)
    alarm = await make_alarm(session, site)
    good = await alarm_links.mint(session, alarm, p["all"], 24)
    old = await alarm_links.mint(session, alarm, p["all"], 24)
    gone = await alarm_links.mint(session, alarm, p["gone"], 24)
    await session.commit()
    row = await session.scalar(select(AlarmLink).where(AlarmLink.token_hash == alarm_links._hash(old)))
    row.expires_at = utcnow() - timedelta(minutes=1)
    await session.commit()

    assert (await client.get(f"/a/{good}")).status_code == 200
    r = await client.get(f"/a/{old}")
    assert r.status_code == 410 and "expired" in r.text and "Panic button" not in r.text
    assert (await client.get(f"/api/a/{old}/clip")).status_code == 410
    r = await client.get(f"/a/{gone}")                        # sent to someone who's been disabled
    assert r.status_code == 404 and "Panic button" not in r.text
    assert (await client.get("/media/a/not-a-real-token/live.jpg")).status_code == 404
    fails = (await session.scalars(select(AuthEvent).where(AuthEvent.kind == "alarm_link"))).all()
    assert len(fails) == 1                                    # only unknown tokens count as guessing


async def test_guessing_links_gets_banned(session, client):
    await world(session)
    client.headers["CF-Connecting-IP"] = "203.0.113.7"         # requests come from 127.0.0.1, which is never banned
    for i in range(5):
        assert (await client.get(f"/a/guess{i}")).status_code == 404
    r = await client.get("/a/guess9")
    assert r.status_code == 403 and "blocked" in r.text.lower()


# ---------------------------------------------------------------- failures
@respx.mock
async def test_twilio_errors_retry_or_fail_loudly(session):
    _, site, _ = await world(session)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(side_effect=[httpx.Response(503), httpx.ConnectError("down"),
                                                  httpx.Response(201, json={"sid": "SM9"})])
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    log = await session.scalar(select(SmsLog))
    assert (log.outcome, log.attempts, log.twilio_sid) == ("sent", 3, "SM9")

    twilio.side_effect = None
    twilio.return_value = httpx.Response(400, json={"code": 21211, "message": "Invalid 'To' Phone Number"})
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    log = await session.scalar(select(SmsLog).order_by(SmsLog.id.desc()))
    assert (log.outcome, log.attempts) == ("failed", 1)                 # a 4xx isn't retried
    assert log.error == "Twilio 21211: Invalid 'To' Phone Number"
    row = await session.get(PlatformSettings, 1)
    await session.refresh(row)
    assert "21211" in row.sms_last_error and row.sms_last_error.startswith("+1******1111")


@respx.mock
async def test_a_hanging_twilio_never_delays_the_alarm(session):
    """The alarm goes to browsers first and the poll returns at once, even with Twilio hanging."""
    from app.services.poller import manager
    _, site, _ = await world(session)
    await texts_on(session)
    site.event_cursor_ms = 5_000_000
    await session.commit()
    hang = asyncio.Event()

    async def slow(request):
        await hang.wait()
        return httpx.Response(201, json={"sid": "SMslow"})
    respx.post(TWILIO).mock(side_effect=slow)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{NX}/rest/v4/events/log").mock(return_value=httpx.Response(200, json=[
        nx_row(5_001_000 + i, action_id=f"x{i}", caption=f"Gate {i}") for i in range(20)]))
    sub = bus.subscribe(site.tenant_id)

    t0 = time.perf_counter()
    created = await manager.poll_once(manager._runtime(site))
    took = time.perf_counter() - t0
    assert len(created) == 20 and took < 1.0, took
    names = [sub.queue.get_nowait()[0] for _ in range(sub.queue.qsize())]
    assert names.count("alarm.new") == 20
    bus.unsubscribe(sub)
    await asyncio.sleep(0.2)
    assert sms._tasks                                                     # texts still waiting on Twilio
    hang.set()
    await sms.drain()
    assert await session.scalar(select(SmsLog).where(SmsLog.outcome == "sent")) is not None


# ---------------------------------------------------------------- flood control
@respx.mock
async def test_floods_become_one_summary(session, monkeypatch):
    _, site, _ = await world(session)
    await texts_on(session, sms_max_per_10min=2)
    monkeypatch.setattr(sms, "WINDOW_S", 0.5)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMx"}))
    alarms = [await make_alarm(session, site, caption=f"Line crossed {i}") for i in range(6)]
    sms.notify(alarms)
    await sms.drain()
    assert len(twilio.calls) == 2                                         # the limit
    await asyncio.wait_for(asyncio.gather(*list(sms._flushers)), 5)
    assert len(twilio.calls) == 3
    summary = body_of(twilio.calls.last)
    assert summary.startswith("TWG: 4 more alarms at TWG Security Office since ") and "/a/" in summary
    log = await session.scalar(select(SmsLog).where(SmsLog.purpose == "summary"))
    assert log.alarm_id == alarms[-1].id                                  # the link is to the newest


# ---------------------------------------------------------------- settings and the Users page
@respx.mock
async def test_users_page_numbers_and_test_text(session, client):
    _, site, p = await world(session)
    await login(client, "admin@twgsecurity.com")
    uid = p["admin"].id
    r = await client.put(f"/api/users/{uid}", json={"sms_level": "alarm"})
    assert r.status_code == 400 and "mobile number" in r.json()["detail"]
    r = await client.put(f"/api/users/{uid}", json={"phone": "570.555.9999", "sms_level": "critical"})
    assert r.json()["phone"] == "+15705559999" and r.json()["sms_level"] == "critical"
    assert (await client.put(f"/api/users/{uid}", json={"phone": "12345"})).status_code == 400
    r = await client.post("/api/users", json={"email": "night@twgsecurity.com", "password": "A-long-password-1!",
                                             "phone": "(570) 555-7777", "sms_level": "alarm"})
    assert r.json()["phone"] == "+15705557777"

    assert (await client.post(f"/api/users/{uid}/test-text")).json()["outcome"] == "disabled"
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMt"}))
    assert (await client.post(f"/api/users/{uid}/test-text")).json() == {"outcome": "sent", "error": ""}
    assert sent_to(twilio) == ["+15705559999"]
    me = next(u for u in (await client.get("/api/users")).json() if u["id"] == uid)
    assert me["last_text"]["outcome"] == "sent" and me["last_text"]["purpose"] == "test"
    r = await client.put(f"/api/users/{uid}", json={"phone": ""})        # removing the number stops texts
    assert r.json()["sms_level"] == "off"


async def test_platform_sms_settings(session, client):
    await world(session)
    await login(client, "admin@twgsecurity.com")
    t = await session.scalar(select(Tenant).where(Tenant.name == "TWG Security"))
    from app.permissions import PLATFORM_PERMISSIONS  # noqa: F401 - admins of the platform company hold them
    r = await client.put("/api/platform/settings/sms", json={"enabled": True, "sid": SID})
    assert r.status_code == 400
    r = await client.put("/api/platform/settings/sms", json={"enabled": True, "sid": "bad", "token": "x", "sender": "+15705550000"})
    assert r.status_code == 400 and "AC" in r.json()["detail"]
    r = await client.put("/api/platform/settings/sms", json={"enabled": True, "sid": SID, "token": "tok-secret",
                                                             "sender": "(570) 555-0000", "link_hours": 12})
    assert r.status_code == 200, r.text
    s = r.json()["sms"]
    assert s["sender"] == "+15705550000" and s["token_set"] and "tok-secret" not in r.text and s["link_hours"] == 12
    assert platform_settings.current().sms_ready
    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "platform.settings").order_by(AuditLog.id.desc()))
    assert "tok-secret" not in str(audit.detail) and t.id

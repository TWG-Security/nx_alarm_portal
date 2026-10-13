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
    monkeypatch.setattr(sms, "WATCH", False)                # the delivery follow-up has its own tests
    yield
    sms.reset()


async def texts_on(session, **kw):
    row = await platform_settings.get_row(session)
    row.sms_enabled, row.twilio_sid, row.twilio_token_enc, row.twilio_from = True, SID, encrypt("tok-secret"), "+15705550000"
    row.portal_url = "https://alarmportal.twgsecurity.net"
    row.sms_pictures = False                       # the picture tests switch it on
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
        "all": mk(t.id, "all@twgsecurity.com", phone="+15705551111", sms_level="alarm", sms_consent="yes"),
        "crit": mk(t.id, "crit@twgsecurity.com", phone="+15705552222", sms_level="critical", sms_consent="yes"),
        "off": mk(t.id, "off@twgsecurity.com", phone="+15705553333", sms_level="off", sms_consent="yes"),
        "gone": mk(t.id, "gone@twgsecurity.com", phone="+15705554444", sms_level="alarm", is_active=False, sms_consent="yes"),
        "acme": mk(other.id, "x@acme.test", phone="+15705555555", sms_level="alarm", sms_consent="yes"),
        "pending": mk(t.id, "pending@twgsecurity.com", phone="+15705556666", sms_level="alarm", sms_consent="pending"),
        "stopped": mk(t.id, "stopped@twgsecurity.com", phone="+15705557777", sms_level="alarm", sms_consent="stopped"),
    }
    session.add_all(people.values())
    await session.commit()
    site = await make_site(session, t, name="TWG Security Office")
    site.timezone, site.address = "America/New_York", "100 Test Rd, Harrisburg, PA"
    await session.commit()
    return t, site, people


n = 0


async def make_alarm(session, site, priority=2, caption="Panic button", **kw):
    global n
    n += 1
    a = Alarm(tenant_id=site.tenant_id, site_id=site.id, event_key=f"k{n}", event_type="softTrigger", priority=priority,
              caption=caption, source_name="Front Door Cam", device_id="dev-1",
              event_ts_ms=kw.pop("event_ts_ms", 1_760_282_602_000), **kw)
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
    assert text.split("\n") == [
        "TWG CRITICAL: Person - loitering at gate",
        "TWG Security Office",
        "100 Test Rd, Harrisburg, PA",
        "Front Door Cam, 11:23:22 AM EDT",
        "Directions: https://www.google.com/maps/dir/?api=1&destination=40.00000,-75.00000",
        "View alarm: https://alarmportal.twgsecurity.net/a/" + "x" * 24,
        "Reply STOP to opt out.",
    ]
    site.lat = site.lng = None
    assert sms.directions_url(site) == "https://www.google.com/maps/dir/?api=1&destination=100%20Test%20Rd,%20Harrisburg,%20PA"
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
    assert sent_to(twilio) == ["+15705551111"]   # not critical-only, off, disabled, another company, not opted in, STOPped
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
    link = body_of(twilio.calls[0]).split("View alarm: ")[1].split("\n")[0]
    token = link.rsplit("/", 1)[-1]
    assert link.startswith("https://alarmportal.twgsecurity.net/a/") and len(token) == 24
    r = await client.get(f"/a/{token}")
    assert r.status_code == 200 and "Panic button" in r.text and "TWG Security Office" in r.text
    assert "google.com/maps/dir/?api=1&amp;destination=40.00000,-75.00000" in r.text      # Directions button
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
    p["admin"].sms_consent = "yes"
    session.add(p["admin"])
    await session.commit()
    r = await client.put(f"/api/users/{uid}", json={"sms_level": "alarm"})
    assert r.status_code == 400 and "mobile number" in r.json()["detail"]
    r = await client.put(f"/api/users/{uid}", json={"phone": "570.555.9999", "sms_level": "critical"})
    assert r.json()["phone"] == "+15705559999" and r.json()["sms_level"] == "critical"
    assert (await client.put(f"/api/users/{uid}", json={"phone": "12345"})).status_code == 400
    r = await client.post("/api/users", json={"email": "night@twgsecurity.com", "password": "A-long-password-1!",
                                             "phone": "(570) 555-7777", "sms_level": "alarm"})
    assert r.json()["phone"] == "+15705557777"

    # A new number is asked again; texts were off, so nobody was asked yet.
    me = next(u for u in (await client.get("/api/users")).json() if u["id"] == uid)
    assert me["sms_consent"] == "none"
    assert (await client.post(f"/api/users/{uid}/test-text")).status_code == 400          # not opted in
    await session.execute(User.__table__.update().where(User.id == uid).values(sms_consent="yes"))
    await session.commit()
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


# ---------------------------------------------------------------- consent: opt in with YES, out with STOP
def signed(url: str, params: dict) -> dict:
    return {"X-Twilio-Signature": _sign(url, params)}


def _sign(url, params):
    import base64
    import hashlib
    import hmac
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(b"tok-secret", data.encode(), hashlib.sha1).digest()).decode()


async def reply(client, frm, body):
    params = {"From": frm, "To": "+15705550000", "Body": body, "MessageSid": "SMin" + body[:3], "AccountSid": SID}
    url = "https://alarmportal.twgsecurity.net/twilio/sms"
    return await client.post("/twilio/sms", data=params, headers=signed(url, params))


@respx.mock
async def test_new_number_asks_for_yes_then_texts(session, client):
    _, site, p = await world(session)
    await texts_on(session, sms_help_contact="support@twgsecurity.com")
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMo"}))
    await login(client, "admin@twgsecurity.com")
    uid = p["admin"].id
    r = await client.put(f"/api/users/{uid}", json={"phone": "570-555-8888", "sms_level": "alarm"})
    assert r.json()["sms_consent"] == "pending"
    await sms.drain()
    ask = body_of(twilio.calls.last)
    assert sent_to(twilio) == ["+15705558888"] and "Reply YES to sign up" in ask and "STOP to opt out at any time" in ask
    assert "Msg & data rates may apply" in ask and ask.endswith("Terms: https://alarmportal.twgsecurity.net/sms-terms")
    assert "for TWG Security" in ask

    # Waiting for YES: alarms don't text them.
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    assert "+15705558888" not in sent_to(twilio)[1:]

    # They reply "Yes!": opted in, a confirmation, audit-logged; alarms now text them.
    r = await reply(client, "+15705558888", "Yes!")
    assert r.status_code == 200 and "<Response>" in r.text
    await sms.drain()
    assert "you're signed up" in body_of(twilio.calls.last) and sent_to(twilio)[-1] == "+15705558888"
    u = await session.get(User, uid)
    await session.refresh(u)
    assert u.sms_consent == "yes" and u.sms_consent_at is not None
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "sms.opted_in")) is not None
    n = len(twilio.calls)
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    assert "+15705558888" in sent_to(twilio)[n:]

    # HELP: the help text with the contact. STOP: opted out, no reply from us (Twilio sends its own), no more alarms.
    await reply(client, "+15705558888", "help")
    await sms.drain()
    assert "Help: support@twgsecurity.com" in body_of(twilio.calls.last)
    n = len(twilio.calls)
    await reply(client, "+15705558888", "STOP")
    await sms.drain()
    assert len(twilio.calls) == n
    await session.refresh(u)
    assert u.sms_consent == "stopped"
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "sms.opted_out")) is not None
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    assert "+15705558888" not in sent_to(twilio)[n:]
    r = await client.post(f"/api/users/{uid}/opt-in")
    assert r.status_code == 400 and "STOP" in r.json()["detail"]
    log = (await session.scalars(select(SmsLog).where(SmsLog.purpose == "inbound").order_by(SmsLog.id))).all()
    assert [x.outcome for x in log] == ["yes", "help", "stop"]


async def test_replies_need_twilios_signature(session, client):
    await world(session)
    await texts_on(session)
    params = {"From": "+15705556666", "Body": "YES", "MessageSid": "SMx"}
    r = await client.post("/twilio/sms", data=params, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403
    u = await session.scalar(select(User).where(User.email == "pending@twgsecurity.com"))
    assert u.sms_consent == "pending"
    row = await session.get(PlatformSettings, 1)
    await session.refresh(row)
    assert "signature didn't match" in row.sms_last_error


@respx.mock
async def test_twilio_21610_marks_them_stopped(session):
    _, site, p = await world(session)
    await texts_on(session)
    respx.post(TWILIO).mock(return_value=httpx.Response(400, json={"code": 21610, "message": "Attempt to send to unsubscribed recipient"}))
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    u = await session.get(User, p["all"].id)
    await session.refresh(u)
    assert u.sms_consent == "stopped"


async def test_terms_page_is_public(client, session):
    await world(session)
    r = await client.get("/sms-terms")
    assert r.status_code == 200
    for words in ("Reply <b>STOP</b>", "Reply <b>HELP</b>", "Message and data rates may apply", "not sold",
                  "Reply YES", "Message frequency varies"):
        assert words in r.text, words
    assert "Alarm text message terms" in (await client.get("/login")).text


# ---------------------------------------------------------------- the picture (MMS)
def jpeg(w=640, h=360):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


def nx_picture(ts):
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices/dev-1/image").mock(return_value=httpx.Response(200, content=jpeg(),
                                                                                  headers={"content-type": "image/jpeg"}))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks").mock(return_value=httpx.Response(200, json=[
        {"id": "{t1}", "objectTypeId": "nx.base.Person", "startTimeMs": ts - 1000, "endTimeMs": ts + 1000}]))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks/t1/objectMetadata").mock(return_value=httpx.Response(200, json=[
        {"timestampMs": ts, "durationMs": 0, "boundingBox": "0.25,0.25,0.5x0.5"}]))


@respx.mock
async def test_alarm_text_carries_a_picture_with_the_boxes(session, client):
    import io
    from PIL import Image
    from app.services.poller import manager
    _, site, _ = await world(session)
    await texts_on(session, sms_pictures=True)
    nx_picture(1_760_282_602_000)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMp"}))
    alarm = await make_alarm(session, site)
    sms.notify([alarm])
    await sms.drain()
    sent = parse_qs(twilio.calls.last.request.content.decode())
    media = sent["MediaUrl"][0]
    assert media.startswith("https://alarmportal.twgsecurity.net/media/a/") and media.endswith("/alarm.jpg")
    # Twilio fetches it with the link's token: the frame with an orange box drawn on it.
    r = await client.get(media.replace("https://alarmportal.twgsecurity.net", ""))
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert img.getpixel((160, 200))[0] > 180 and img.getpixel((320, 180)) == img.getpixel((10, 10))   # box edge vs inside
    await manager.shutdown()


@respx.mock
async def test_a_slow_camera_never_holds_the_text_long(session, monkeypatch):
    _, site, _ = await world(session)
    await texts_on(session, sms_pictures=True)
    monkeypatch.setattr(sms, "PICTURE_BUDGET_S", 0.4)

    async def hang(request):
        await asyncio.sleep(30)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(side_effect=hang)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMn"}))
    t0 = time.perf_counter()
    sms.notify([await make_alarm(session, site)])
    while not twilio.calls and time.perf_counter() - t0 < 3:
        await asyncio.sleep(0.02)
    took = time.perf_counter() - t0
    assert twilio.calls and took < 1.0, took
    assert "MediaUrl" not in parse_qs(twilio.calls.last.request.content.decode())
    await sms.drain()                     # let the send log write finish (the abandoned picture is already cancelled)


@respx.mock
async def test_twilio_refusing_the_picture_still_sends_the_text(session):
    _, site, _ = await world(session)
    await texts_on(session, sms_pictures=True)
    nx_picture(1_760_282_602_000)
    twilio = respx.post(TWILIO).mock(side_effect=[
        httpx.Response(400, json={"code": 21620, "message": "Invalid media URL(s)"}), httpx.Response(201, json={"sid": "SMq"})])
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    first, second = (parse_qs(c.request.content.decode()) for c in twilio.calls)
    assert "MediaUrl" in first and "MediaUrl" not in second
    log = await session.scalar(select(SmsLog))
    assert log.outcome == "sent" and "without the picture" in log.error


async def test_background_texts_have_a_time_limit_and_stop_at_shutdown(monkeypatch, caplog):
    monkeypatch.setattr(sms, "TASK_MAX_S", 0.2)
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.sleep(60)
    sms._spawn(stuck())
    await started.wait()
    assert sms.status()["in_flight"] == 1
    t0 = time.perf_counter()
    await sms.drain()
    assert time.perf_counter() - t0 < 1 and sms.status()["in_flight"] == 0
    assert "ran over" in caplog.text
    # Shutdown: waits briefly, then cancels whatever is left (summaries waiting for the window too).
    monkeypatch.setattr(sms, "TASK_MAX_S", 60)
    sms._spawn(stuck())
    sms._spawn(stuck(), sms._flushers, limit_s=600)
    t0 = time.perf_counter()
    await sms.shutdown(grace_s=0.2)
    assert time.perf_counter() - t0 < 2
    await asyncio.sleep(0)
    assert not sms._tasks and not sms._flushers


# ---------------------------------------------------------------- which sites text whom
@respx.mock
async def test_only_these_sites(session, client):
    t, site, p = await world(session)
    other_site = await make_site(session, t, name="Warehouse")
    acme_site = await make_site(session, await session.get(Tenant, p["acme"].tenant_id), name="Acme HQ")
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMs"}))
    await login(client, "admin@twgsecurity.com")
    uid = p["all"].id

    r = await client.put(f"/api/users/{uid}", json={"sms_sites": [other_site.id]})
    assert r.status_code == 200 and r.json()["sms_sites"] == [other_site.id]
    sms.notify([await make_alarm(session, site)])                 # TWG Security Office: not theirs now
    await sms.drain()
    assert "+15705551111" not in sent_to(twilio)
    sms.notify([await make_alarm(session, other_site)])           # Warehouse: theirs
    await sms.drain()
    assert sent_to(twilio).count("+15705551111") == 1

    assert (await client.put(f"/api/users/{uid}", json={"sms_sites": []})).status_code == 400            # none picked
    assert (await client.put(f"/api/users/{uid}", json={"sms_sites": [acme_site.id]})).status_code == 400  # another company's
    r = await client.put(f"/api/users/{uid}", json={"sms_all_sites": True})
    assert r.json()["sms_sites"] is None
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    assert sent_to(twilio).count("+15705551111") == 2

    r = await client.post("/api/users", json={"email": "gate@twgsecurity.com", "password": "A-long-password-1!",
                                             "phone": "5705550199", "sms_level": "alarm", "sms_sites": [site.id]})
    assert r.status_code == 200 and r.json()["sms_sites"] == [site.id]



# ---------------------------------------------------------------- delivery follow-up (production 2026-10-12, alarm 1880)
@respx.mock
async def test_a_picture_that_sinks_the_mms_is_resent_without_it(session, monkeypatch):
    """Twilio accepted an MMS, then failed it (63019: couldn't download the picture): the words go again alone."""
    _, site, _ = await world(session)
    await texts_on(session, sms_pictures=True)
    monkeypatch.setattr(sms, "WATCH", True)
    monkeypatch.setattr(sms, "WATCH_AT_S", (0.05, 0.1))
    nx_picture(1_760_282_602_000)
    sids = iter(["SMpic", "SMtext"])
    twilio = respx.post(TWILIO).mock(side_effect=lambda req: httpx.Response(201, json={"sid": next(sids)}))
    respx.get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/SMpic.json").mock(
        return_value=httpx.Response(200, json={"sid": "SMpic", "status": "failed", "error_code": 63019, "num_media": "1"}))
    respx.get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/SMtext.json").mock(
        return_value=httpx.Response(200, json={"sid": "SMtext", "status": "delivered", "error_code": None}))
    alarm = await make_alarm(session, site)
    sms.notify([alarm])
    await sms.drain()
    for _ in range(100):
        if not sms._watchers:
            break
        await asyncio.sleep(0.05)
    first, second = (parse_qs(c.request.content.decode()) for c in twilio.calls)
    assert "MediaUrl" in first and "MediaUrl" not in second and first["Body"] == second["Body"]
    rows = (await session.scalars(select(SmsLog).order_by(SmsLog.id))).all()
    assert [(r.outcome, r.twilio_sid) for r in rows] == [("failed", "SMpic"), ("delivered", "SMtext")]
    assert "63019" in rows[0].error and "resent without the picture" in rows[1].error
    row = await session.get(PlatformSettings, 1)
    await session.refresh(row)
    assert "not delivered" in row.sms_last_error


@respx.mock
async def test_carrier_refusal_is_recorded(session, monkeypatch):
    _, site, _ = await world(session)
    await texts_on(session)
    monkeypatch.setattr(sms, "WATCH", True)
    monkeypatch.setattr(sms, "WATCH_AT_S", (0.05,))
    respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMc"}))
    respx.get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/SMc.json").mock(
        return_value=httpx.Response(200, json={"status": "undelivered", "error_code": 30007}))
    sms.notify([await make_alarm(session, site)])
    await sms.drain()
    for _ in range(100):
        if not sms._watchers:
            break
        await asyncio.sleep(0.05)
    log = await session.scalar(select(SmsLog))
    assert log.outcome == "undelivered" and "30007" in log.error and "spam" in log.error
    assert len(respx.calls) == 2                                   # no resend: there was no picture to blame


# ---------------------------------------------------------------- confirmed by an operator (mobile guards)
async def guards(session, t, site):
    """guard: only confirmed texts; both: both kinds; mike: confirmed texts too, but he's the one confirming."""
    mk = lambda email, phone, **kw: User(tenant_id=t.id, email=email, display_name=email.split("@")[0],  # noqa: E731
                                         password_hash=hash_password(PASSWORD), role="admin", phone=phone,
                                         sms_consent="yes", **kw)
    g = {"guard": mk("guard@twgsecurity.com", "+15705558001", sms_level="alarm", sms_when="confirmed"),
         "both": mk("both@twgsecurity.com", "+15705558002", sms_level="critical", sms_when="both"),
         "mike": mk("mike@twgsecurity.com", "+15705558003", sms_level="alarm", sms_when="confirmed"),
         "offguard": mk("offguard@twgsecurity.com", "+15705558004", sms_level="off", sms_when="confirmed"),
         "elsewhere": mk("elsewhere@twgsecurity.com", "+15705558005", sms_level="alarm", sms_when="confirmed",
                         sms_sites=[site.id + 1000])}
    session.add_all(g.values())
    await session.commit()
    return g


def nx_ok():
    respx.route(url__startswith=NX).mock(return_value=httpx.Response(200, json={"token": "tok", "id": "bm-1"}))


def now_ms():
    return int(time.time() * 1000)


@respx.mock
async def test_guard_is_texted_only_when_an_operator_confirms(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMc"}))
    await login(client, "mike@twgsecurity.com")

    alarm = await make_alarm(session, site, priority=2, event_ts_ms=now_ms())
    sms.notify([alarm])
    await sms.drain()
    assert sent_to(twilio) == ["+15705551111"]            # goes off: the usual people; no guard (both = critical only)

    r = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "Man in black hoodie at the loading dock",
                                                              "verdict": "real"})
    assert r.status_code == 200
    await sms.drain()
    confirmed = twilio.calls[1:]
    assert sorted(sent_to(twilio)[1:]) == ["+15705558001", "+15705558002"]   # not mike (confirmed it), off, other site
    text = body_of(confirmed[0]).split("\n")
    assert text[0] == "TWG CONFIRMED: Panic button" and text[1] == "TWG Security Office"
    assert text[4].startswith("Confirmed real by mike, ") and text[5] == "Note: Man in black hoodie at the loading dock"
    assert text[6].startswith("Directions: ") and text[7].startswith("View alarm: ") and text[-1] == "Reply STOP to opt out."
    log = (await session.scalars(select(SmsLog).where(SmsLog.purpose == "confirmed"))).all()
    assert len(log) == 2 and all(r.alarm_id == alarm.id and r.outcome == "sent" for r in log)

    # The guard's link shows it confirmed, with the note.
    token = body_of(confirmed[0]).split("View alarm: ")[1].split("\n")[0].rsplit("/", 1)[-1]
    page = (await client.get(f"/a/{token}")).text
    assert "Confirmed real by mike" in page and "Man in black hoodie" in page

    # False alarm / dismissed: no confirmed texts. A confirmed warning does text (any level).
    for verdict in ("false", "dismissed"):
        a = await make_alarm(session, site, priority=2, event_ts_ms=now_ms())
        await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": verdict})
    await sms.drain()
    assert len(twilio.calls) == 3
    warn = await make_alarm(session, site, priority=3, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{warn.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert sorted(sent_to(twilio)[3:]) == ["+15705558001", "+15705558002"]


@respx.mock
async def test_stand_down_when_a_confirmation_is_withdrawn(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMs"}))
    await login(client, "mike@twgsecurity.com")
    a = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    other = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())   # never confirmed
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert len(twilio.calls) == 2

    r = await client.post("/api/alarms/bulk", json={"ids": [a.id, other.id], "verdict": "false", "note": "employee"})
    assert r.status_code == 200
    await sms.drain()
    assert sorted(sent_to(twilio)[2:]) == ["+15705558001", "+15705558002"]   # only the people told it was real
    assert body_of(twilio.calls[2]).startswith("TWG STAND DOWN: Panic button at TWG Security Office is no longer confirmed real. "
                                               "mike marked it a false alarm, ")
    assert len((await session.scalars(select(SmsLog).where(SmsLog.purpose == "stand_down"))).all()) == 2


@respx.mock
async def test_bulk_confirmations_text_once_and_old_alarms_never(session, client):
    t, site, p = await world(session)
    await guards(session, t, site)
    await texts_on(session, sms_max_per_10min=1)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMb"}))
    await login(client, "mike@twgsecurity.com")

    old = await make_alarm(session, site, event_ts_ms=now_ms() - (sms.CONFIRM_MAX_AGE_S + 60) * 1000)
    await client.post(f"/api/alarms/{old.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert twilio.calls.call_count == 0                       # re-marking old alarms never sends guards out

    ids = [(await make_alarm(session, site, event_ts_ms=now_ms())).id for _ in range(3)]
    r = await client.post("/api/alarms/bulk", json={"ids": ids, "verdict": "real"})
    assert len(r.json()["acknowledged"]) == 3
    await sms.drain()
    assert sorted(sent_to(twilio)) == ["+15705558001", "+15705558002"]   # one each, for the newest
    body = body_of(twilio.calls[0])
    assert "(+2 more confirmed alarms, see the portal)" in body
    newest = await session.scalar(select(AlarmLink).where(AlarmLink.alarm_id == max(ids)))
    assert newest is not None

    # Flood control never holds a confirmed text back (limit 1 per window, already used).
    a = await make_alarm(session, site, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert twilio.calls.call_count == 4


@respx.mock
async def test_a_hanging_twilio_never_delays_the_acknowledgement(session, client):
    t, site, p = await world(session)
    await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    hang = asyncio.Event()

    async def slow(request):
        await hang.wait()
        return httpx.Response(201, json={"sid": "SMh"})
    respx.post(TWILIO).mock(side_effect=slow)
    await login(client, "mike@twgsecurity.com")
    a = await make_alarm(session, site, event_ts_ms=now_ms())
    t0 = time.perf_counter()
    r = await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real"})
    took = time.perf_counter() - t0
    assert r.status_code == 200 and took < 0.5, took
    await asyncio.sleep(0.2)
    assert sms._tasks
    hang.set()
    await sms.drain()
    assert len((await session.scalars(select(SmsLog).where(SmsLog.purpose == "confirmed"))).all()) == 2


async def test_users_api_when_to_text(session, client):
    t, site, p = await world(session)
    await login(client, "admin@twgsecurity.com")
    uid = p["all"].id
    assert (await client.get("/api/users")).json()[1]["sms_when"] == "raised"          # everyone so far
    r = await client.put(f"/api/users/{uid}", json={"sms_when": "confirmed"})
    assert r.status_code == 200 and r.json()["sms_when"] == "confirmed"
    assert (await client.put(f"/api/users/{uid}", json={"sms_when": "later"})).status_code == 422
    r = await client.post("/api/users", json={"email": "patrol@twgsecurity.com", "password": "A-long-password-1!",
                                             "phone": "5705550199", "sms_level": "alarm", "sms_when": "both"})
    assert r.status_code == 200 and r.json()["sms_when"] == "both"
    audit = (await session.scalars(select(AuditLog).where(AuditLog.action == "user.updated"))).all()
    assert any("sms_when" in (a.detail or {}).get("fields", []) for a in audit)

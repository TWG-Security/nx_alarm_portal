"""Mute a site or one camera for a few minutes (app/services/mute.py): muted alarms are listed but silent."""
import time

from sqlalchemy import select

from app.models import Alarm, AuditLog, User
from app.security import hash_password
from app.services import mute
from app.services.poller import ingest
from app.services.serialize import alarm_dict
from tests.conftest import PASSWORD, login, make_site, nx_row


def now_ms() -> int:
    return int(time.time() * 1000)


def entry(minutes=5, **kw):
    t = now_ms()
    return {"until_ms": t + minutes * 60_000, "at_ms": t, "by": "op", "user_id": 1, "name": "", **kw}


def test_active_ignores_expired_entries():
    t = now_ms()
    m = {"site": {"until_ms": t - 1}, "cameras": {"a": {"until_ms": t + 60_000}, "b": {"until_ms": t - 5}}}
    assert mute.active(m, t) == {"cameras": {"a": {"until_ms": t + 60_000}}}
    assert mute.is_muted(m, "a", t) and not mute.is_muted(m, "b", t) and not mute.is_muted(m, "", t)
    assert mute.clear(m, "a", t) is None
    assert not mute.applies(1, "security") and not mute.applies(2, "system") and mute.applies(2, "security")


async def test_muted_site_alarms_are_stored_open_and_marked(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.mute = {"site": entry()}
    await session.commit()
    t = now_ms()
    created = await ingest(session, site, [
        nx_row(t, action_id="intrusion", type_="analytics", device="cam-1"),                 # alarm: muted
        nx_row(t + 1, action_id="panic", ack=True, device="cam-1", caption="Panic"),         # critical: never muted
        nx_row(t + 2, action_id="store", type_="storageIssue", device="", caption="Disk"),   # system: never muted
    ], {}, now=t)
    await session.commit()
    by = {a.nx_action_id: a for a in created}
    assert set(by) == {"intrusion", "panic", "store"}                       # all raised: nothing hidden
    assert all(a.state == "new" for a in created)
    assert (by["intrusion"].muted, by["panic"].muted, by["store"].muted) == (True, False, False)
    assert alarm_dict(by["intrusion"])["muted"] is True and alarm_dict(by["panic"])["muted"] is False
    row = await session.scalar(select(AuditLog).where(AuditLog.alarm_id == by["intrusion"].id, AuditLog.action == "alarm.received"))
    assert row.detail.get("muted") is True


async def test_camera_mute_only_that_camera_and_expiry(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.mute = {"cameras": {"cam-1": entry()}}
    await session.commit()
    t = now_ms()
    created = await ingest(session, site, [nx_row(t, action_id="a", type_="analytics", device="cam-1"),
                                           nx_row(t, action_id="b", type_="analytics", device="cam-2")], {}, now=t)
    assert {a.nx_action_id: a.muted for a in created} == {"a": True, "b": False}
    # Ten minutes later the 5-minute mute is over.
    later = t + 10 * 60_000
    created = await ingest(session, site, [nx_row(later, action_id="c", type_="analytics", device="cam-1")], {}, now=later)
    assert created[0].muted is False


async def test_muted_alarms_are_not_texted_or_pushed(session, admin, monkeypatch):
    from app import apns, sms
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.mute = {"site": entry()}
    await session.commit()
    t = now_ms()
    created = await ingest(session, site, [nx_row(t, action_id="m", type_="analytics"),
                                           nx_row(t, action_id="p", ack=True, caption="Panic")], {}, now=t)
    await session.commit()
    spawned = []
    monkeypatch.setattr(sms, "_spawn", lambda coro: (spawned.append("sms"), coro.close()))
    monkeypatch.setattr(apns, "_spawn", lambda coro: (spawned.append("apns"), coro.close()))
    monkeypatch.setattr(apns, "configured", lambda: True)

    class Ready:
        sms_ready = True
    monkeypatch.setattr(sms.platform_settings, "current", lambda: Ready())
    sms.notify(created)
    apns.notify(created)
    assert spawned == ["sms", "apns"]                     # only the critical one, once each


async def test_operator_mutes_and_unmutes(client, session, admin):
    tenant, _ = admin
    op = User(tenant_id=tenant.id, email="op@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    site = await make_site(session, tenant)
    site.camera_places = {"cam-1": {"name": "Driveway", "lat": 40, "lng": -75, "heading": 0, "fov": 60, "range_m": 40}}
    alarm = Alarm(tenant_id=tenant.id, site_id=site.id, event_key="k1", state="new", category="security", priority=2,
                  event_type="analytics", caption="Line crossed", device_id="cam-1", event_ts_ms=now_ms(), raw={})
    session.add(alarm)
    await session.commit()
    await login(client, op.email)

    r = await client.post(f"/api/sites/{site.id}/mute", json={"minutes": 5, "alarm_id": alarm.id})
    assert r.status_code == 200, r.text
    m = r.json()["mute"]
    assert m["site"]["by"] and abs(m["site"]["until_ms"] - now_ms() - 5 * 60_000) < 5_000
    r = await client.post(f"/api/sites/{site.id}/mute", json={"minutes": 2, "device_id": "cam-1"})
    assert r.json()["mute"]["cameras"]["cam-1"]["name"] == "Driveway"
    assert (await client.post(f"/api/sites/{site.id}/mute", json={"minutes": 2, "device_id": "nope"})).status_code == 404
    assert (await client.post(f"/api/sites/{site.id}/mute", json={"minutes": 0})).status_code == 422
    assert (await client.post(f"/api/sites/{site.id}/mute", json={"minutes": 241})).status_code == 422

    r = await client.delete(f"/api/sites/{site.id}/mute")
    assert r.status_code == 200 and "site" not in r.json()["mute"] and "cam-1" in r.json()["mute"]["cameras"]
    r = await client.delete(f"/api/sites/{site.id}/mute", params={"device_id": "cam-1"})
    assert r.json()["mute"] == {}

    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.site_id == site.id).order_by(AuditLog.id))).all()
    assert actions == ["site.muted", "site.muted", "site.unmuted", "site.unmuted"]
    tl = (await client.get(f"/api/alarms/{alarm.id}/timeline")).json()
    rows = tl if isinstance(tl, list) else tl.get("rows") or tl.get("timeline") or []
    assert any("Muted the site for 5 min" in (x.get("what") or "") for x in rows)
    assert (await client.get(f"/api/sites/{site.id}")).json()["id"] == site.id

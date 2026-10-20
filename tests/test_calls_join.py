"""Alarms during an active call join it (services/calls.py join_active_call). The user, 2026-10-19: "when an active
call is going on, put all subsequent alarms into that call", option "Join the call"."""
import httpx
import respx
from sqlalchemy import select

from app import sms
from app.models import Alarm, AuditLog, SmsLog
from app.services.poller import ingest
from tests.conftest import login, make_site, nx_row
from tests.test_dispatch import track
from tests.test_sms import TWILIO, body_of, fresh_sms, guards, make_alarm, now_ms, nx_ok, sent_to, texts_on, world  # noqa: F401

n = 0


async def incoming(session, site, **kw):
    """A new NX row through ingest, as the poller / push would; returns what's raised."""
    global n
    n += 1
    raised = await ingest(session, site, [nx_row(now_ms() + n, action_id=f"j{n}", **kw)], {})
    await session.commit()
    return raised


@respx.mock
async def test_new_alarms_at_the_site_join_its_active_call(client, session):
    t, site, p = await world(session)
    nx_ok()
    call = await make_alarm(session, site, event_ts_ms=now_ms())
    await login(client, "admin@twgsecurity.com")
    assert (await client.post(f"/api/alarms/{call.id}/ack", json={"verdict": "real"})).json()["call_state"] == "active"
    cid = call.id

    # An alarm-level security event: joins quietly (no feed card, no acknowledgement of its own)
    raised = await incoming(session, site, type_="analytics", caption="Back door intrusion")
    assert raised == []
    joined = await session.scalar(select(Alarm).where(Alarm.caption == "Back door intrusion"))
    assert (joined.call_id, joined.state) == (cid, "acknowledged") and joined.ack_note.startswith("Joined the active call")
    assert [a["id"] for a in (await client.get("/api/alarms?state=open")).json()] == []

    # A critical one: linked to the call, and still raised on its own
    raised = await incoming(session, site, type_="softTrigger", caption="Panic button")
    assert len(raised) == 1 and raised[0].call_id == cid and raised[0].state == "new"

    # The call shows them: Active calls count + newest, timeline rows, the joined alarm's own timeline
    [c] = (await client.get("/api/alarms?state=active")).json()
    assert c["id"] == cid and c["joined_count"] == 2 and c["joined_newest"]["id"] == raised[0].id
    rows = (await client.get(f"/api/alarms/{cid}/timeline")).json()
    during = [r for r in rows if r["what"] == "Alarm during the call"]
    assert [r["detail"].split(" · ")[0] for r in during] == ["Back door intrusion", raised[0].caption]
    assert all(r["kind"] == "joined" for r in during) and "also raised on its own" in during[1]["detail"]
    own = (await client.get(f"/api/alarms/{joined.id}/timeline")).json()
    assert any(r["what"] == "Joined the active call" for r in own)
    assert (await client.get(f"/api/alarms/{joined.id}")).json()["call_id"] == cid

    # Another site isn't part of it; after End call nothing joins
    other = await make_site(session, t, name="Other Site")
    assert len(await incoming(session, other, type_="analytics", caption="Elsewhere")) == 1
    assert (await client.post(f"/api/alarms/{cid}/end-call", json={"note": "done"})).status_code == 200
    raised = await incoming(session, site, type_="analytics", caption="After the call")
    assert len(raised) == 1 and raised[0].call_id is None and raised[0].state == "new"


@respx.mock
async def test_a_joined_alarm_chimes_the_call_and_tells_the_dispatched_guard(client, session):
    from app.services.bus import bus
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMj"}))
    tr = await track(session, t.id, "Guard 1", 400, g["guard"])
    call = await make_alarm(session, site, event_ts_ms=now_ms())
    await login(client, "mike@twgsecurity.com")
    await client.post(f"/api/alarms/{call.id}/ack", json={"verdict": "real"})
    await client.post(f"/api/alarms/{call.id}/dispatch", json={"tracker_id": tr.id})
    await sms.drain()
    before = len(twilio.calls)
    sub = bus.subscribe({t.id})
    await incoming(session, site, type_="analytics", caption="Gate line crossed")
    await sms.drain()
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    bus.unsubscribe(sub)
    # The call's timeline changed as a guard-style update (update chime + Active calls card flash on screens)
    assert ("alarm.timeline", {"alarm_id": call.id, "guard": True}) in events
    assert not any(e == "alarm.new" for e, _ in events)                          # no new feed card
    texts = [(c, body_of(c)) for c in twilio.calls[before:]]
    upd = [b for c, b in texts if b.startswith("TWG UPDATE")]
    assert sent_to(twilio)[before:] == ["+15705558001"] and upd and "Gate line crossed" in upd[0] and "same call" in upd[0]
    row = await session.scalar(select(SmsLog).where(SmsLog.purpose == "joined"))
    assert row is not None and row.user_id == g["guard"].id
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "alarm.joined_call", AuditLog.alarm_id == call.id))


from tests.test_mobile import apns_on, apns_route, phone_for, sent  # noqa: E402,F401


@respx.mock
async def test_the_dispatched_guards_app_gets_a_call_update(client, session, apns_on):  # noqa: F811
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMk"}))
    route = apns_route()
    tr = await track(session, t.id, "Guard 1", 400, g["guard"])
    await phone_for(client, "guard@twgsecurity.com", token="9" * 64)
    call = await make_alarm(session, site, event_ts_ms=now_ms())
    await login(client, "mike@twgsecurity.com")
    await client.post(f"/api/alarms/{call.id}/ack", json={"verdict": "real"})
    await client.post(f"/api/alarms/{call.id}/dispatch", json={"tracker_id": tr.id})
    await sms.drain()
    await incoming(session, site, type_="analytics", caption="Gate line crossed")
    await sms.drain()
    upd = [(h, b) for tok, h, b in sent(route) if b["type"] == "call_update"]
    assert len(upd) == 1
    h, b = upd[0]
    assert b["alarm_id"] == call.id and b["joined_alarm_id"] and b["aps"]["alert"]["title"] == f"UPDATE · {site.name}"
    assert b["aps"]["alert"]["body"].startswith("Gate line crossed") and b["aps"]["thread-id"] == f"alarm-{call.id}"
    assert h["apns-collapse-id"] == f"update-{b['joined_alarm_id']}"
    assert b["aps"]["sound"] == "twg-alarm.wav"                           # the joined alarm's level

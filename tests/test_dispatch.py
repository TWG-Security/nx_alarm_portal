"""Dispatch (closest guard, Dispatch button, automatic on confirm), guards sharing their location from a phone
browser, routers going offline, position history by time range, and the open-alarm queue order."""
import re
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app import sms
from app.models import AlarmNote, AuditLog, AuthEvent, SmsLog, Tenant, Tracker, TrackerPoint, User, utcnow
from app.routers import guards as guards_router
from app.services import trackers as svc
from tests.conftest import login
from tests.test_sms import TWILIO, body_of, fresh_sms, guards, make_alarm, now_ms, nx_ok, sent_to, texts_on, world  # noqa: F401

SITE = (40.0, -75.0)          # tests.conftest.make_site


def north(m: float) -> tuple[float, float]:
    return SITE[0] + m / 111_320, SITE[1]


async def track(session, tenant_id, name, metres_north, user=None, *, age_s=30, online=True, source="peplink"):
    lat, lng = north(metres_north)
    t = Tracker(tenant_id=tenant_id, name=name, kind="guard", source=source, user_id=user.id if user else None,
                lat=lat, lng=lng, fix_at=utcnow() - timedelta(seconds=age_s), online=online)
    session.add(t)
    await session.commit()
    return t


@respx.mock
async def test_closest_guard_is_listed_first_and_dispatched(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMd"}))
    near_nobody = await track(session, t.id, "Unlinked car", 50)                 # closest, but nobody to text
    offline = await track(session, t.id, "Both's router", 100, g["both"], online=False)
    guard = await track(session, t.id, "Guard 1", 400, g["guard"])
    stale = await track(session, t.id, "Old fix", 20, g["elsewhere"], age_s=3600)
    await login(client, "mike@twgsecurity.com")
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())

    r = (await client.get(f"/api/alarms/{alarm.id}/guards")).json()
    assert [c["name"] for c in r["guards"]] == ["Unlinked car", "Guard 1", "Old fix", "Both's router"]
    by = {c["name"]: c for c in r["guards"]}
    assert by["Guard 1"]["closest"] and not by["Unlinked car"]["closest"]
    assert by["Unlinked car"]["reason"].startswith("No person linked") and not by["Old fix"]["usable"]
    assert by["Guard 1"]["distance"] == "0.2 mi" and r["from"] == "site" and r["texts_ready"]

    assert (await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_id": near_nobody.id})).status_code == 400
    res = await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_id": guard.id})
    assert res.status_code == 200, res.text
    assert res.json()["sent"][0]["to"] == "Guard 1" and res.json()["sent"][0]["distance"] == "0.2 mi" and not res.json()["failed"]
    await sms.drain()
    assert sent_to(twilio) == ["+15705558001"]
    text = body_of(twilio.calls[0]).split("\n")
    assert text[0] == "TWG DISPATCH: Panic button" and text[4].startswith("Go now. Dispatched by mike, ")
    assert text[4].endswith("(you're 0.2 mi away)") and sms.SHARE_LABEL not in text      # a router guard: no share link
    note = (await session.scalars(select(AlarmNote).where(AlarmNote.alarm_id == alarm.id))).one()
    assert note.text == "Dispatched Guard 1 (guard), 0.2 mi away"
    assert (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.dispatched"))).one().detail["guard"] == "Guard 1"
    r = (await client.get(f"/api/alarms/{alarm.id}/guards")).json()
    assert [(d["guard"], d["to"], d["text"], d["status"]) for d in r["dispatched"]] == [("Guard 1", "guard", "sent", "dispatched")]

    # Another company can't see or dispatch them.
    acme_user = p["acme"]
    await login(client, acme_user.email)
    assert (await client.get(f"/api/alarms/{alarm.id}/guards")).status_code == 404
    assert (await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_id": guard.id})).status_code == 404


@respx.mock
async def test_confirming_texts_only_the_closest_guard(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMa"}))
    await track(session, t.id, "Guard 1", 900, g["guard"])
    await track(session, t.id, "Guard 2", 120, g["both"])                       # closest
    await login(client, "mike@twgsecurity.com")

    a = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real", "note": "Two people at the fence"})
    await sms.drain()
    assert sent_to(twilio) == ["+15705558002"]                                  # only the closest guard
    text = body_of(twilio.calls[0]).split("\n")
    assert text[0] == "TWG DISPATCH: Panic button" and text[4].startswith("Go now. Confirmed real by mike")
    assert text[5] == "Note: Two people at the fence"
    assert "automatically" in (await session.scalars(select(AlarmNote).where(AlarmNote.alarm_id == a.id))).one().text

    # Withdrawn: the dispatched guard gets STAND DOWN.
    await client.post("/api/alarms/bulk", json={"ids": [a.id], "verdict": "false"})
    await sms.drain()
    assert sent_to(twilio)[1:] == ["+15705558002"] and body_of(twilio.calls[1]).startswith("TWG STAND DOWN")

    # Nobody with a usable position: every guard set to confirmed texts gets the confirmed text (never nobody).
    for tr in (await session.scalars(select(Tracker))).all():
        tr.fix_at = utcnow() - timedelta(hours=2)
    await session.commit()
    b = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{b.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert sorted(sent_to(twilio)[2:]) == ["+15705558001", "+15705558002"]
    assert all(body_of(c).startswith("TWG CONFIRMED") for c in twilio.calls[2:])

    # Auto-dispatch off (Sites page): confirmed texts as before.
    for tr in (await session.scalars(select(Tracker))).all():
        tr.fix_at = utcnow()
    tenant = await session.get(Tenant, t.id)
    tenant.settings = {"auto_dispatch": False}
    await session.commit()
    c = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{c.id}/ack", json={"verdict": "real"})
    await sms.drain()
    assert sorted(sent_to(twilio)[4:]) == ["+15705558001", "+15705558002"]


@respx.mock
async def test_guard_shares_location_from_a_phone_browser(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMs"}))
    await login(client, "admin@twgsecurity.com")

    r = await client.post("/api/tracking/phones", json={"user_id": g["guard"].id, "kind": "guard"})
    assert r.status_code == 200, r.text
    share = r.json()["share"]
    assert share["texted"] and share["link"].startswith("https://alarmportal.twgsecurity.net/g/")
    assert (await client.post("/api/tracking/phones", json={"user_id": g["guard"].id})).status_code == 409
    await sms.drain()
    assert sent_to(twilio) == ["+15705558001"] and share["link"] in body_of(twilio.calls[0])
    tracker = next(x for x in r.json()["trackers"] if x["source"] == "phone")
    assert tracker["name"] == "guard" and tracker["user_id"] == g["guard"].id and tracker["online"] is None
    token = share["link"].rstrip("/").rsplit("/", 1)[-1]

    client.cookies.clear()                                         # the guard's phone: not signed in
    page = await client.get(f"/g/{token}")
    assert page.status_code == 200 and "Start sharing" in page.text and page.headers["cache-control"] == "no-store"
    lat, lng = north(300)
    assert (await client.post(f"/api/g/{token}/pos", json={"lat": lat, "lng": lng, "accuracy": 8, "speed": 1.2})).json()["taken"]
    assert not (await client.post(f"/api/g/{token}/pos", json={"lat": lat, "lng": lng})).json()["taken"]      # too soon
    svc._phone_at.clear()
    assert not (await client.post(f"/api/g/{token}/pos", json={"lat": lat, "lng": lng, "accuracy": 900})).json()["taken"]   # too vague
    row = await session.get(Tracker, tracker["id"])
    await session.refresh(row)
    assert (round(row.lat, 6), row.online, row.source) == (round(lat, 6), True, "phone")
    assert (await session.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id == row.id))).all()

    # Dispatching a phone guard carries the link again.
    await login(client, "mike@twgsecurity.com")
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    assert (await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_id": row.id})).status_code == 200
    await sms.drain()
    lines = body_of(twilio.calls[-1]).split("\n")
    assert lines[lines.index(sms.SHARE_LABEL) + 1].strip() == share["link"]

    # Stop: offline, last position kept. A wrong token counts as a failed sign-in.
    client.cookies.clear()
    assert (await client.post(f"/api/g/{token}/stop")).status_code == 200
    await session.refresh(row)
    assert row.online is False and row.offline_since is not None and row.lat is not None
    assert (await client.post("/api/g/not-a-token/pos", json={"lat": 1, "lng": 2})).status_code == 404
    assert (await client.get("/g/not-a-token")).status_code == 404
    assert (await session.scalars(select(AuthEvent).where(AuthEvent.kind == "share_link"))).all()

    # A new link replaces the old one; a deactivated person's link stops working.
    await login(client, "admin@twgsecurity.com")
    r = await client.post(f"/api/tracking/trackers/{row.id}/share", json={"renew": True})
    new_token = r.json()["share"]["link"].rstrip("/").rsplit("/", 1)[-1]
    assert new_token != token
    client.cookies.clear()
    assert (await client.get(f"/g/{token}")).status_code == 404 and (await client.get(f"/g/{new_token}/")).status_code == 200
    u = await session.get(User, g["guard"].id)
    u.is_active = False
    await session.commit()
    assert (await client.post(f"/api/g/{new_token}/pos", json={"lat": lat, "lng": lng})).status_code == 404


async def test_a_quiet_phone_page_shows_offline(session):
    t, site, p = await world(session)
    tr = await track(session, t.id, "Phone guard", 10, p["all"], source="phone")
    tr.share_seen_at = utcnow() - timedelta(seconds=svc.PHONE_QUIET_S + 5)
    await session.commit()
    await svc._phones_quiet()
    await session.refresh(tr)
    assert tr.online is False and tr.lat is not None


async def test_history_by_time_range_from_the_server(session, client, monkeypatch):
    t, site, p = await world(session)
    tr = await track(session, t.id, "Guard 1", 0)
    start = utcnow() - timedelta(hours=10)
    session.add_all([TrackerPoint(tracker_id=tr.id, ts=start + timedelta(minutes=i), lat=40 + i / 1e5, lng=-75, speed=1.0)
                     for i in range(600)])
    await session.commit()
    await login(client, "admin@twgsecurity.com")
    frm, to = start + timedelta(minutes=100), start + timedelta(minutes=199, seconds=30)
    r = await client.get(f"/api/trackers/{tr.id}/trail", params={"start": frm.isoformat(), "end": to.isoformat()})
    pts = r.json()["points"]
    assert r.json()["total"] == 100 and len(pts) == 100 and pts[0][0] == pytest.approx(40.001)
    monkeypatch.setattr(guards_router, "HISTORY_MAX_POINTS", 50)
    r = (await client.get(f"/api/trackers/{tr.id}/trail", params={"start": start.isoformat()})).json()
    assert r["total"] == 600 and len(r["points"]) == 50 and r["points"][-1][0] == pytest.approx(40 + 599 / 1e5)
    assert (await client.get(f"/api/trackers/{tr.id}/trail", params={"start": to.isoformat(), "end": frm.isoformat()})).status_code == 400
    assert svc.TRAIL_KEEP_DAYS == 30


async def test_open_alarms_can_be_listed_oldest_first(session, client):
    t, site, p = await world(session)
    ids = [(await make_alarm(session, site)).id for _ in range(5)]
    await login(client, "admin@twgsecurity.com")
    first = (await client.get("/api/alarms", params={"state": "open", "order": "oldest", "limit": 3})).json()
    assert [a["id"] for a in first] == ids[:3]
    rest = (await client.get("/api/alarms", params={"state": "open", "order": "oldest", "limit": 3, "after_id": ids[2]})).json()
    assert [a["id"] for a in rest] == ids[3:]
    assert [a["id"] for a in (await client.get("/api/alarms", params={"state": "open"})).json()] == ids[::-1]


def test_distances_read_like_a_guard_says_them():
    assert [sms.distance_words(m) for m in (None, 3, 100, 304, 400, 1609.344, 30_000)] == \
        ["", "10 ft", "330 ft", "1000 ft", "0.2 mi", "1.0 mi", "19 mi"]
    assert re.fullmatch(r"\d+ ft", sms.distance_words(250))


async def test_admins_link_a_person_to_a_router_for_dispatch_texts(session, client):
    t, site, p = await world(session)
    tr = await track(session, t.id, "Patrol 1", 10)
    await login(client, "admin@twgsecurity.com")
    r = await client.put(f"/api/tracking/trackers/{tr.id}", json={"user_id": p["all"].id})
    assert r.status_code == 200 and next(x for x in r.json()["trackers"] if x["id"] == tr.id)["user_id"] == p["all"].id
    assert any(x["id"] == p["all"].id and x["phone"] for x in r.json()["people"])
    assert (await client.put(f"/api/tracking/trackers/{tr.id}", json={"user_id": p["acme"].id})).status_code == 400
    r = await client.put(f"/api/tracking/trackers/{tr.id}", json={"user_id": None})
    assert next(x for x in r.json()["trackers"] if x["id"] == tr.id)["user_id"] is None
    r = await client.put("/api/tracking/dispatch", json={"auto": False})
    assert r.json()["auto_dispatch"] is False


@respx.mock
async def test_the_guard_who_confirms_is_dispatched_when_closest(session, client):
    """A guard can confirm an alarm themselves (e.g. on their phone) and still be the one sent: the closest guard
    is texted even if they confirmed it. The CONFIRMED text still never goes to whoever confirmed."""
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMm"}))
    await track(session, t.id, "Mike's truck", 60, g["mike"])                    # mike confirms, and he's closest
    await track(session, t.id, "Guard 1", 900, g["guard"])
    await login(client, "mike@twgsecurity.com")
    a = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real"})
    await sms.drain()
    texted = {to: body_of(c) for to, c in zip(sent_to(twilio), twilio.calls)}
    assert sorted(texted) == ["+15705558002", "+15705558003"]                  # "both" isn't a guard: confirmed text
    assert texted["+15705558003"].startswith("TWG DISPATCH:") and texted["+15705558002"].startswith("TWG CONFIRMED:")

    # Mike's truck goes offline: Guard 1 is the closest usable one now.
    mike_truck = (await session.scalars(select(Tracker).where(Tracker.name == "Mike's truck"))).one()
    mike_truck.online = False
    await session.commit()
    b = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{b.id}/ack", json={"verdict": "real"})
    await sms.drain()
    texted = {to: body_of(c) for to, c in zip(sent_to(twilio)[2:], twilio.calls[2:])}
    assert sorted(texted) == ["+15705558001", "+15705558002"] and texted["+15705558001"].startswith("TWG DISPATCH:")


@respx.mock
async def test_share_my_location_now_from_the_texted_alarm_page(session, client):
    """The alarm page a guard was texted has "Share my location now": it makes them a phone tracker if they have none
    (named "(phone)" when they already have a router), and the page then sends positions like the location page."""
    from app import alarm_links
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await track(session, t.id, "Guard truck", 500, g["guard"])                     # a router already linked to them
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    token = await alarm_links.mint(session, alarm, await session.get(User, g["guard"].id), 24)
    nobody = await alarm_links.mint(session, alarm, None, 24)
    await session.commit()

    page = (await client.get(f"/a/{token}/")).text
    assert "Share my location now" in page and '"canShare": true' in page
    assert "Share my location now" not in (await client.get(f"/a/{nobody}/")).text
    r = await client.post(f"/api/a/{token}/share")
    assert r.status_code == 200 and r.json()["name"] == "guard (phone)"
    share = r.json()["token"]
    lat, lng = north(200)
    assert (await client.post(f"/api/g/{share}/pos", json={"lat": lat, "lng": lng, "accuracy": 5})).json()["taken"]
    phone = (await session.scalars(select(Tracker).where(Tracker.source == "phone"))).one()
    await session.refresh(phone)
    assert phone.user_id == g["guard"].id and phone.online and round(phone.lat, 6) == round(lat, 6)
    again = await client.post(f"/api/a/{token}/share")                           # same tracker, same link
    assert again.json()["token"] == share and len((await session.scalars(select(Tracker))).all()) == 2
    assert (await client.post(f"/api/a/{nobody}/share")).status_code == 400
    assert (await client.post("/api/a/not-a-token/share")).status_code == 404



@respx.mock
async def test_dispatch_several_guards_mdt_status_and_a_live_timeline(session, client):
    """Dispatch to all guards at once; each guard's taps (opened, En route, On scene, Clear) show on the dispatcher's
    board and timeline, live, and in the incident report."""
    from app import alarm_links
    from app.services import report
    from app.services.bus import bus
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMx"}))
    t1 = await track(session, t.id, "Guard 1", 300, g["guard"])
    t2 = await track(session, t.id, "Guard 2", 800, g["both"])
    nobody = await track(session, t.id, "Unlinked", 50)
    await login(client, "mike@twgsecurity.com")
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    sub = bus.subscribe({t.id})

    r = await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_ids": [t1.id, t2.id, nobody.id]})
    assert r.status_code == 200
    assert [x["to"] for x in r.json()["sent"]] == ["Guard 1", "Guard 2"]
    assert r.json()["failed"] == [{"name": "Unlinked", "error": "No person linked to text (Sites → Guards & vehicles)"}]
    assert (await client.post(f"/api/alarms/{alarm.id}/dispatch", json={"tracker_ids": [nobody.id]})).status_code == 400
    assert (await client.post(f"/api/alarms/{alarm.id}/dispatch", json={})).status_code == 400
    await sms.drain()
    assert sorted(sent_to(twilio)) == ["+15705558001", "+15705558002"]
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    assert ("alarm.timeline", {"alarm_id": alarm.id}) in events                  # dispatchers' screens refresh

    # Guard 1 opens the link from the text and taps En route, then On scene (their router places them).
    to_guard1 = twilio.calls[sent_to(twilio).index("+15705558001")]
    link = re.search(r"/a/([^/ \n]+)/", body_of(to_guard1)).group(1)
    client.cookies.clear()
    page = (await client.get(f"/a/{link}/")).text
    assert "En route" in page and "On scene" in page and '"status": ""' in page
    assert (await client.post(f"/api/a/{link}/status", json={"status": "en_route"})).json()["position"] is True
    assert (await client.post(f"/api/a/{link}/status", json={"status": "on_scene"})).status_code == 200
    assert (await client.post(f"/api/a/{link}/status", json={"status": "lunch"})).status_code == 422
    assert '"status": "on_scene"' in (await client.get(f"/a/{link}/")).text

    await login(client, "mike@twgsecurity.com")
    board = (await client.get(f"/api/alarms/{alarm.id}/guards")).json()["dispatched"]
    g1 = next(d for d in board if d["guard"] == "Guard 1")
    assert g1["status"] == "on_scene" and g1["opened_at"] and g1["status_at"] and g1["by"] == "mike"
    assert next(d for d in board if d["guard"] == "Guard 2")["status"] == "dispatched"

    rows = (await client.get(f"/api/alarms/{alarm.id}/timeline")).json()
    whats = [(e["what"], e["who"]) for e in rows]
    for want in [("Event occurred in NX", "Front Door Cam"), ("Dispatched Guard 1", "mike"), ("Dispatch text", "guard"),
                 ("Opened the alarm link", "guard"), ("En route", "guard"), ("On scene", "guard")]:
        assert want in whats, want
    on_scene = next(e for e in rows if e["what"] == "On scene")
    assert on_scene["kind"] == "guard" and "from the alarm" in on_scene["detail"] and on_scene["detail"].startswith("at 40.")
    assert [e["ts"] for e in rows] == sorted(e["ts"] for e in rows)
    # The incident report has the same timeline.
    events = await report.timeline(session, await session.get(type(alarm), alarm.id))
    assert [e["what"] for e in events] == [e["what"] for e in rows]
    pdf = report.build_pdf(await session.get(type(alarm), alarm.id), report.Evidence(), events, p["admin"], "", 10, 20, "sd", now_ms(), [])
    assert pdf.startswith(b"%PDF")

    # Another company sees none of it; a status from a link not sent to a person is refused.
    nolink = await alarm_links.mint(session, alarm, None, 24)
    await session.commit()
    client.cookies.clear()
    assert (await client.post(f"/api/a/{nolink}/status", json={"status": "clear"})).status_code == 400
    await login(client, p["acme"].email)
    assert (await client.get(f"/api/alarms/{alarm.id}/timeline")).status_code == 404


@respx.mock
async def test_a_dispatched_real_alarm_stays_open_as_a_call_until_ended(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMk"}))
    guard = await track(session, t.id, "Guard 1", 300, g["guard"])
    await login(client, "admin@twgsecurity.com")
    tenant = await session.get(Tenant, t.id)
    tenant.settings = {"auto_dispatch": False}
    await session.commit()

    a = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    assert (await client.post(f"/api/alarms/{a.id}/end-call", json={})).status_code == 400          # no call yet
    await client.post(f"/api/alarms/{a.id}/dispatch", json={"tracker_id": guard.id})
    assert (await client.get(f"/api/alarms/{a.id}")).json()["call_state"] == "active"
    r = await client.post(f"/api/alarms/{a.id}/end-call", json={})
    assert r.status_code == 400 and "Acknowledge" in r.json()["detail"]                              # acknowledge first
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real", "note": "Intruder"})
    assert [x["id"] for x in (await client.get("/api/alarms?state=active")).json()] == [a.id]       # still on screen
    await sms.drain()
    n = len(twilio.calls)

    r = await client.post(f"/api/alarms/{a.id}/end-call", json={"note": "Police took over"})
    assert r.status_code == 200 and r.json()["call_state"] == "ended" and r.json()["call_end_note"] == "Police took over"
    assert (await client.get("/api/alarms?state=active")).json() == []
    await sms.drain()
    assert sent_to(twilio)[n:] == ["+15705558001"]
    body = body_of(twilio.calls[n])
    assert body.startswith("TWG CALL ENDED: Panic button at TWG Security Office.") and "Note: Police took over" in body
    rows = (await client.get(f"/api/alarms/{a.id}/timeline")).json()
    ended = next(e for e in rows if e["what"] == "Call ended")
    assert ended["who"] == "admin" and "Police took over" in ended["detail"]
    assert any(e["what"] == "Call-ended text" for e in rows)

    # A guard who already tapped Clear isn't texted; another company can't end it.
    b = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{b.id}/dispatch", json={"tracker_id": guard.id})
    await sms.drain()
    from app import alarm_links
    link = re.search(r"/a/([^/ \n]+)/", body_of(twilio.calls[-1])).group(1)
    await client.post(f"/api/alarms/{b.id}/ack", json={"verdict": "real"})
    client.cookies.clear()
    await client.post(f"/api/a/{link}/status", json={"status": "clear"})
    await login(client, p["acme"].email)
    assert (await client.post(f"/api/alarms/{b.id}/end-call", json={})).status_code == 404
    await login(client, "admin@twgsecurity.com")
    n = len(twilio.calls)
    assert (await client.post(f"/api/alarms/{b.id}/end-call", json={})).status_code == 200
    await sms.drain()
    assert len(twilio.calls) == n


@respx.mock
async def test_a_false_alarm_ends_the_call_and_stands_the_guard_down(session, client):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMf"}))
    guard = await track(session, t.id, "Guard 1", 300, g["guard"])
    await login(client, "admin@twgsecurity.com")
    a = await make_alarm(session, site, priority=1, event_ts_ms=now_ms())
    await client.post(f"/api/alarms/{a.id}/dispatch", json={"tracker_id": guard.id})
    await sms.drain()
    n = len(twilio.calls)
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "false"})
    await sms.drain()
    got = (await client.get(f"/api/alarms/{a.id}")).json()
    assert got["call_state"] == "ended" and got["call_ended_by"] == "admin"
    assert sent_to(twilio)[n:] == ["+15705558001"] and body_of(twilio.calls[n]).startswith("TWG STAND DOWN:")
    assert "marked a false alarm by admin" in body_of(twilio.calls[n])


def test_safari_fix_times_never_break_a_position():
    """Safari sent a timestamp that isn't milliseconds (a year-58753 date crashed the endpoint with a 500)."""
    import time as _t
    from app.routers.share import fix_time
    now = _t.time()
    assert abs(fix_time(now * 1000).timestamp() - now) < 1                  # milliseconds (Chrome, Brave)
    assert abs(fix_time(now * 1_000_000).timestamp() - now) < 1             # microseconds
    assert fix_time(1_771_234_567_890_123_456) is None and fix_time(None) is None and fix_time(5) is None


async def test_a_bad_fix_time_is_ignored_not_an_error(session, client):
    t, site, p = await world(session)
    tr = await track(session, t.id, "Phone", 10, p["all"], source="phone")
    from app import guard_share
    token = guard_share.share_url(tr).rstrip("/").rsplit("/", 1)[-1]
    await session.commit()
    lat, lng = north(40)
    r = await client.post(f"/api/g/{token}/pos", json={"lat": lat, "lng": lng, "ts": 1.85e15 * 1000})
    assert r.status_code == 200 and r.json()["taken"]

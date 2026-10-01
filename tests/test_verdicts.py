"""Verdicts (real / false alarm), groups and permissions, bulk edit, and the live video relay."""
import asyncio

import httpx
import respx
from sqlalchemy import select

from app.models import Alarm, AuditLog, User
from app.security import hash_password
from app.services.poller import ingest
from tests.conftest import NX, PASSWORD, login, make_site, make_tenant_user, nx_row


async def _as(client, email):
    """Sign in as someone else (the /login page redirects while signed in)."""
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    await login(client, email)


async def _operator(session, tenant, email="op@twgsecurity.com"):
    u = User(tenant_id=tenant.id, email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD),
             role="operator")
    session.add(u)
    await session.commit()
    return u


async def _alarms(session, tenant, n=1, device=""):
    site = await make_site(session, tenant)
    out = await ingest(session, site, [nx_row(5_000_000 + i * 1000, action_id=f"a{i}", device=device) for i in range(n)], {})
    await session.commit()
    return out


async def test_acknowledge_records_the_verdict(client, session, admin):
    tenant, user = admin
    a, b = await _alarms(session, tenant, 2)
    await login(client, user.email)
    r = await client.post(f"/api/alarms/{a.id}/ack", json={"note": "Wind in the trees", "verdict": "false"})
    assert r.status_code == 200, r.text
    assert (r.json()["verdict"], r.json()["verdict_by"]) == ("false", user.display_name)
    log = (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.acknowledged"))).one()
    assert log.detail["verdict"] == "false"
    # A page opened before verdicts existed still acknowledges (no verdict).
    r = await client.post(f"/api/alarms/{b.id}/ack", json={"note": ""})
    assert r.status_code == 200 and r.json()["verdict"] == ""
    assert (await client.post(f"/api/alarms/{b.id}/ack", json={"verdict": "maybe"})).status_code == 422
    assert [x["id"] for x in (await client.get("/api/alarms?state=all&verdict=false")).json()] == [a.id]
    assert [x["id"] for x in (await client.get("/api/alarms?state=all&verdict=none")).json()] == [b.id]


async def test_bulk_edit_needs_the_permission_and_groups_grant_it(client, session, admin):
    tenant, boss = admin
    op = await _operator(session, tenant)
    [a] = await _alarms(session, tenant)
    await _as(client, op.email)
    r = await client.post("/api/alarms/bulk", json={"ids": [a.id], "verdict": "false"})
    assert r.status_code == 403
    assert "alarms.bulk_edit" not in (await client.get("/")).text            # not offered in the page config
    assert (await client.get("/api/groups")).status_code == 403             # only admins manage groups

    await _as(client, boss.email)
    r = await client.post("/api/groups", json={"name": "Supervisors", "permissions": ["alarms.bulk_edit"], "member_ids": [op.id]})
    assert r.status_code == 200, r.text
    gid = r.json()["groups"][0]["id"]
    assert r.json()["groups"][0]["member_ids"] == [op.id]
    assert (await client.post("/api/groups", json={"name": "Supervisors"})).status_code == 409
    assert (await client.post("/api/groups", json={"name": "X", "permissions": ["root"]})).status_code == 400

    await _as(client, op.email)
    assert '"alarms.bulk_edit"' in (await client.get("/")).text
    r = await client.post("/api/alarms/bulk", json={"ids": [a.id], "verdict": "false", "note": "storm"})
    assert r.status_code == 200 and r.json()["acknowledged"] == [a.id]

    await _as(client, boss.email)
    await client.put(f"/api/groups/{gid}", json={"name": "Supervisors", "permissions": [], "member_ids": [op.id]})
    await _as(client, op.email)
    assert (await client.post("/api/alarms/bulk", json={"ids": [a.id], "verdict": "real"})).status_code == 403
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.action.startswith("group.")))).all()
    assert actions == ["group.created", "group.updated"]


async def test_bulk_acknowledges_open_alarms_and_overrides_closed_ones(client, session, admin):
    tenant, user = admin
    open1, open2, closed, same = await _alarms(session, tenant, 4)
    other_tenant, _ = await make_tenant_user(session, "Other Co", "x@other.test")
    [foreign] = await _alarms(session, other_tenant)
    await login(client, user.email)
    await client.post(f"/api/alarms/{closed.id}/ack", json={"verdict": "real"})
    await client.post(f"/api/alarms/{same.id}/ack", json={"verdict": "false"})

    r = await client.post("/api/alarms/bulk", json={
        "ids": [open1.id, open2.id, closed.id, same.id, foreign.id], "verdict": "false", "note": "Wind storm, reviewed"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["acknowledged"], body["changed"], sorted(body["unchanged"])) == \
        ([open1.id, open2.id], [closed.id], sorted([same.id, foreign.id]))
    for a in (await session.scalars(select(Alarm).where(Alarm.tenant_id == tenant.id)
                                    .execution_options(populate_existing=True))).all():
        assert (a.state, a.verdict) == ("acknowledged", "false")
    assert (await session.get(Alarm, open1.id)).ack_note == "Wind storm, reviewed"
    assert (await session.get(Alarm, foreign.id)).state == "new"          # other tenants are untouched
    override = (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.verdict"))).one()
    assert (override.alarm_id, override.detail["old"], override.detail["new"], override.detail["bulk"]) == \
        (closed.id, "real", "false", 5)


@respx.mock
async def test_live_video_is_relayed_and_capped_per_site(client, session, admin, monkeypatch):
    from app.routers import stream
    tenant, user = admin
    [a] = await _alarms(session, tenant, device="dev-1")
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 50_000
    live = respx.get(url__regex=rf"{NX}/media/dev-1\.webm.*").mock(
        return_value=httpx.Response(200, content=webm, headers={"content-type": "video/webm"}))
    await login(client, user.email)

    r = await client.get(f"/media/alarms/{a.id}/live.webm")

    assert r.status_code == 200 and r.content == webm and r.headers["content-type"] == "video/webm"
    assert live.calls.last.request.url.params["resolution"] == "640x360"
    assert stream._live_slots[a.site_id]._value == stream.LIVE_MAX_PER_SITE      # slot released after the stream
    stream._live_slots[a.site_id] = asyncio.Semaphore(0)                         # site already at its cap
    assert (await client.get(f"/media/alarms/{a.id}/live.webm")).status_code == 429
    stream._live_slots.pop(a.site_id)
    respx.get(url__regex=rf"{NX}/media/dev-1\.webm.*").mock(return_value=httpx.Response(500))
    assert (await client.get(f"/media/alarms/{a.id}/live.webm")).status_code == 502
    assert stream._live_slots[a.site_id]._value == stream.LIVE_MAX_PER_SITE      # released on failure too


async def test_follow_up_notes_after_acknowledging(client, session, admin):
    from app.services.bus import bus
    tenant, user = admin
    [a] = await _alarms(session, tenant)
    await login(client, user.email)
    await client.post(f"/api/alarms/{a.id}/ack", json={"verdict": "real", "note": "Intruder at the gate"})
    sub = bus.subscribe(tenant.id)

    r = await client.post(f"/api/alarms/{a.id}/notes", json={"text": "  Police on scene 11:45  "})

    assert r.status_code == 200, r.text
    assert (r.json()["text"], r.json()["by"]) == ("Police on scene 11:45", user.display_name)
    assert sub.queue.get_nowait()[0] == "alarm.note"
    await client.post(f"/api/alarms/{a.id}/notes", json={"text": "Keyholder called back, all clear"})
    notes = (await client.get(f"/api/alarms/{a.id}/notes")).json()
    assert [n["text"] for n in notes] == ["Police on scene 11:45", "Keyholder called back, all clear"]
    assert (await client.post(f"/api/alarms/{a.id}/notes", json={"text": "   "})).status_code == 422
    log = (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.note"))).all()
    assert [x.detail["text"] for x in log] == ["Police on scene 11:45", "Keyholder called back, all clear"]
    # Other tenants can't read or add notes.
    _, stranger = await make_tenant_user(session, "Other Co", "x@other.test")
    await _as(client, stranger.email)
    assert (await client.get(f"/api/alarms/{a.id}/notes")).status_code == 404
    assert (await client.post(f"/api/alarms/{a.id}/notes", json={"text": "hi"})).status_code == 404
    bus.unsubscribe(sub)

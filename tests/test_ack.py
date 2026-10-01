import json

import httpx
import respx
from sqlalchemy import select

from app.models import Alarm, AuditLog
from app.services.poller import ingest
from tests.conftest import NX, login, make_site, nx_row


async def _alarm(session, tenant, **kw):
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(7_000_000, **kw)], {})
    await session.commit()
    return alarm


@respx.mock
async def test_ack_forced_rule_calls_nx_acknowledge(client, session, admin):
    tenant, user = admin
    alarm = await _alarm(session, tenant, action_id="act-1", ack=True, server="srv-1")
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    ack = respx.post(f"{NX}/rest/v4/events/acknowledges").mock(return_value=httpx.Response(200, json={"id": "bm-1"}))
    await login(client, user.email)

    r = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "Verified on camera, police called"})

    assert r.status_code == 200, r.text
    body = json.loads(ack.calls.last.request.content)
    assert body["actionId"] == "act-1" and body["actionServerId"] == "srv-1" and body["deviceId"] == "dev-1"
    assert "police called" in body["description"]
    data = r.json()
    assert data["state"] == "acknowledged" and data["nx_ack_result"]["method"] == "nx_acknowledge"
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "alarm.acknowledged"))
    assert row.user_id == user.id and row.detail["note"] == "Verified on camera, police called"

    again = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "dup"})
    assert again.status_code == 409


@respx.mock
async def test_ack_without_forced_rule_creates_bookmark(client, session, admin):
    tenant, user = admin
    alarm = await _alarm(session, tenant)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    bm = respx.post(f"{NX}/rest/v4/devices/dev-1/bookmarks").mock(return_value=httpx.Response(200, json={"id": "bm-2"}))
    await login(client, user.email)
    r = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "false alarm"})
    assert r.status_code == 200 and bm.called
    assert r.json()["nx_ack_result"] == {"method": "bookmark", "ok": True, "bookmark_id": "bm-2"}


@respx.mock
async def test_ack_still_recorded_when_nx_unreachable(client, session, admin):
    tenant, user = admin
    alarm = await _alarm(session, tenant)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(side_effect=httpx.ConnectError("down"))
    await login(client, user.email)
    r = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "site offline"})
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "acknowledged" and data["nx_ack_result"]["ok"] is False
    stored = (await session.execute(select(Alarm).execution_options(populate_existing=True))).scalar_one()
    assert stored.state == "acknowledged" and stored.ack_note == "site offline"

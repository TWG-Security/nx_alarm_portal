"""Webhooks into NX (app/webhooks.py, app/routers/webhooks.py): a company's webhook on one of its sites sends a
generic event or fires a soft trigger on that site's NX, from outside (/hook/<token>, no sign-in) or from the portal
(Send test, site panel buttons). NX is tools/fake_nx.py on a real port, reached by the portal's real NX client."""
import asyncio
import socket

import pytest
import uvicorn
from sqlalchemy import select

from app import webhooks
from app.models import AuditLog, AuthEvent, User, Webhook
from app.security import hash_password
from app.services.poller import manager
from tests.conftest import PASSWORD, login, make_site
from tests.test_tenancy import world  # noqa: F401 - fixture
from tools import fake_nx

GATE = "11111111-1111-1111-1111-111111111111"
STROBE = "256dacab-d698-4a32-b9ec-135ba73ba127"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def nx():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(fake_nx.app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    fake_nx.NX_SENT.clear(); fake_nx.NX_EVENTS_MODE["mode"] = "ok"; webhooks._calls.clear()
    events = list(fake_nx.EVENTS)
    yield f"http://127.0.0.1:{port}"
    fake_nx.EVENTS[:] = events
    for rt in list(manager._rt.values()):
        await rt.client.close()
    manager._rt.clear()
    server.should_exit = True
    await task


def test_templates_and_fields():
    f = webhooks.flatten({"zone": "North lot", "event": {"type": "intrusion", "ids": [1, 2]}, "x": None})
    assert f == {"zone": "North lot", "event.type": "intrusion", "event.ids": "1, 2", "x": ""}
    assert webhooks.render("Intrusion in {zone} ({event.type}){missing}", f) == "Intrusion in North lot (intrusion)"
    with pytest.raises(ValueError, match="camera"):
        webhooks.clean_config("trigger", {"trigger_id": STROBE})
    with pytest.raises(ValueError, match="State"):
        webhooks.clean_config("generic", {"state": "active"})
    t, h, hint = webhooks.new_token()
    assert t.startswith("twgh_") and len(t) > 30 and h == webhooks.token_hash(t) and t not in hint


async def test_a_generic_event_webhook_from_outside(client, session, admin, nx):
    tenant, user = admin
    site = await make_site(session, tenant, host=nx)
    await login(client, user.email)
    r = await client.post("/api/webhooks", json={"site_id": site.id, "name": "Ajax hub", "kind": "generic", "config": {
        "source": "Ajax", "caption": "{event} in {zone}", "description": "", "device_ids": [GATE]}})
    assert r.status_code == 200, r.text
    made = r.json()
    token, url = made["token"], made["url"]
    assert url.endswith(f"/hook/{token}") and token not in str((await client.get("/api/webhooks")).json())
    assert (await session.scalar(select(Webhook.token_hash))) == webhooks.token_hash(token)   # only the hash is kept

    # Called from outside: no session, no CSRF token.
    client.cookies.clear(); client.headers.pop("X-CSRF-Token", None)
    r = await client.post(f"/hook/{token}", json={"event": "Intrusion", "zone": "Warehouse", "description": "Sensor 4"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["sent"] == {"source": "Ajax", "caption": "Intrusion in Warehouse",
                                                   "description": "Sensor 4", "state": "instant", "device_ids": [GATE]}
    [sent] = fake_nx.NX_SENT
    assert (sent["kind"], sent["source"], sent["caption"], sent["deviceIds"], sent["state"]) == (
        "generic", "Ajax", "Intrusion in Warehouse", [GATE], "instant")
    # GET with a query string (devices that can only do a GET), and the caller's state.
    r = await client.get(f"/hook/{token}", params={"event": "Door", "zone": "Lobby", "state": "started"})
    assert r.status_code == 200 and fake_nx.NX_SENT[-1]["caption"] == "Door in Lobby" and fake_nx.NX_SENT[-1]["state"] == "started"
    # A plain-text body is {text}.
    await login(client, user.email)
    await client.put(f"/api/webhooks/{made['id']}", json={"config": {"caption": "Panel says: {text}"}})
    client.cookies.clear(); client.headers.pop("X-CSRF-Token", None)
    await client.post(f"/hook/{token}", content=b"ZONE 3 ALARM", headers={"Content-Type": "text/plain"})
    assert fake_nx.NX_SENT[-1]["caption"] == "Panel says: ZONE 3 ALARM" and fake_nx.NX_SENT[-1]["source"] == "Ajax hub"

    # Wrong token: counted as a failed sign-in for the address; switched off: refused; NX refusing: 502 with why.
    r = await client.post("/hook/twgh_" + "x" * 32, json={})
    assert r.status_code == 404
    assert (await session.scalars(select(AuthEvent).where(AuthEvent.kind == "webhook"))).all()
    fake_nx.NX_EVENTS_MODE["mode"] = "refuse"
    r = await client.post(f"/hook/{token}", json={})
    assert r.status_code == 502 and "rights to create generic events" in r.json()["detail"]
    hook = await session.get(Webhook, made["id"], populate_existing=True)
    assert hook.uses == 4 and [c["ok"] for c in hook.recent] == [False, True, True, True]
    await login(client, user.email)
    await client.put(f"/api/webhooks/{made['id']}", json={"enabled": False})
    client.cookies.clear(); client.headers.pop("X-CSRF-Token", None)
    assert (await client.post(f"/hook/{token}", json={})).status_code == 403

    # Rotate: the old address stops working.
    await login(client, user.email)
    await client.put(f"/api/webhooks/{made['id']}", json={"enabled": True})
    new = (await client.post(f"/api/webhooks/{made['id']}/rotate")).json()["token"]
    fake_nx.NX_EVENTS_MODE["mode"] = "ok"
    assert (await client.post(f"/hook/{token}", json={})).status_code == 404
    assert (await client.post(f"/hook/{new}", json={})).status_code == 200
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.action.like("webhook.%")).order_by(AuditLog.id))).all()
    assert actions == ["webhook.created", "webhook.updated", "webhook.updated", "webhook.updated", "webhook.rotated"]


async def test_a_soft_trigger_button_raises_the_alarm_like_a_press_in_nx(client, session, admin, nx):
    tenant, user = admin
    site = await make_site(session, tenant, host=nx)
    op = User(tenant_id=tenant.id, email="op@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    await session.commit()
    await login(client, user.email)
    trig = (await client.get(f"/api/sites/{site.id}/nx-triggers")).json()
    assert [(t["name"], t["prolonged"], [c["id"] for c in t["cameras"]][:1]) for t in trig][0] == ("Strobe speaker armed", False, [GATE])
    assert (await client.post("/api/webhooks", json={"site_id": site.id, "name": "Strobe", "kind": "trigger",
                                                     "config": {"trigger_id": STROBE}})).status_code == 400   # no camera
    r = await client.post("/api/webhooks", json={"site_id": site.id, "name": "Strobe on", "kind": "trigger", "button": True,
                                                 "config": {"trigger_id": STROBE, "trigger_name": "Strobe speaker armed",
                                                            "device_id": GATE}})
    hid = r.json()["id"]
    plain = (await client.post("/api/webhooks", json={"site_id": site.id, "name": "Test only", "kind": "generic",
                                                      "config": {}})).json()["id"]

    # An operator sees the button on the site panel and presses it; NX fires the trigger on its camera.
    await login(client, op.email)
    assert [b["name"] for b in (await client.get(f"/api/sites/{site.id}/buttons")).json()] == ["Strobe on"]
    r = await client.post(f"/api/webhooks/{hid}/send", json={})
    assert r.status_code == 200, r.text
    assert fake_nx.NX_SENT[-1] == {**fake_nx.NX_SENT[-1], "kind": "trigger", "triggerId": STROBE, "deviceId": GATE, "state": "instant"}
    assert (await client.post(f"/api/webhooks/{plain}/send", json={})).status_code == 403   # not a button: admins only
    assert (await client.get("/api/webhooks")).status_code == 403
    row = (await session.scalars(select(AuditLog).where(AuditLog.action == "webhook.sent"))).one()
    assert row.user_id == op.id and row.detail["ok"] is True

    # NX logs the press like any soft trigger: the portal's own poll picks it up as an alarm.
    from app.services.poller import SiteRuntime
    rt = manager._rt.get(site.id) or SiteRuntime(site.id, tenant.id, manager.client_for(site))
    created = await manager.poll_once(rt)
    mine = [a for a in created if a.event_type in ("softTrigger", "generic")]
    assert [(a.event_type, a.priority, a.state) for a in mine] == [("softTrigger", 1, "new")]


async def test_webhooks_belong_to_their_company(client, session, world, nx):  # noqa: F811
    sa, sb = world["sites"]["Acme Security"].id, world["sites"]["Bravo Guards"].id
    await login(client, world["a_admin"].email)
    made = (await client.post("/api/webhooks", json={"site_id": sa, "name": "Acme hook", "kind": "generic", "config": {}})).json()
    assert (await client.post("/api/webhooks", json={"site_id": sb, "name": "x", "kind": "generic", "config": {}})).status_code == 404
    await login(client, world["b_admin"].email)
    assert (await client.get("/api/webhooks")).json() == []
    for method, path in (("PUT", f"/api/webhooks/{made['id']}"), ("DELETE", f"/api/webhooks/{made['id']}"),
                         ("POST", f"/api/webhooks/{made['id']}/send"), ("POST", f"/api/webhooks/{made['id']}/rotate")):
        assert (await client.request(method, path, json={})).status_code == 404, path


async def test_too_many_calls_are_turned_away(client, session, admin, nx, monkeypatch):
    tenant, user = admin
    site = await make_site(session, tenant, host=nx)
    await login(client, user.email)
    token = (await client.post("/api/webhooks", json={"site_id": site.id, "name": "Busy", "kind": "generic", "config": {}})).json()["token"]
    monkeypatch.setattr(webhooks, "RATE_PER_MIN", 3)
    codes = [(await client.post(f"/hook/{token}", json={})).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]

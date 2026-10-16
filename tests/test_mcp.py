"""The portal's MCP server (app/mcp_server.py) and API tokens (app/api_tokens.py). The user, 2026-10-15: "Make a MCP
for this platform", scope "Read + actions + dispatch"."""
import json

import httpx
import respx
from sqlalchemy import select

from app import sms
from app.models import ApiToken, AuditLog, AuthEvent, User
from tests.conftest import login
from tests.test_dispatch import track
from tests.test_sms import TWILIO, fresh_sms, guards, make_alarm, now_ms, nx_ok, sent_to, texts_on, world  # noqa: F401


async def _token(client, email, name="Claude gateway", days=90) -> str:
    await login(client, email)
    r = await client.post("/api/account/tokens", json={"name": name, "days": days})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith("twgp_") and r.json()["mcp_url"].endswith("/mcp")
    client.cookies.clear()                          # from here on: the bearer token alone
    client.headers.pop("X-CSRF-Token", None)
    return r.json()["token"]


async def rpc(client, token, method, params=None, mid=1):
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params is not None else {})})
    assert r.status_code == 200, r.text
    return r.json()


async def call(client, token, name, **args):
    out = (await rpc(client, token, "tools/call", {"name": name, "arguments": args}))["result"]
    text = out["content"][0]["text"]
    return (json.loads(text) if not out["isError"] else text), out["isError"]


async def test_handshake_tool_list_and_auth(client, session):
    t, site, p = await world(session)
    assert (await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
    r = await client.post("/mcp", headers={"Authorization": "Bearer twgp_nope"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    assert await session.scalar(select(AuthEvent).where(AuthEvent.kind == "api_token", AuthEvent.outcome == "failure"))

    token = await _token(client, "admin@twgsecurity.com")
    init = await rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "test", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"]
    assert init["result"]["serverInfo"]["name"] == "twg-alarm-portal"
    old = await rpc(client, token, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert old["result"]["protocolVersion"] == "2024-11-05"
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    tools = {t["name"]: t for t in (await rpc(client, token, "tools/list"))["result"]["tools"]}
    assert {"list_alarms", "get_alarm", "set_alarm_verdict", "dispatch_guards", "end_call", "get_alarm_snapshot"} <= set(tools)
    assert tools["list_alarms"]["annotations"]["readOnlyHint"] and tools["dispatch_guards"]["annotations"]["destructiveHint"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools.values())
    assert (await rpc(client, token, "nope"))["error"]["code"] == -32601
    assert (await client.get("/mcp")).status_code == 405
    batch = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                              json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert [m["id"] for m in batch.json()] == [1, 2]


@respx.mock
async def test_alarms_read_note_and_verdict_as_the_tokens_person(client, session):
    t, site, p = await world(session)
    nx_ok()
    if True:
        alarm = await make_alarm(session, site, caption="Panic button", event_ts_ms=now_ms())
        token = await _token(client, "admin@twgsecurity.com", name="Desk Claude")

        over, err = await call(client, token, "get_overview")
        assert not err and over["open_alarms"]["alarm"] == 1 and over["sites"] == 1
        listed, err = await call(client, token, "list_alarms", level="alarm")
        assert not err and [a["id"] for a in listed["alarms"]] == [alarm.id]
        a = listed["alarms"][0]
        assert a["state"] == "open" and a["site"] == site.name and a["link"].endswith(f"/?alarm={alarm.id}")
        one, err = await call(client, token, "get_alarm", alarm_id=alarm.id)
        assert not err and one["caption"] == "Panic button" and any(r["what"] for r in one["timeline"])

        note, err = await call(client, token, "add_alarm_note", alarm_id=alarm.id, text="Checked cameras, nobody there")
        assert not err and note["note"]["text"] == "Checked cameras, nobody there"
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "alarm.note"))
        assert row.detail["via"] == "MCP (Desk Claude)" and row.user_id == p["admin"].id     # audited as the person, via MCP

        done, err = await call(client, token, "set_alarm_verdict", alarm_id=alarm.id, verdict="false", note="Wind")
        assert not err and done["done"] == "acknowledged" and done["alarm"]["verdict"] == "false"
        assert done["alarm"]["state"] == "acknowledged" and done["alarm"]["acknowledged_by"]
        bad, err = await call(client, token, "get_alarm", alarm_id=999999)
        assert err and "not found" in bad.lower()
        bad, err = await call(client, token, "set_alarm_verdict", alarm_id=alarm.id, verdict="maybe")
        assert err and "verdict" in bad


async def test_a_token_sees_only_its_company(client, session):
    t, site, p = await world(session)
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())          # TWG's site
    token = await _token(client, "x@acme.test")                           # Acme's admin
    listed, err = await call(client, token, "list_alarms", state="all")
    assert not err and listed["alarms"] == []
    bad, err = await call(client, token, "get_alarm", alarm_id=alarm.id)
    assert err and "not found" in bad.lower()
    bad, err = await call(client, token, "add_alarm_note", alarm_id=alarm.id, text="hi")
    assert err
    # company_id can't widen it: only TWG staff with platform access choose a company
    listed, err = await call(client, token, "list_alarms", state="all", company_id=t.id)
    assert not err and listed["alarms"] == []


@respx.mock
async def test_dispatch_texts_the_guard_and_end_call(client, session):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMm"}))
    nx_ok()
    guard = await track(session, t.id, "Guard 1", 400, g["guard"])
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    token = await _token(client, "mike@twgsecurity.com")

    cands, err = await call(client, token, "get_dispatch_candidates", alarm_id=alarm.id)
    assert not err and cands["guards"][0]["guard_id"] == guard.id and cands["guards"][0]["can_text"]
    out, err = await call(client, token, "dispatch_guards", alarm_id=alarm.id, guard_ids=[guard.id])
    assert not err and out["sent"][0]["to"] == "Guard 1" and not out["failed"]
    await sms.drain()
    assert sent_to(twilio) == ["+15705558001"]
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "alarm.dispatched"))
    assert row is None or row.detail.get("via", "").startswith("MCP")
    calls, err = await call(client, token, "list_alarms", state="active")
    assert not err and [a["id"] for a in calls["alarms"]] == [alarm.id]
    bad, err = await call(client, token, "end_call", alarm_id=alarm.id)
    assert err and "Acknowledge the alarm first" in bad                  # the portal's own rule, told to the model
    done, err = await call(client, token, "set_alarm_verdict", alarm_id=alarm.id, verdict="real", note="Guard on scene")
    assert not err and done["alarm"]["verdict"] == "real"
    ended, err = await call(client, token, "end_call", alarm_id=alarm.id, note="All clear")
    assert not err and ended["alarm"]["call"] == "ended", ended
    bad, err = await call(client, token, "dispatch_guards", alarm_id=alarm.id, guard_ids=[987654])
    assert err and "not found" in bad.lower()


async def test_tokens_stop_working(client, session):
    t, site, p = await world(session)
    token = await _token(client, "admin@twgsecurity.com")
    assert (await rpc(client, token, "ping"))["result"] == {}
    row = await session.scalar(select(ApiToken))
    assert row.token_hash != token and token[-4:] in row.hint                       # only the hash is stored
    assert token not in json.dumps([a.detail for a in (await session.scalars(select(AuditLog))).all()])
    # Revoked on the Account page
    await login(client, "admin@twgsecurity.com")
    listed = (await client.get("/api/account/tokens")).json()["tokens"]
    assert [x["name"] for x in listed] == ["Claude gateway"] and listed[0]["last_used_at"] and "token" not in listed[0]
    assert (await client.delete(f"/api/account/tokens/{listed[0]['id']}")).status_code == 200
    client.cookies.clear()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

    # Sign out everywhere ends tokens too
    token = await _token(client, "admin@twgsecurity.com", name="second")
    await login(client, "admin@twgsecurity.com")
    await client.post("/api/account/sign-out-everywhere")
    client.cookies.clear()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

    # A disabled person's token stops
    token = await _token(client, "all@twgsecurity.com", name="third")
    session.expire_all()
    u = await session.scalar(select(User).where(User.email == "all@twgsecurity.com"))
    u.is_active = False
    await session.commit()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

    # Admins see and revoke a user's tokens
    token = await _token(client, "crit@twgsecurity.com", name="crit's")
    await login(client, "admin@twgsecurity.com")
    crit = await session.scalar(select(User).where(User.email == "crit@twgsecurity.com"))
    sec = (await client.get(f"/api/users/{crit.id}/security")).json()
    assert [x["name"] for x in sec["tokens"]] == ["crit's"]
    assert (await client.delete(f"/api/users/{crit.id}/tokens/{sec['tokens'][0]['id']}")).status_code == 200
    client.cookies.clear()
    r = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

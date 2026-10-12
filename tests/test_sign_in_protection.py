"""Sign-in protection: IP bans with escalation, account locks, allowlist, trusted proxies, Cloudflare edge bans.

Test requests come from 127.0.0.1 (always a trusted proxy), so a CF-Connecting-IP header sets the client
address the way the Cloudflare Tunnel does in production.
"""
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, update

from app import cloudflare_edge, net, platform_settings
from app import security_guard as guard
from app.models import AuditLog, AuthEvent, IpBan, PlatformSettings, Tenant, User, UserGroup, UserGroupMember
from app.security import hash_password
from tests.conftest import PASSWORD, login

ATTACKER = "203.0.113.7"
CF = cloudflare_edge.API + "/zones/" + "a" * 32 + "/firewall/access_rules/rules"


def via(ip):
    return {"CF-Connecting-IP": ip}


async def attempt(client, email, password, ip):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = await client.get("/login", headers=via(ip))
    if r.status_code != 200:
        return r
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    return await client.post("/login", data={"email": email, "password": password, "csrf_token": csrf}, headers=via(ip))


async def fail(client, n, ip, email="nobody@example.com"):
    for _ in range(n):
        r = await attempt(client, email, "wrong-password", ip)
    return r


@pytest.fixture
async def twg(session):
    """TWG (platform) with an admin; the admin holds platform.* permissions."""
    t = Tenant(name="TWG Security", kind="platform")
    session.add(t)
    await session.flush()
    admin = User(tenant_id=t.id, email="boss@twgsecurity.com", display_name="Boss", password_hash=hash_password(PASSWORD),
                 role="admin")
    session.add(admin)
    await session.commit()
    return t, admin


async def as_admin(client, admin, ip="198.51.100.20"):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.headers["CF-Connecting-IP"] = ip
    await login(client, admin.email)


async def age_events(session, ip, minutes=11):
    """Push an IP's failures out of the counting window."""
    rows = (await session.scalars(select(AuthEvent).where(AuthEvent.ip == ip))).all()
    for e in rows:
        e.ts = guard.aware(e.ts) - timedelta(minutes=minutes)
    await session.commit()


async def expire_ban(session, ip):
    await session.execute(update(IpBan).where(IpBan.ip == ip).values(expires_at=guard.utcnow() - timedelta(seconds=1)))
    await session.commit()


# ---------------------------------------------------------------- client address
class FakeReq:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})()
        self.headers = {k.lower(): v for k, v in headers.items()}


async def test_cf_header_only_believed_from_a_trusted_connector(session):
    assert net.resolve(FakeReq("10.0.2.58", via(ATTACKER))) == ("10.0.2.58", "untrusted-proxy")
    assert "10.0.2.58" in net.untrusted_cf_peers                                    # shown on the Platform page
    assert net.resolve(FakeReq("198.51.100.1", via("8.8.8.8"))) == ("198.51.100.1", "untrusted-proxy")   # forged
    assert net.resolve(FakeReq("10.1.10.50", {})) == ("10.1.10.50", "direct")
    row = await platform_settings.get_row(session)
    row.trusted_proxies = "10.0.2.58"
    await session.commit()
    await platform_settings.refresh()
    assert net.resolve(FakeReq("10.0.2.58", via(ATTACKER))) == (ATTACKER, "cloudflare")
    assert net.resolve(FakeReq("::ffff:10.0.2.58", via("2001:db8::1"))) == ("2001:db8::1", "cloudflare")
    assert net.resolve(FakeReq("10.0.2.58", via("not-an-ip"))) == ("10.0.2.58", "direct")
    assert net.resolve(FakeReq("10.0.2.59", via(ATTACKER)))[1] == "untrusted-proxy"


async def test_only_private_connectors_can_be_trusted(client, twg):
    await as_admin(client, twg[1])
    r = await client.put("/api/platform/settings/proxies", json={"trusted": "10.0.2.58, 8.8.8.8"})
    assert r.status_code == 400 and "public" in r.text
    assert (await client.put("/api/platform/settings/proxies", json={"trusted": "10.0.0.0/8"})).status_code == 400
    r = await client.put("/api/platform/settings/proxies", json={"trusted": "10.0.2.58  10.0.2.60/32"})
    assert r.status_code == 200 and r.json()["proxies"]["trusted"] == "10.0.2.58, 10.0.2.60"


# ---------------------------------------------------------------- bans
async def test_five_failures_ban_the_address_and_show_the_blocked_page(client, twg):
    r = await fail(client, 4, ATTACKER)
    assert r.status_code == 200 and "Invalid email or password" in r.text
    r = await fail(client, 1, ATTACKER)
    assert r.status_code == 403 and "blocked" in r.text.lower() and ATTACKER in r.text
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 403
    r = await client.get("/api/sites", headers=via(ATTACKER))
    assert r.status_code == 403 and "blocked" in r.json()["detail"]
    # Another address is unaffected, and even the right password from the banned one is refused.
    assert (await attempt(client, twg[1].email, PASSWORD, "198.51.100.30")).status_code == 303
    assert (await attempt(client, twg[1].email, PASSWORD, ATTACKER)).status_code == 403


async def test_escalation_15m_1h_4h_16h_then_permanent(client, session, twg):
    expected = [15, 60, 240, 960]
    for n, minutes in enumerate(expected, 1):
        await fail(client, 5, ATTACKER)
        ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
        await session.refresh(ban)
        assert ban.ban_count == n and not ban.permanent
        left = guard.aware(ban.expires_at) - guard.utcnow()
        assert timedelta(minutes=minutes - 1) < left <= timedelta(minutes=minutes), (n, left)
        await expire_ban(session, ATTACKER)
        await age_events(session, ATTACKER)
    await fail(client, 5, ATTACKER)
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await session.refresh(ban)
    assert ban.ban_count == 5 and ban.permanent and "permanent" in ban.reason
    assert "The block is permanent" in (await client.get("/login", headers=via(ATTACKER))).text


async def test_ban_length_is_capped(session):
    rules = platform_settings.BanRules(first_min=15, max_min=10080, permanent_after=0)
    assert [guard.ban_minutes(rules, n) for n in (1, 2, 3, 4, 5, 6, 9)] == [15, 60, 240, 960, 3840, 10080, 10080]


async def test_never_banned_internal_allowlisted_or_live_session_addresses(client, session, twg):
    await fail(client, 7, "10.1.2.3", email="a@example.com")              # the LAN
    await fail(client, 7, "192.168.5.5", email="b@example.com")
    await as_admin(client, twg[1])
    assert (await client.post("/api/platform/allowlist", json={"ip": "198.51.100.0/24", "label": "TWG office"})).status_code == 200
    await fail(client, 7, "198.51.100.77", email="c@example.com")
    # Someone signed in from this address a moment ago: a colleague's typos there don't ban it.
    await as_admin(client, twg[1], ip="192.0.2.50")
    await client.get("/api/sites")
    await fail(client, 7, "192.0.2.50", email="colleague@example.com")
    assert (await session.scalars(select(IpBan))).all() == []
    # The failures were still recorded (and count toward account locks).
    n = len((await session.scalars(select(AuthEvent).where(AuthEvent.outcome == "failure"))).all())
    assert n == 28


async def test_signed_in_sessions_are_never_cut_off_by_a_ban(client, session, twg):
    await as_admin(client, twg[1], ip=ATTACKER)
    async with session.begin():
        await guard.ban(session, ATTACKER, reason="test", permanent=True)
    assert (await client.get("/api/sites")).status_code == 200          # the monitoring screen keeps working
    assert (await client.get("/api/alarms?state=open")).status_code == 200
    assert (await client.get("/")).status_code == 200
    client.cookies.clear()
    assert (await client.get("/api/sites")).status_code == 403          # without a session: blocked


async def test_account_lock_across_addresses_then_admin_unlock(client, session, twg):
    victim = twg[1].email
    for i in range(10):                                                  # a slow spray: one try per address
        await attempt(client, victim, "guess", f"203.0.113.{100 + i}")
    assert (await session.scalars(select(IpBan))).all() == []
    r = await attempt(client, victim, PASSWORD, "198.51.100.40")         # even the right password is refused
    assert r.status_code == 200 and "Invalid email or password" in r.text
    locked = await session.scalar(select(AuthEvent).where(AuthEvent.outcome == "locked"))
    assert locked.email == victim
    # TWG sees and clears it (from another admin account; the locked one can't sign in).
    other = User(tenant_id=twg[0].id, email="noc@twgsecurity.com", password_hash=hash_password(PASSWORD), role="admin")
    session.add(other)
    await session.commit()
    await as_admin(client, other)
    sec = (await client.get("/api/platform/security")).json()
    assert [x["email"] for x in sec["locked"]] == [victim]
    assert (await client.post("/api/platform/locks/clear", json={"email": victim})).status_code == 200
    assert (await client.get("/api/platform/security")).json()["locked"] == []
    assert (await attempt(client, victim, PASSWORD, "198.51.100.40")).status_code == 303


async def test_unknown_and_known_emails_answer_the_same(client, twg):
    a = await attempt(client, "nobody@example.com", "x", "198.51.100.61")
    b = await attempt(client, twg[1].email, "x", "198.51.100.62")
    assert a.status_code == b.status_code == 200
    import re
    strip = lambda t, email: re.sub(r'(content|value)="[A-Za-z0-9_\-]{40,}"', "", t).replace(email, "X")  # noqa: E731
    assert "Invalid email or password" in a.text
    assert strip(a.text, "nobody@example.com") == strip(b.text, twg[1].email)


async def test_unban_forgives_and_restarts_escalation(client, session, twg):
    await fail(client, 5, ATTACKER)
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await as_admin(client, twg[1])
    assert (await client.delete(f"/api/platform/bans/{ban.id}")).status_code == 200
    client.cookies.clear()
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 200
    r = await fail(client, 1, ATTACKER)                  # the old failures no longer count
    assert r.status_code == 200
    await fail(client, 4, ATTACKER)
    await session.refresh(ban)
    assert ban.ban_count == 1 and timedelta(minutes=14) < guard.aware(ban.expires_at) - guard.utcnow() <= timedelta(minutes=15)
    actions = {a.action for a in (await session.scalars(select(AuditLog))).all()}
    assert "security.unban" in actions


async def test_manual_ban_guards(client, twg):
    await as_admin(client, twg[1], ip="198.51.100.20")
    r = await client.post("/api/platform/bans", json={"ip": "198.51.100.20"})
    assert r.status_code == 400 and "your own address" in r.text
    assert "internal" in (await client.post("/api/platform/bans", json={"ip": "10.0.2.58"})).text
    await client.post("/api/platform/allowlist", json={"ip": "192.0.2.9", "label": "office"})
    assert "allowlisted" in (await client.post("/api/platform/bans", json={"ip": "192.0.2.9"})).text
    assert (await client.post("/api/platform/bans", json={"ip": "192.0.2.0/24"})).status_code == 400
    r = await client.post("/api/platform/bans", json={"ip": ATTACKER, "minutes": 60, "reason": "scanner"})
    assert r.status_code == 200 and r.json()["manual"] and not r.json()["permanent"]
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 403


async def test_allowlisting_lifts_existing_bans(client, session, twg):
    await fail(client, 5, ATTACKER)
    await as_admin(client, twg[1])
    await client.post("/api/platform/allowlist", json={"ip": ATTACKER, "label": "oops, that's us"})
    assert (await client.get("/api/platform/security")).json()["bans"] == []
    client.cookies.clear()
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 200


# ---------------------------------------------------------------- settings & permissions
async def test_rules_are_editable_and_take_effect(client, session, twg):
    await as_admin(client, twg[1])
    rules = {"max_fails": 3, "window_min": 5, "first_min": 30, "max_min": 600, "permanent_after": 0, "account_lock_max": 20}
    r = await client.put("/api/platform/settings/sign-in", json=rules)
    assert r.status_code == 200 and r.json()["sign_in"] == rules
    assert (await client.put("/api/platform/settings/sign-in", json={**rules, "max_min": 10})).status_code == 400
    assert (await client.put("/api/platform/settings/sign-in", json={**rules, "account_lock_max": 2})).status_code == 400
    await fail(client, 3, ATTACKER)
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    assert ban is not None and timedelta(minutes=29) < guard.aware(ban.expires_at) - guard.utcnow() <= timedelta(minutes=30)
    log = await session.scalar(select(AuditLog).where(AuditLog.action == "platform.settings"))
    assert log.detail["fields"] == list(rules) and log.tenant_id == twg[0].id


async def test_platform_api_is_twg_only_and_view_only_without_manage(client, session, twg):
    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.flush()
    cust = User(tenant_id=acme.id, email="admin@acme.test", password_hash=hash_password(PASSWORD), role="admin")
    noc = User(tenant_id=twg[0].id, email="noc@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add_all([cust, noc])
    await session.flush()
    g = UserGroup(tenant_id=twg[0].id, name="NOC", permissions=["platform.view"])
    session.add(g)
    await session.flush()
    session.add(UserGroupMember(group_id=g.id, user_id=noc.id))
    await session.commit()
    reads = ["/platform", "/api/platform/settings", "/api/platform/security", "/api/platform/auth-events"]
    writes = [("PUT", "/api/platform/settings/sign-in", {"max_fails": 5, "window_min": 10, "first_min": 15, "max_min": 60,
                                                          "permanent_after": 5, "account_lock_max": 10}),
              ("PUT", "/api/platform/settings/proxies", {"trusted": ""}),
              ("PUT", "/api/platform/settings/cloudflare", {"enabled": False}),
              ("POST", "/api/platform/cloudflare/test", None),
              ("POST", "/api/platform/bans", {"ip": ATTACKER}), ("DELETE", "/api/platform/bans/1", None),
              ("POST", "/api/platform/allowlist", {"ip": ATTACKER}), ("DELETE", "/api/platform/allowlist/1", None),
              ("POST", "/api/platform/locks/clear", {"email": "x@y.z"})]
    await as_admin(client, cust)
    for path in reads:
        assert (await client.get(path)).status_code == 403, path
    for method, path, body in writes:
        assert (await client.request(method, path, json=body)).status_code == 403, path
    assert "/platform" not in (await client.get("/")).text
    await as_admin(client, noc)
    for path in reads:
        assert (await client.get(path)).status_code == 200, path
    for method, path, body in writes:
        assert (await client.request(method, path, json=body)).status_code == 403, path


async def test_recent_attempts_filter(client, twg):
    await fail(client, 2, ATTACKER, email="a@example.com")
    await attempt(client, twg[1].email, PASSWORD, "198.51.100.30")
    await as_admin(client, twg[1])
    ev = (await client.get(f"/api/platform/auth-events?ip={ATTACKER}")).json()
    assert len(ev) == 2 and all(e["outcome"] == "failure" and e["reason"] == "unknown email" for e in ev)
    ok = (await client.get("/api/platform/auth-events?outcome=success")).json()
    assert {e["ip"] for e in ok} == {"198.51.100.30", "198.51.100.20"} and ok[0]["company"] == "TWG Security"


# ---------------------------------------------------------------- Cloudflare
async def cf_on(client, admin):
    await as_admin(client, admin)
    r = await client.put("/api/platform/settings/cloudflare", json={"enabled": True, "zone_id": "a" * 32,
                                                                     "api_token": "T" * 40})
    assert r.status_code == 200, r.text
    assert "T" * 40 not in r.text and r.json()["cloudflare"]["token_set"]


@respx.mock
async def test_cloudflare_push_and_remove(client, session, twg):
    await cf_on(client, twg[1])
    row = await session.get(PlatformSettings, 1)
    assert row.cf_api_token_enc and "TTTT" not in row.cf_api_token_enc            # encrypted at rest
    log = await session.scalar(select(AuditLog).where(AuditLog.action == "platform.settings"))
    assert "TTTT" not in str(log.detail) and "cf_api_token" in log.detail["fields"]
    push = respx.post(CF).mock(return_value=httpx.Response(200, json={"success": True, "result": {"id": "rule-1"}}))
    await fail(client, 5, ATTACKER)
    await guard.drain()
    req = push.calls.last.request
    assert req.headers["authorization"] == "Bearer " + "T" * 40
    body = __import__("json").loads(req.content)
    assert body["mode"] == "block" and body["configuration"] == {"target": "ip", "value": ATTACKER}
    assert body["notes"].startswith("twg-alarm-portal auto-ban: 5 failed login attempts")
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await session.refresh(ban)
    assert ban.cf_rule_id == "rule-1"
    delete = respx.delete(f"{CF}/rule-1").mock(return_value=httpx.Response(200, json={"success": True, "result": {"id": "rule-1"}}))
    await as_admin(client, twg[1])
    await client.delete(f"/api/platform/bans/{ban.id}")
    await guard.drain()
    assert delete.called
    await session.refresh(ban)
    assert ban.cf_rule_id == ""


@respx.mock
async def test_cloudflare_duplicate_is_adopted(client, session, twg):
    await cf_on(client, twg[1])
    respx.post(CF).mock(return_value=httpx.Response(400, json={"success": False, "errors": [
        {"code": 10009, "message": "firewallaccessrules.api.duplicate_of_existing"}]}))
    respx.get(CF).mock(return_value=httpx.Response(200, json={"success": True, "result": [
        {"id": "manual-1", "notes": "blocked by hand"}, {"id": "ours-9", "notes": "twg-alarm-portal auto-ban: old"}]}))
    await fail(client, 5, ATTACKER)
    await guard.drain()
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await session.refresh(ban)
    assert ban.cf_rule_id == "ours-9"


@respx.mock
async def test_cloudflare_down_never_affects_the_local_ban_and_is_retried(client, session, twg):
    await cf_on(client, twg[1])
    route = respx.post(CF).mock(side_effect=httpx.ConnectError("down"))
    r = await fail(client, 5, ATTACKER)
    assert r.status_code == 403                                     # banned locally regardless
    await guard.drain()
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await session.refresh(ban)
    assert ban.cf_rule_id == ""
    await as_admin(client, twg[1])
    status = (await client.get("/api/platform/settings")).json()["cloudflare"]
    assert "unreachable" in status["last_error"]
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 403
    route.mock(return_value=httpx.Response(200, json={"success": True, "result": {"id": "rule-2"}}))
    await guard.sweep()                                              # the 60 s sweeper catches up
    await session.refresh(ban)
    assert ban.cf_rule_id == "rule-2"
    assert (await client.get("/api/platform/settings")).json()["cloudflare"]["last_error"] == ""


@respx.mock
async def test_expired_bans_are_removed_at_cloudflare_by_the_sweep(client, session, twg):
    await cf_on(client, twg[1])
    respx.post(CF).mock(return_value=httpx.Response(200, json={"success": True, "result": {"id": "rule-3"}}))
    await fail(client, 5, ATTACKER)
    await guard.drain()
    await expire_ban(session, ATTACKER)
    gone = respx.delete(f"{CF}/rule-3").mock(return_value=httpx.Response(404, json={"success": False, "errors": [
        {"code": 10001, "message": "not found"}]}))
    await guard.sweep()
    assert gone.called
    ban = await session.scalar(select(IpBan).where(IpBan.ip == ATTACKER))
    await session.refresh(ban)
    assert ban.cf_rule_id == "" and ban.ban_count == 1                # history kept for escalation


@respx.mock
async def test_cloudflare_test_button_and_validation(client, twg):
    await as_admin(client, twg[1])
    assert (await client.put("/api/platform/settings/cloudflare", json={"enabled": True, "zone_id": "a" * 32})).status_code == 400
    assert (await client.put("/api/platform/settings/cloudflare", json={"enabled": False, "zone_id": "xyz"})).status_code == 400
    assert not (await client.post("/api/platform/cloudflare/test")).json()["ok"]
    await cf_on(client, twg[1])
    respx.get(CF).mock(return_value=httpx.Response(200, json={"success": True, "result": [
        {"id": "1", "notes": "twg-alarm-portal auto-ban: x"}, {"id": "2", "notes": ""}], "result_info": {"total_count": 2}}))
    assert (await client.post("/api/platform/cloudflare/test")).json() == {"ok": True, "rules": 2, "ours": 1}
    respx.get(CF).mock(return_value=httpx.Response(403, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}))
    r = (await client.post("/api/platform/cloudflare/test")).json()
    assert not r["ok"] and "Authentication error" in r["error"]
    # Remove the token: it's gone, and switching on again needs a new one.
    r = await client.put("/api/platform/settings/cloudflare", json={"enabled": False, "zone_id": "a" * 32, "api_token": ""})
    assert not r.json()["cloudflare"]["token_set"]


async def test_cli_unban_allow_and_clear_lock(client, session, twg, capsys):
    from app import cli
    await fail(client, 5, ATTACKER)
    await cli.unban_ip(ATTACKER)
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 200
    await cli.allow_ip("192.0.2.0/28", "office")
    platform_settings.reset_cache()
    await platform_settings.refresh()
    assert guard.protection("192.0.2.5").startswith("allowlisted")
    await cli.clear_lock("Someone@Example.com")
    ev = await session.scalar(select(AuthEvent).where(AuthEvent.kind == "admin", AuthEvent.email == "someone@example.com"))
    assert ev.outcome == "cleared"
    assert "Unbanned 203.0.113.7" in capsys.readouterr().out


async def test_ban_check_fails_open(monkeypatch, client, twg):
    await fail(client, 5, ATTACKER)

    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(guard, "sessionmaker", broken)
    assert (await client.get("/login", headers=via(ATTACKER))).status_code == 200

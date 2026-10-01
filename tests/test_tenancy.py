"""Multi-company isolation and TWG's platform views (app/scope.py).

The platform company (TWG) plus two customer companies, A and B. Customers must never see or touch
each other's data; TWG sees all of it, and changes it only with platform.support (audit-logged in the
customer's own log).
"""
import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import AuditLog, Site, Tenant, User, UserGroup, UserGroupMember
from app.security import hash_password
from app.services.poller import ingest
from tests.conftest import PASSWORD, login, make_site, nx_row


async def _as(client, email):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    await login(client, email)


async def _company(session, name, kind="customer", *users):
    t = Tenant(name=name, kind=kind)
    session.add(t)
    await session.flush()
    out = []
    for email, role in users:
        u = User(tenant_id=t.id, email=email, display_name=email.split("@")[0], password_hash=hash_password(PASSWORD),
                 role=role)
        session.add(u)
        out.append(u)
    await session.commit()
    return t, out


@pytest.fixture
async def world(session):
    twg, (twg_admin, twg_viewer) = await _company(session, "TWG Security", "platform",
                                                  ("boss@twgsecurity.com", "admin"), ("noc@twgsecurity.com", "operator"))
    a, (a_admin,) = await _company(session, "Acme Security", "customer", ("admin@acme.test", "admin"))
    b, (b_admin, b_op) = await _company(session, "Bravo Guards", "customer",
                                        ("admin@bravo.test", "admin"), ("op@bravo.test", "operator"))
    # TWG's NOC operator may look at every company but not change them.
    g = UserGroup(tenant_id=twg.id, name="NOC", permissions=["platform.view"])
    session.add(g)
    await session.flush()
    session.add(UserGroupMember(group_id=g.id, user_id=twg_viewer.id))
    sites, alarms = {}, {}
    for t in (twg, a, b):
        s = await make_site(session, t, name=f"{t.name} HQ")
        [al] = await ingest(session, s, [nx_row(4_000_000 + t.id, action_id=f"x{t.id}", device="dev-1")], {})
        await session.commit()
        sites[t.name], alarms[t.name] = s, al
    return {"twg": twg, "a": a, "b": b, "twg_admin": twg_admin, "twg_viewer": twg_viewer, "a_admin": a_admin,
            "b_admin": b_admin, "b_op": b_op, "sites": sites, "alarms": alarms}


async def test_customers_never_see_or_touch_each_other(client, session, world):
    sa, sb = world["sites"]["Acme Security"], world["sites"]["Bravo Guards"]
    aa, ab = world["alarms"]["Acme Security"], world["alarms"]["Bravo Guards"]
    await _as(client, world["a_admin"].email)

    assert [s["id"] for s in (await client.get("/api/sites")).json()] == [sa.id]
    for q in ("state=all", "state=open", "state=open&alerting=1", f"state=all&tenant_id={world['b'].id}"):
        ids = [x["id"] for x in (await client.get(f"/api/alarms?{q}")).json()]
        assert ab.id not in ids and (ids == [aa.id] or "tenant_id" in q), q
    assert (await client.get("/api/summary")).json()["open_alarm"] + (await client.get("/api/summary")).json()["open_critical"] == 1
    assert all(r["tenant_id"] == world["a"].id for r in (await client.get("/api/audit")).json())
    assert {u["email"] for u in (await client.get("/api/users")).json()} == {"admin@acme.test"}

    # Every way of reaching Bravo's things by id answers 404 (not 403: don't confirm they exist).
    for method, path, body in [
        ("GET", f"/api/alarms/{ab.id}", None), ("GET", f"/api/alarms/{ab.id}/notes", None),
        ("POST", f"/api/alarms/{ab.id}/ack", {"verdict": "false"}), ("POST", f"/api/alarms/{ab.id}/notes", {"text": "x"}),
        ("POST", f"/api/sites/{sb.id}/disarm", {}), ("POST", f"/api/sites/{sb.id}/arm", {}),
        ("PUT", f"/api/sites/{sb.id}", {"name": "x", "host": "https://x.test", "nx_user": "x"}),
        ("POST", f"/api/sites/{sb.id}/disable", None), ("POST", f"/api/sites/{sb.id}/archive", None),
        ("POST", "/api/sites/test", {"host": "https://x.test", "nx_user": "x", "site_id": sb.id}),
        ("PUT", f"/api/users/{world['b_op'].id}", {"is_active": False}),
        ("GET", f"/api/users/{world['b_op'].id}/security", None),
        ("POST", f"/api/users/{world['b_op'].id}/invite", None),
        ("POST", f"/api/users/{world['b_op'].id}/reset-2fa", {}),
        ("DELETE", f"/api/users/{world['b_op'].id}/sessions", None),
        ("DELETE", f"/api/users/{world['b_op'].id}/sessions/x", None),
        ("GET", f"/media/alarms/{ab.id}/snapshot.jpg", None), ("GET", f"/media/alarms/{ab.id}/live.jpg", None),
        ("GET", f"/media/alarms/{ab.id}/live.webm", None), ("GET", f"/api/alarms/{ab.id}/clip", None),
        ("GET", f"/api/alarms/{ab.id}/objects", None), ("GET", f"/api/alarms/{ab.id}/export", None),
        ("GET", f"/media/clips/a{ab.id}_10_20_sd.mp4", None), ("GET", f"/sites/{sb.id}/edit", None),
        ("GET", f"/branding/{world['b'].id}/logo", None), ("PUT", f"/api/tenants/{world['b'].id}", {"display_name": "x"}),
    ]:
        r = await client.request(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
    r = await client.post("/api/alarms/bulk", json={"ids": [ab.id], "verdict": "false"})
    assert r.json()["unchanged"] == [ab.id]

    # Bravo's data is untouched.
    await session.refresh(ab)
    await session.refresh(sb)
    assert (ab.state, sb.enabled, sb.armed) == ("new", True, True)
    # And the platform screens are TWG-only.
    for path in ("/api/tenants", "/companies", "/platform", "/api/platform/settings", "/api/platform/security",
                 "/api/platform/auth-events", "/api/platform/email-log"):
        assert (await client.get(path)).status_code == 403, path
    for method, path in (("PUT", "/api/platform/settings/sign-in"), ("PUT", "/api/platform/settings/proxies"),
                         ("PUT", "/api/platform/settings/cloudflare"), ("POST", "/api/platform/cloudflare/test"),
                         ("POST", "/api/platform/bans"), ("DELETE", "/api/platform/bans/1"),
                         ("POST", "/api/platform/allowlist"), ("DELETE", "/api/platform/allowlist/1"),
                         ("POST", "/api/platform/locks/clear"), ("PUT", "/api/platform/settings/two-factor"),
                         ("PUT", "/api/platform/settings/sessions"), ("PUT", "/api/platform/settings/passwords"),
                         ("PUT", "/api/platform/settings/email"), ("POST", "/api/platform/email/test")):
        assert (await client.request(method, path, json={})).status_code in (403, 422), path
    assert (await client.post("/api/scope", json={"scope": "all"})).status_code == 403


async def test_customer_admins_cannot_hold_or_grant_platform_permissions(client, session, world):
    await _as(client, world["a_admin"].email)
    page = (await client.get("/")).text
    assert "platform." not in page.split("window.PORTAL")[1].split("</script>")[0]
    payload = (await client.get("/api/groups")).json()
    assert not any(p.startswith("platform.") for p in payload["permissions"])
    r = await client.post("/api/groups", json={"name": "Sneaky", "permissions": ["platform.view"]})
    assert r.status_code == 400
    # A group can only hold the company's own users.
    r = await client.post("/api/groups", json={"name": "Mixed", "member_ids": [world["a_admin"].id, world["b_op"].id]})
    assert r.json()["groups"][0]["member_ids"] == [world["a_admin"].id]


async def test_twg_views_own_all_or_one_company(client, session, world):
    await _as(client, world["twg_viewer"].email)
    names = lambda: sorted(s["tenant_name"] for s in client_sites)  # noqa: E731
    client_sites = (await client.get("/api/sites")).json()
    assert names() == ["TWG Security"]                                   # default: own company
    assert (await client.post("/api/scope", json={"scope": "all"})).status_code == 200
    client_sites = (await client.get("/api/sites")).json()
    assert names() == ["Acme Security", "Bravo Guards", "TWG Security"]
    assert len((await client.get("/api/alarms?state=all")).json()) == 3
    await client.post("/api/scope", json={"scope": str(world["a"].id)})
    client_sites = (await client.get("/api/sites")).json()
    assert names() == ["Acme Security"]
    # Viewing Acme is recorded in Acme's own audit log.
    seen = await session.scalar(select(AuditLog).where(AuditLog.tenant_id == world["a"].id, AuditLog.action == "support.viewed"))
    assert seen.user_id == world["twg_viewer"].id


async def test_twg_view_only_cannot_change_customers(client, session, world):
    aa, sa = world["alarms"]["Acme Security"], world["sites"]["Acme Security"]
    await _as(client, world["twg_viewer"].email)
    await client.post("/api/scope", json={"scope": str(world["a"].id)})
    assert (await client.get(f"/api/alarms/{aa.id}")).status_code == 200
    assert (await client.post(f"/api/alarms/{aa.id}/ack", json={"verdict": "real"})).status_code == 403
    assert (await client.post(f"/api/sites/{sa.id}/disarm", json={})).status_code == 403
    assert (await client.post(f"/api/alarms/{aa.id}/notes", json={"text": "hi"})).status_code == 403


async def test_twg_support_acts_and_the_customer_sees_it(client, session, world):
    aa, sa = world["alarms"]["Acme Security"], world["sites"]["Acme Security"]
    await _as(client, world["twg_admin"].email)
    await client.post("/api/scope", json={"scope": str(world["a"].id)})
    r = await client.post(f"/api/alarms/{aa.id}/ack", json={"verdict": "false", "note": "Checked for Acme"})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/api/sites/{sa.id}/disarm", json={"minutes": 30})).status_code == 200
    r = await client.post("/api/users", json={"email": "night@acme.test", "password": "A-long-password-1!", "role": "operator"})
    assert r.status_code == 200
    assert (await session.scalar(select(User).where(User.email == "night@acme.test"))).tenant_id == world["a"].id
    # In the All companies view, creating needs a single company.
    await client.post("/api/scope", json={"scope": "all"})
    r = await client.post("/api/sites", json={"name": "x", "host": "https://x.test", "nx_user": "x", "nx_pass": "y"})
    assert r.status_code == 400 and "Pick a company" in r.text
    assert (await client.get("/api/users")).status_code == 400            # per-company pages need one company

    await _as(client, world["a_admin"].email)
    log = (await client.get("/api/audit")).json()
    mine = {r["action"]: r for r in log if r["user"] == world["twg_admin"].display_name}
    assert {"alarm.acknowledged", "site.disarmed", "user.created", "support.viewed"} <= set(mine)
    assert all(r["support"] for r in mine.values())
    assert not any(r["support"] for r in log if r["user"] == world["a_admin"].display_name)


async def test_own_alarms_keep_reaching_twg_while_viewing_a_customer(client, session, world):
    own, aa = world["alarms"]["TWG Security"], world["alarms"]["Acme Security"]
    await _as(client, world["twg_viewer"].email)
    await client.post("/api/scope", json={"scope": str(world["a"].id)})
    assert [x["id"] for x in (await client.get("/api/alarms?state=open")).json()] == [aa.id]       # the view
    store = {x["id"] for x in (await client.get("/api/alarms?state=open&alerting=1")).json()}      # the sound/pop-up store
    assert store == {aa.id, own.id}
    assert (await client.get(f"/api/alarms/{own.id}")).status_code == 200                         # pop-up can open it
    assert (await client.post(f"/api/alarms/{own.id}/ack", json={"verdict": "real"})).status_code == 200


async def test_disabled_company_cannot_sign_in_but_stays_monitored(client, session, world):
    sb = world["sites"]["Bravo Guards"]
    await _as(client, world["b_op"].email)                        # a session that's already open
    bravo_cookies = dict(client.cookies)
    await _as(client, world["twg_admin"].email)
    assert (await client.put(f"/api/tenants/{world['twg'].id}", json={"is_active": False})).status_code == 400
    r = await client.put(f"/api/tenants/{world['b'].id}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False

    client.cookies.clear()
    client.cookies.update(bravo_cookies)
    assert (await client.get("/api/sites")).status_code == 401    # the open session is cut off
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    page = await client.get("/login")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = await client.post("/login", data={"email": world["b_op"].email, "password": PASSWORD, "csrf_token": csrf})
    assert r.status_code == 200 and "portal access is disabled" in r.text
    await session.refresh(sb)
    assert sb.enabled and sb.archived_at is None                  # its sites are still monitored


async def test_create_company_branding_and_logo_rules(client, session, world):
    await _as(client, world["twg_admin"].email)
    r = await client.post("/api/tenants", json={"name": "Charlie Patrol", "admin_email": "boss@charlie.test",
                                                "admin_password": PASSWORD, "display_name": "Charlie"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert (await client.post("/api/tenants", json={"name": "Charlie Patrol", "admin_email": "z@z.test",
                                                    "admin_password": "Charlie-pass-1234!"})).status_code == 409
    stats = {t["name"]: t for t in (await client.get("/api/tenants")).json()}
    assert stats["Acme Security"]["sites"] == 1 and stats["Acme Security"]["open_alarm"] + stats["Acme Security"]["open_critical"] == 1
    assert stats["Charlie Patrol"]["sites"] == 0 and stats["TWG Security"]["kind"] == "platform"

    def png(size=(1200, 400), fmt="PNG"):
        buf = io.BytesIO()
        Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
        return buf.getvalue()

    await _as(client, "boss@charlie.test")
    assert (await client.get("/api/sites")).json() == []                  # a new company starts empty
    r = await client.post(f"/api/tenants/{cid}/logo", files={"file": ("logo.png", png(), "image/png")})
    assert r.status_code == 200 and r.json()["width"] <= 600 and r.json()["height"] <= 160
    logo = await client.get(f"/branding/{cid}/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert f"/branding/{cid}/logo" in (await client.get("/")).text and "Powered by" in (await client.get("/")).text
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert (await client.post(f"/api/tenants/{cid}/logo", files={"file": ("x.svg", svg, "image/svg+xml")})).status_code == 400
    assert (await client.post(f"/api/tenants/{world['a'].id}/logo", files={"file": ("l.png", png(), "image/png")})).status_code == 404
    assert (await client.put(f"/api/tenants/{cid}", json={"is_active": False})).status_code == 403
    await _as(client, world["a_admin"].email)
    assert (await client.get(f"/branding/{cid}/logo")).status_code == 404

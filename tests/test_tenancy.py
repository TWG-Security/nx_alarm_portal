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
    sb.camera_places = {"cam-b": {"name": "Bravo cam", "lat": 40, "lng": -75, "heading": 0, "fov": 60, "range_m": 40}}
    from app.models import Tracker
    bt = Tracker(tenant_id=world["b"].id, name="Bravo guard", source="phone", user_id=world["b_op"].id, lat=40, lng=-75)
    session.add(bt)
    await session.commit()
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
        ("POST", f"/api/users/{world['b_op'].id}/test-text", None),
        ("PUT", f"/api/users/{world['b_op'].id}", {"phone": "5705551234", "sms_level": "alarm"}),
        ("DELETE", f"/api/users/{world['b_op'].id}", None),
        ("GET", f"/media/alarms/{ab.id}/snapshot.jpg", None), ("GET", f"/media/alarms/{ab.id}/live.jpg", None),
        ("GET", f"/media/alarms/{ab.id}/live.webm", None), ("GET", f"/api/alarms/{ab.id}/clip", None),
        ("GET", f"/api/alarms/{ab.id}/objects", None), ("GET", f"/api/alarms/{ab.id}/export", None),
        ("GET", f"/media/clips/a{ab.id}_10_20_sd.mp4", None), ("GET", f"/sites/{sb.id}/edit", None),
        ("GET", f"/branding/{world['b'].id}/logo", None), ("PUT", f"/api/tenants/{world['b'].id}", {"display_name": "x"}),
        ("GET", f"/api/sites/{sb.id}/cameras", None),
        ("PUT", f"/api/sites/{sb.id}/cameras/cam-b", {"lat": 40, "lng": -75, "heading": 90}),
        ("DELETE", f"/api/sites/{sb.id}/cameras/cam-b", None),
        ("GET", f"/media/sites/{sb.id}/cameras/cam-b/live.jpg", None),
        ("GET", f"/api/sites/{sb.id}/model", None), ("POST", f"/api/sites/{sb.id}/model/uploads", {"name": "a.zip", "size": 9}),
        ("PUT", f"/api/sites/{sb.id}/model/placement", {"lat": 40, "lng": -75, "height": 0, "heading": 0, "scale": 1}),
        ("POST", f"/api/sites/{sb.id}/model/rebuild", {"up": "y"}), ("DELETE", f"/api/sites/{sb.id}/model", None),
        ("GET", f"/media/sites/{sb.id}/model/1/tiles/tileset.json", None),
        ("GET", f"/media/sites/{sb.id}/cameras/cam-b/live.webm", None),
        ("GET", f"/api/alarms/{ab.id}/guards", None), ("POST", f"/api/alarms/{ab.id}/dispatch", {"tracker_id": bt.id}),
        ("GET", f"/api/trackers/{bt.id}/trail?start=2026-01-01T00:00:00Z", None), ("POST", f"/api/trackers/{bt.id}/refresh", None), ("POST", f"/api/trackers/{bt.id}/locate", None), ("GET", f"/api/sites/{sb.id}/rules", None), ("POST", f"/api/sites/{sb.id}/check", None), ("PATCH", f"/api/sites/{sb.id}/rules/4c1d77c5-5f11-4567-8cfa-6db360231186", {"level": "alarm"}),
        ("GET", f"/api/sites/{sb.id}/rules/catalog", None), ("GET", f"/api/sites/{sb.id}/rules/cameras/cam-b/still.jpg", None), ("GET", f"/api/sites/{sb.id}/rules/4c1d77c5-5f11-4567-8cfa-6db360231186", None),
        ("PUT", f"/api/sites/{sb.id}/rules/4c1d77c5-5f11-4567-8cfa-6db360231186", {"enabled": False}),
        ("POST", f"/api/sites/{sb.id}/rules", {"event": {"type": "motion"}, "action": {"type": "writeToLog"}}),
        ("DELETE", f"/api/sites/{sb.id}/rules/4c1d77c5-5f11-4567-8cfa-6db360231186", None),
        ("PUT", f"/api/tracking/trackers/{bt.id}", {"name": "x"}), ("DELETE", f"/api/tracking/trackers/{bt.id}", None),
        ("POST", f"/api/tracking/trackers/{bt.id}/share", {"renew": True}),
    ]:
        r = await client.request(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
    # Their people can't be linked to our guards or tracked by our company.
    assert (await client.post("/api/tracking/phones", json={"user_id": world["b_op"].id})).status_code == 400
    r = await client.post("/api/alarms/bulk", json={"ids": [ab.id], "verdict": "false"})
    assert r.json()["unchanged"] == [ab.id]

    # Bravo's data is untouched.
    await session.refresh(ab)
    await session.refresh(sb)
    assert (ab.state, sb.enabled, sb.armed) == ("new", True, True)
    assert list(sb.camera_places) == ["cam-b"]
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


async def test_iphone_app_tokens_see_only_their_own_company(client, session, world):
    """The iPhone app (Bearer device tokens, app/routers/mobile.py): always the user's own company, no switcher,
    and every way into another company's things answers 404 as on the website."""
    sb, ab, aa = world["sites"]["Bravo Guards"], world["alarms"]["Bravo Guards"], world["alarms"]["Acme Security"]
    twg_alarm = world["alarms"]["TWG Security"]
    a_email, viewer_email, a_id = world["a_admin"].email, world["twg_viewer"].email, world["a"].id

    async def token(email):
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        r = await client.post("/api/mobile/login", json={"email": email, "password": PASSWORD, "device_name": "test"})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    h = await token(a_email)
    assert (await client.get("/api/mobile/me", headers=h)).json()["company"]["id"] == a_id
    assert [s["name"] for s in (await client.get("/api/sites", headers=h)).json()] == ["Acme Security HQ"]
    for q in ("state=all", "state=open", "state=open&alerting=1", f"state=all&tenant_id={world['b'].id}"):
        ids = [x["id"] for x in (await client.get(f"/api/alarms?{q}", headers=h)).json()]
        assert ids == ([aa.id] if "tenant_id" not in q else []), q
    for method, path, body in [
        ("GET", f"/api/alarms/{ab.id}", None), ("GET", f"/api/alarms/{ab.id}/notes", None),
        ("GET", f"/api/alarms/{ab.id}/timeline", None), ("POST", f"/api/alarms/{ab.id}/ack", {"verdict": "false"}),
        ("POST", f"/api/alarms/{ab.id}/notes", {"text": "x"}), ("POST", f"/api/alarms/{ab.id}/end-call", {}),
        ("GET", f"/api/alarms/{ab.id}/guards", None), ("POST", f"/api/alarms/{ab.id}/dispatch", {"tracker_ids": [1]}),
        ("GET", f"/media/alarms/{ab.id}/snapshot.jpg", None), ("GET", f"/media/alarms/{ab.id}/live.jpg", None),
        ("GET", f"/api/alarms/{ab.id}/clip", None), ("GET", f"/api/sites/{sb.id}/cameras", None),
        ("POST", f"/api/sites/{sb.id}/disarm", {}),
    ]:
        r = await client.request(method, path, json=body, headers=h)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])

    # TWG's view-everything operator: the app shows TWG's own company only, even after asking for "all".
    h = await token(viewer_email)
    await client.post("/api/scope", json={"scope": "all"}, headers=h)
    ids = [x["id"] for x in (await client.get("/api/alarms?state=all", headers=h)).json()]
    assert ids == [twg_alarm.id]
    assert (await client.get(f"/api/alarms/{aa.id}", headers=h)).status_code == 404


async def test_guard_dispatch_endpoints_stay_inside_their_company(client, session, world):
    """GET /api/mobile/dispatches lists only the caller's own dispatch links in their own company; GET /api/a/{token}
    answers only for a real token (the token is the key), and an unknown one counts as a failed sign-in."""
    from datetime import timedelta
    from app import alarm_links
    from app.models import AuthEvent
    ab = world["alarms"]["Bravo Guards"]
    b_op_id, a_email, b_email = world["b_op"].id, world["a_admin"].email, world["b_op"].email
    ab.call_state = "active"
    b_op = await session.get(User, b_op_id)
    token = await alarm_links.mint(session, ab, b_op, 24, purpose="dispatch")
    await session.commit()

    async def bearer_for(email):
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        r = await client.post("/api/mobile/login", json={"email": email, "password": PASSWORD, "device_name": "t"})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    assert (await client.get("/api/mobile/dispatches", headers=await bearer_for(a_email))).json() == []
    mine = (await client.get("/api/mobile/dispatches", headers=await bearer_for(b_email))).json()
    assert [(d["alarm_id"], d["link_token"]) for d in mine] == [(ab.id, token)]
    assert (await client.get("/api/mobile/dispatches")).status_code == 401           # no sign-in
    client.cookies.clear()
    assert (await client.get(f"/api/a/{token}")).json()["alarm"]["id"] == ab.id
    before = len((await session.scalars(select(AuthEvent).where(AuthEvent.kind == "alarm_link"))).all())
    assert (await client.get(f"/api/a/{token[:-2]}xx")).status_code == 404
    after = len((await session.scalars(select(AuthEvent).where(AuthEvent.kind == "alarm_link"))).all())
    assert after == before + 1


async def test_iphone_app_passkey_sign_in_stays_in_its_company(client, session, world):
    """A customer's passkey sign-in from the app (POST /api/mobile/passkey/options + verify) gives a token for
    their own company only; a second factor can't be answered with another company's passkey."""
    from tests.test_two_factor import add_passkey, soft_key
    a_email, b_email, a_id = world["a_admin"].email, world["b_admin"].email, world["a"].id
    aa, ab = world["alarms"]["Acme Security"], world["alarms"]["Bravo Guards"]
    keys = {}
    for email in (a_email, b_email):
        keys[email] = soft_key()
        await _as(client, email)
        await add_passkey(client, keys[email])
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    o = (await client.post("/api/mobile/passkey/options", json={"device_name": "t"})).json()
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": keys[a_email].sign(o["options"])})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert r.json()["user"]["company"]["id"] == a_id
    assert [x["id"] for x in (await client.get("/api/alarms?state=all", headers=h)).json()] == [aa.id]
    assert (await client.get(f"/api/alarms/{ab.id}", headers=h)).status_code == 404
    # Acme's password step, answered with Bravo's passkey: refused
    from app import mfa
    from sqlalchemy import update
    await session.execute(update(User).where(User.email == a_email).values(totp_enabled=True, totp_secret_enc=mfa.new_totp_secret()))
    await session.commit()
    ch = (await client.post("/api/mobile/login", json={"email": a_email, "password": PASSWORD, "device_name": "t"})).json()["challenge"]
    o = (await client.post("/api/mobile/passkey/options", json={"challenge": ch})).json()
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": keys[b_email].sign(o["options"])})
    assert r.status_code == 400 and "token" not in r.json()


async def test_iphone_push_stays_inside_its_company(client, session, world, tmp_path, monkeypatch):
    """App notifications (app/apns.py) go only to the alarm's own company's phones; a phone can only be removed by its
    owner; push settings are per person (PUT /api/mobile/me/push changes only the caller)."""
    import httpx
    import respx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from app import apns, sms
    from app.config import get_settings
    from app.models import PushDevice
    path = tmp_path / "k.p8"
    path.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for k, v in {"apns_key_path": str(path), "apns_key_id": "K1", "apns_team_id": "NY8H9358QD", "apns_api_base": "https://apns.test"}.items():
        monkeypatch.setattr(get_settings(), k, v)
    apns.reset()
    aa, ab = world["alarms"]["Acme Security"], world["alarms"]["Bravo Guards"]
    a_email, b_email = world["a_admin"].email, world["b_admin"].email
    tokens = {}
    for email, tok in ((a_email, "a" * 64), (b_email, "b" * 64)):
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        t = (await client.post("/api/mobile/login", json={"email": email, "password": PASSWORD, "device_name": "t"})).json()["token"]
        tokens[email] = {"Authorization": f"Bearer {t}"}
        assert (await client.post("/api/mobile/devices", headers=tokens[email], json={"apns_token": tok, "environment": "sandbox"})).status_code == 200
    with respx.mock:
        route = respx.post(url__startswith="https://apns.test/3/device/").mock(return_value=httpx.Response(200))
        for a in (aa, ab):
            a.state = "new"
        await session.commit()
        apns.notify([aa])
        await sms.drain()
        assert [c.request.url.path.rsplit("/", 1)[-1] for c in route.calls] == ["a" * 64]       # Acme's alarm: Acme's phone only
    # Bravo can't remove Acme's phone; settings change only the caller
    r = await client.delete(f"/api/mobile/devices/{'a' * 64}", headers=tokens[b_email])
    assert r.json() == {"removed": False}
    assert len((await session.scalars(select(PushDevice))).all()) == 2
    await client.put("/api/mobile/me/push", headers=tokens[b_email], json={"push_level": "off"})
    session.expire_all()
    a_user = await session.scalar(select(User).where(User.email == a_email))
    assert a_user.push_level is None
    apns.reset()


async def test_on_duty_share_token_is_in_the_callers_company(client, session, world):
    """POST /api/mobile/share makes / finds the caller's phone tracker in THEIR company only."""
    from app.models import Tracker
    a_email, a_id = world["a_admin"].email, world["a"].id
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    t = (await client.post("/api/mobile/login", json={"email": a_email, "password": PASSWORD, "device_name": "t"})).json()["token"]
    r = await client.post("/api/mobile/share", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    rows = (await session.scalars(select(Tracker).where(Tracker.source == "phone"))).all()
    assert [x.tenant_id for x in rows] == [a_id]

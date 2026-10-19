"""Sign-in for the iPhone app (app/routers/mobile.py, docs/IOS_APP_BRIEF.md 6.1) and device tokens as Bearer.
The stream's immediate ping and SIGNED OUT are checked against a running server (httpx's ASGI transport buffers
whole responses, so an endless SSE stream can't be read here)."""
from datetime import timedelta

from sqlalchemy import select

from app import sessions
from app.models import AuditLog, AuthEvent, Tracker, User, UserSession
from tests.conftest import PASSWORD, login
from tests.test_two_factor import enable_totp, now_code, settings_row, twg  # noqa: F401 - fixture
from tests.test_sms import make_alarm, now_ms
from tests.conftest import make_site

DEVICE = "iPhone · iOS 27.0 · TWG Alarms 0.1"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def app_login(client, email, password=PASSWORD, ip=None):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    return await client.post("/api/mobile/login", json={"email": email, "password": password, "device_name": DEVICE},
                             headers={"CF-Connecting-IP": ip} if ip else {})


async def test_sign_in_me_api_and_sign_out(client, session, twg):
    t, u = twg
    site = await make_site(session, t)
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    session.add(Tracker(tenant_id=t.id, name="Op's car", kind="guard", user_id=u.id))
    await session.commit()
    r = await app_login(client, u.email)
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["token"]
    assert token.startswith("twgm_") and set(body) == {"token", "user"}
    me = body["user"]
    assert me["email"] == u.email and me["role"] == "admin" and me["company"] == {"id": t.id, "name": "TWG Security"}
    assert me["is_guard"] is True and me["monitoring_station"] is False and me["sms_level"] == "off" and me["sms_when"] == "raised"
    assert "alarms.bulk_edit" in me["permissions"]
    assert client.cookies.get("twg_portal") is None                     # a token, not a cookie session

    assert (await client.get("/api/mobile/me", headers=bearer(token))).json() == me
    alarms = (await client.get("/api/alarms?state=open", headers=bearer(token))).json()
    assert [a["id"] for a in alarms] == [alarm.id]
    # Writes need no CSRF token with a Bearer device token
    r = await client.post(f"/api/alarms/{alarm.id}/notes", json={"text": "from the app"}, headers=bearer(token))
    assert r.status_code == 200 and r.json()["text"] == "from the app"
    # Only the hash is stored; the phone is listed as a signed-in device
    row = await session.scalar(select(UserSession).where(UserSession.method == "mobile"))
    assert row.id == sessions.mobile_id(token) and token not in row.id and row.user_agent == DEVICE
    login_row = await session.scalar(select(AuditLog).where(AuditLog.action == "login"))
    assert login_row.detail["method"] == "app (password)"

    await login(client, u.email)                                       # the website, as the same person
    listed = (await client.get("/api/account")).json()["sessions"]
    assert any(s["method"] == "mobile" and s["user_agent"] == DEVICE for s in listed)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)

    assert (await client.post("/api/mobile/logout", headers=bearer(token))).json() == {"signed_out": True}
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 401
    assert (await client.get("/api/alarms", headers=bearer(token))).status_code == 401
    assert (await client.get("/api/mobile/me", headers=bearer("twgm_made-up"))).status_code == 401


async def test_wrong_password_is_recorded_and_bans_like_the_website(client, session, twg):
    """Test requests come from 127.0.0.1 (a trusted proxy), so CF-Connecting-IP sets a public phone address."""
    t, u = twg
    email, ip = u.email, "198.51.100.77"
    r = await app_login(client, email, "wrong", ip)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
    r = await app_login(client, "nobody@x.test", "wrong", ip)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
    rows = (await session.scalars(select(AuthEvent).where(AuthEvent.outcome == "failure"))).all()
    assert len(rows) == 2 and all("(app)" in e.reason for e in rows)
    for _ in range(10):
        r = await app_login(client, "nobody@x.test", "wrong", ip)
        if r.status_code == 403:
            break
    assert r.status_code == 403 and "blocked" in r.json()["detail"]
    r = await app_login(client, email, PASSWORD, ip)                    # banned: even the right password
    assert r.status_code == 403 and "blocked" in r.json()["detail"]


async def test_two_step_code_recovery_code_and_limits(client, session, twg):
    t, u = twg
    email, uid = u.email, u.id
    secret, codes = await enable_totp(client, email)
    r = await app_login(client, email)
    assert r.status_code == 200 and r.json()["step"] == "totp" and r.json()["challenge"] and "token" not in r.json()
    ch = r.json()["challenge"]
    bad = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": "000000"})
    assert bad.status_code == 400 and bad.json()["detail"] == "That code didn't work."
    ok = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": now_code(secret, 30)})   # enable used this step
    assert ok.status_code == 200 and ok.json()["token"].startswith("twgm_") and ok.json()["user"]["id"] == uid
    again = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": now_code(secret)})
    assert again.status_code == 401                                     # single use

    ch = (await app_login(client, email)).json()["challenge"]            # a recovery code instead
    r = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": codes[0]})
    assert r.status_code == 200 and r.json()["token"]
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "mfa.recovery_used"))

    ch = (await app_login(client, email)).json()["challenge"]            # 5 wrong codes end the challenge
    for i in range(5):
        r = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": f"00000{i}"})
    assert r.status_code == 401 and "Too many" in r.json()["detail"]
    r = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": now_code(secret)})
    assert r.status_code == 401


async def test_required_two_step_not_set_up_says_enroll(client, session, twg):
    t, u = twg
    await settings_row(session, mfa_require_twg=True)
    try:
        r = await app_login(client, u.email)
        assert r.status_code == 200 and r.json()["step"] == "enroll" and "website" in r.json()["detail"] and "token" not in r.json()
    finally:
        await settings_row(session, mfa_require_twg=False)


async def test_token_ends_on_revoke_sign_out_everywhere_disable_and_long_disuse(client, session, twg):
    t, u = twg
    uid, email = u.id, u.email
    token = (await app_login(client, email)).json()["token"]
    # Revoked from the website's Account page
    await login(client, email)
    sid = sessions.mobile_id(token)
    assert (await client.delete(f"/api/account/sessions/{sid}")).status_code == 200
    client.cookies.clear()
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 401

    token = (await app_login(client, email)).json()["token"]
    await login(client, email)
    await client.post("/api/account/sign-out-everywhere")
    client.cookies.clear()
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 401

    # A phone idle 13 h is still signed in (a browser wouldn't be); idle past the app's limit it isn't
    token = (await app_login(client, email)).json()["token"]
    row = await session.get(UserSession, sessions.mobile_id(token))
    row.last_seen_at = sessions.utcnow() - timedelta(hours=13)
    await session.commit()
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 200
    session.expire_all()
    row = await session.get(UserSession, sessions.mobile_id(token))
    row.last_seen_at = sessions.utcnow() - timedelta(days=sessions.MOBILE_IDLE_DAYS + 1)
    await session.commit()
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 401
    session.expire_all()
    assert (await session.get(UserSession, sessions.mobile_id(token))).revoked_reason == "inactive"

    token = (await app_login(client, email)).json()["token"]
    user = await session.get(User, uid)
    user.is_active = False
    await session.commit()
    assert (await client.get("/api/mobile/me", headers=bearer(token))).status_code == 401


async def test_a_device_token_never_works_as_a_cookie_and_cookies_are_ignored(client, session, twg):
    t, u = twg
    token = (await app_login(client, u.email)).json()["token"]
    await login(client, u.email)                                       # a browser cookie too
    r = await client.get("/api/mobile/me", headers=bearer("twgm_wrong"))
    assert r.status_code == 401                                        # a bad token isn't rescued by the cookie
    assert (await client.post("/api/mobile/logout")).status_code == 400   # a cookie can't end a phone
    r = await client.get("/api/mobile/me", headers=bearer(token))
    assert r.status_code == 200

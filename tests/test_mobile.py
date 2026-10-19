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


# ---------------------------------------------------------------- the guard's side (brief 6.7)
import re  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402

from app import sms  # noqa: E402
from app.services.bus import bus  # noqa: E402
from tests.test_dispatch import track  # noqa: E402
from tests.test_sms import TWILIO, body_of, fresh_sms, guards, nx_ok, texts_on, world  # noqa: E402,F401


@respx.mock
async def test_guard_dispatch_list_link_json_and_personal_events(client, session):
    t, site, p = await world(session)
    g = await guards(session, t, site)
    await texts_on(session)
    nx_ok()
    twilio = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SMg"}))
    guard_tracker = await track(session, t.id, "Guard 1", 400, g["guard"])
    other_tracker = await track(session, t.id, "Both's car", 900, g["both"])
    site.camera_places = {"cam-1": {"name": "Gate", "lat": 40.001, "lng": -75.002, "heading": 0, "fov": 60, "range_m": 30}}
    await session.commit()
    a1 = await make_alarm(session, site, event_ts_ms=now_ms(), device_id="cam-1")
    a2 = await make_alarm(session, site, event_ts_ms=now_ms(), caption="Door forced")
    ids = (a1.id, a2.id)
    guard_sub, both_sub, admin_sub = bus.subscribe({t.id}), bus.subscribe({t.id}), bus.subscribe({t.id})

    await login(client, "mike@twgsecurity.com")                         # the dispatcher
    for aid in ids:
        assert (await client.post(f"/api/alarms/{aid}/dispatch", json={"tracker_id": guard_tracker.id})).status_code == 200
    await client.post(f"/api/alarms/{a2.id}/dispatch", json={"tracker_id": other_tracker.id})
    await sms.drain()
    sent = [body_of(c) for c in twilio.calls]
    token_1 = re.search(r"/a/([\w-]+)/", sent[0]).group(1)

    # The guard's app signs in and lists its calls: newest first, with each link token
    h = bearer((await app_login(client, "guard@twgsecurity.com")).json()["token"])
    mine = (await client.get("/api/mobile/dispatches", headers=h)).json()
    assert [d["alarm_id"] for d in mine] == [a2.id, a1.id]
    d1 = mine[1]
    assert d1["link_token"] == token_1 and d1["site_name"] == site.name and d1["status"] == ""
    assert (d1["lat"], d1["lng"]) == (40.001, -75.002)                      # the alarm camera's placed spot
    assert mine[0]["lat"] == site.lat and mine[0]["dispatched_at"]           # no placed camera: the site pin

    # The link as JSON: what the page shows; opening is logged once per 5 minutes, not on every refresh
    r = await client.get(f"/api/a/{token_1}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["alarm"]["id"] == a1.id and "nx_ack_result" not in j["alarm"] and j["sent_to"] == "guard"
    assert j["guard_status"] == "" and j["guard_notes"] == [] and (j["lat"], j["lng"]) == (40.001, -75.002)
    assert j["position_from"] == "camera" and j["expires_at"]
    await client.post(f"/api/a/{token_1}/status", json={"status": "en_route"})
    await client.post(f"/api/a/{token_1}/note", json={"text": "Two minutes out"})
    j = (await client.get(f"/api/a/{token_1}")).json()
    assert j["guard_status"] == "en_route" and [n["text"] for n in j["guard_notes"]] == ["Two minutes out"]
    assert (await client.get("/api/mobile/dispatches", headers=h)).json()[1]["status"] == "en_route"
    opens = (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.link_opened",
                                                          AuditLog.alarm_id == a1.id))).all()
    assert len(opens) == 1 and opens[0].detail["app"] is True
    assert (await client.get("/api/a/not-a-real-token")).status_code == 404

    # Live events: only the dispatched person's streams get them (they carry the link token)
    def events(sub, name):
        out = []
        while not sub.queue.empty():
            ev, data = sub.queue.get_nowait()
            if ev == name:
                out.append(data)
        return out
    dispatched = events(guard_sub, "dispatch")
    assert [(d["alarm_id"], d["user_ids"]) for d in dispatched][:2] == [(a1.id, [g["guard"].id]), (a2.id, [g["guard"].id])]
    assert dispatched[0]["link_token"] == token_1

    # A call ends (Real event, then End call): it leaves the list; call_ended goes to everyone dispatched to it
    await login(client, "mike@twgsecurity.com")
    await client.post(f"/api/alarms/{a2.id}/ack", json={"verdict": "real", "note": "ok"})
    assert (await client.post(f"/api/alarms/{a2.id}/end-call", json={"note": "clear"})).status_code == 200
    await sms.drain()
    ended = events(both_sub, "call_ended")
    assert ended and ended[-1]["alarm_id"] == a2.id and sorted(ended[-1]["user_ids"]) == sorted([g["guard"].id, g["both"].id])
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    assert [d["alarm_id"] for d in (await client.get("/api/mobile/dispatches", headers=h)).json()] == [a1.id]
    # False alarm on the other one ends its call too
    await login(client, "mike@twgsecurity.com")
    await client.post(f"/api/alarms/{a1.id}/ack", json={"verdict": "false"})
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    assert (await client.get("/api/mobile/dispatches", headers=h)).json() == []
    for sub in (guard_sub, both_sub, admin_sub):
        bus.unsubscribe(sub)



# ---------------------------------------------------------------- passkeys in the app (same keys as the website)
from tests.test_two_factor import add_passkey, soft_key  # noqa: E402


async def _app_key(client, email):
    key = soft_key()
    await login(client, email)
    await add_passkey(client, key, name="iPhone")
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    return key


async def test_passkey_sign_in_without_a_password(client, session, twg):
    t, u = twg
    uid = u.id
    key = await _app_key(client, u.email)
    r = await client.post("/api/mobile/passkey/options", json={"device_name": DEVICE})
    assert r.status_code == 200 and r.json()["options"]["allowCredentials"] == [] and r.json()["challenge_id"]
    body = r.json()
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": body["challenge_id"], "credential": key.sign(body["options"])})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith("twgm_") and r.json()["user"]["id"] == uid
    row = await session.get(UserSession, sessions.mobile_id(r.json()["token"]))
    assert row.method == "mobile" and row.user_agent == DEVICE
    assert (await client.get("/api/mobile/me", headers=bearer(r.json()["token"]))).status_code == 200
    # The challenge is single use
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": body["challenge_id"], "credential": key.sign(body["options"])})
    assert r.status_code == 400 and "too long" in r.json()["detail"]


async def test_passkey_rejections_count_as_failed_sign_ins(client, session, twg):
    t, u = twg
    key = await _app_key(client, u.email)

    async def attempt(credential_from):
        o = (await client.post("/api/mobile/passkey/options", json={})).json()
        return await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": credential_from(o["options"])})
    r = await attempt(lambda o: soft_key().sign(o))                          # not registered here
    assert r.status_code == 400 and "isn't registered" in r.json()["detail"]
    r = await attempt(lambda o: key.sign(o, origin="https://evil.example"))  # wrong origin
    assert r.status_code == 400 and "couldn't be verified" in r.json()["detail"]
    o1 = (await client.post("/api/mobile/passkey/options", json={})).json()
    o2 = (await client.post("/api/mobile/passkey/options", json={})).json()
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o2["challenge_id"], "credential": key.sign(o1["options"])})
    assert r.status_code == 400                                              # signed for another challenge
    fails = (await session.scalars(select(AuthEvent).where(AuthEvent.kind == "passkey", AuthEvent.outcome == "failure"))).all()
    assert len(fails) == 3 and all("(app)" in f.reason for f in fails)


async def test_passkey_as_the_second_factor_and_the_two_step_answer_says_so(client, session, twg):
    t, u = twg
    uid, email = u.id, u.email
    key = await _app_key(client, email)
    secret, _codes = await enable_totp(client, email)
    r = await app_login(client, email)
    assert r.json()["step"] == "totp" and r.json()["passkey"] is True
    ch = r.json()["challenge"]
    o = (await client.post("/api/mobile/passkey/options", json={"challenge": ch})).json()
    assert len(o["options"]["allowCredentials"]) == 1                       # only this person's keys
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": key.sign(o["options"])})
    assert r.status_code == 200 and r.json()["user"]["id"] == uid
    # The two-step challenge is used up: the code no longer works with it
    r = await client.post("/api/mobile/login/totp", json={"challenge": ch, "code": now_code(secret, 30)})
    assert r.status_code == 401
    assert (await client.post("/api/mobile/passkey/options", json={"challenge": ch})).status_code == 401
    # A different person's passkey can't be the second factor
    o = (await client.post("/api/mobile/passkey/options", json={"challenge": (await app_login(client, email)).json()["challenge"]})).json()
    r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": soft_key().sign(o["options"])})
    assert r.status_code == 400


async def test_passkey_only_two_step_when_required(client, session, twg):
    """Required two-step, no authenticator app, a passkey: the app is asked for the passkey (was a 400 before)."""
    t, u = twg
    email = u.email
    key = await _app_key(client, email)
    await settings_row(session, mfa_require_twg=True)
    try:
        r = await app_login(client, email)
        assert r.json()["step"] == "passkey" and r.json()["passkey"] is True and "token" not in r.json()
        o = (await client.post("/api/mobile/passkey/options", json={"challenge": r.json()["challenge"]})).json()
        r = await client.post("/api/mobile/passkey/verify", json={"challenge_id": o["challenge_id"], "credential": key.sign(o["options"])})
        assert r.status_code == 200 and r.json()["token"]
    finally:
        await settings_row(session, mfa_require_twg=False)


async def test_apple_app_site_association(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "apple_team_id", "")                 # independent of the machine's .env
    monkeypatch.setattr(get_settings(), "apple_bundle_id", "com.twgsecurity.alarms")
    r = await client.get("/.well-known/apple-app-site-association")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json" and r.json() == {"webcredentials": {"apps": []}}
    monkeypatch.setattr(get_settings(), "apple_team_id", "abcde12345")
    r = await client.get("/.well-known/apple-app-site-association", follow_redirects=False)
    assert r.status_code == 200 and r.json() == {"webcredentials": {"apps": ["ABCDE12345.com.twgsecurity.alarms"]}}
    monkeypatch.setattr(get_settings(), "apple_team_id", "not-a-team")
    assert (await client.get("/.well-known/apple-app-site-association")).json()["webcredentials"]["apps"] == []

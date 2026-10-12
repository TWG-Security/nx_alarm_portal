"""Two-step sign-in (authenticator app, recovery codes, passkeys) and server-side sessions."""
import base64
import json
import re
from datetime import timedelta

import httpx
import itsdangerous
import pyotp
import pytest
from sqlalchemy import select

from app import mfa, platform_settings, sessions
from app.config import get_settings
from app.models import AuditLog, AuthEvent, PlatformSettings, Tenant, User, UserSession
from app.security import hash_password
from tests.conftest import PASSWORD, login
from tests.soft_passkey import SoftPasskey


def csrf_of(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


async def password_step(client, email, password=PASSWORD):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    page = await client.get("/login")
    return await client.post("/login", data={"email": email, "password": password, "csrf_token": csrf_of(page.text)})


async def submit(client, path, code):
    page = await client.get(path)
    return await client.post(path, data={"code": code, "csrf_token": csrf_of(page.text)})


def now_code(secret, offset=0):
    import time
    return pyotp.TOTP(secret).at(time.time() + offset)


async def settings_row(session, **values):
    row = await session.get(PlatformSettings, 1) or PlatformSettings(id=1)
    for k, v in values.items():
        setattr(row, k, v)
    session.add(row)
    await session.commit()
    await platform_settings.refresh()


@pytest.fixture
async def twg(session):
    t = Tenant(name="TWG Security", kind="platform")
    session.add(t)
    await session.flush()
    u = User(tenant_id=t.id, email="op@twgsecurity.com", display_name="Op", password_hash=hash_password(PASSWORD), role="admin")
    session.add(u)
    await session.commit()
    return t, u


async def enable_totp(client, email) -> tuple[str, list[str]]:
    await login(client, email)
    setup = (await client.post("/api/account/totp/setup")).json()
    r = await client.post("/api/account/totp/enable", json={"code": now_code(setup["secret"])})
    assert r.status_code == 200, r.text
    return setup["secret"], r.json()["recovery_codes"]


# ---------------------------------------------------------------- authenticator app
async def test_totp_setup_and_two_step_sign_in(client, session, twg):
    secret, codes = await enable_totp(client, twg[1].email)
    assert len(codes) == 10 and all(re.fullmatch(r"[0-9a-f]{4}(-[0-9a-f]{4}){3}", c) for c in codes)
    r = await password_step(client, twg[1].email)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"
    # Half signed in: nothing else works yet.
    assert (await client.get("/api/sites")).status_code == 401
    assert (await client.get("/api/account")).status_code == 401
    r = await submit(client, "/login/2fa", "000000")
    assert r.status_code == 200 and "That code didn" in r.text
    # The code that enabled 2FA can't be replayed; the next step's code works.
    r = await submit(client, "/login/2fa", now_code(secret))
    assert "already used" in r.text
    r = await submit(client, "/login/2fa", now_code(secret, 30))
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await client.get("/api/sites")).status_code == 200
    kinds = [(e.kind, e.outcome) for e in (await session.scalars(select(AuthEvent).order_by(AuthEvent.id))).all()]
    assert ("totp", "failure") in kinds and kinds[-1] == ("totp", "success")


async def test_recovery_codes_are_single_use(client, session, twg):
    _, codes = await enable_totp(client, twg[1].email)
    await password_step(client, twg[1].email)
    r = await submit(client, "/login/recovery", codes[0].upper().replace("-", " "))     # forgiving about format
    assert r.status_code == 303
    assert (await client.get("/api/account")).json()["recovery_left"] == 9
    await password_step(client, twg[1].email)
    r = await submit(client, "/login/recovery", codes[0])
    assert r.status_code == 200 and "That code didn" in r.text


async def test_too_many_wrong_codes_restart_the_sign_in(client, twg):
    await enable_totp(client, twg[1].email)
    await password_step(client, twg[1].email)
    for _ in range(4):
        await submit(client, "/login/2fa", "123456")
    r = await submit(client, "/login/2fa", "123456")
    assert "Too many wrong codes" in r.text
    assert (await client.get("/login/2fa")).headers["location"] == "/login"


async def test_required_2fa_walks_through_enrolment(client, session, twg):
    await settings_row(session, mfa_require_twg=True)
    r = await password_step(client, twg[1].email)
    assert r.headers["location"] == "/login/enroll"
    assert (await client.get("/api/sites")).status_code == 401                 # the enrol ticket allows nothing else
    page = await client.get("/login/enroll")
    secret = page.text.split('id="totp-secret">')[1].split("<")[0]
    assert 'src="data:image/png;base64,' in page.text
    r = await client.post("/login/enroll", data={"code": "111111", "csrf_token": csrf_of(page.text)})
    assert "That code didn" in r.text
    r = await client.post("/login/enroll", data={"code": now_code(secret), "csrf_token": csrf_of(page.text)})
    assert r.status_code == 200 and "Save your recovery codes" in r.text and len(re.findall(r"<li>[0-9a-f-]{19}</li>", r.text)) == 10
    assert (await client.get("/api/sites")).status_code == 200                 # signed in now
    page = await client.get("/")
    client.headers["X-CSRF-Token"] = page.text.split('name="csrf-token" content="')[1].split('"')[0]
    await session.refresh(twg[1])
    assert twg[1].totp_enabled
    # It can't be turned off while required.
    r = await client.post("/api/account/totp/disable", json={"code": now_code(secret, 30)})
    assert r.status_code == 400 and "requires" in r.text


async def test_company_decides_unless_twg_requires_it_everywhere(client, session, twg):
    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.flush()
    admin = User(tenant_id=acme.id, email="admin@acme.test", password_hash=hash_password(PASSWORD), role="admin")
    session.add(admin)
    await session.commit()
    await login(client, admin.email)
    assert (await client.get("/api/settings/security")).json() == {"require_2fa": False, "decided_by": "company", "company": "Acme"}
    assert (await client.put("/api/settings/security", json={"require_2fa": True})).status_code == 200
    assert (await password_step(client, admin.email)).headers["location"] == "/login/enroll"
    await settings_row(session, mfa_customers="all")
    await login_skip_enrol(client, session, admin)
    assert (await client.put("/api/settings/security", json={"require_2fa": False})).status_code == 400
    assert (await client.get("/api/settings/security")).json()["decided_by"] == "platform"


async def login_skip_enrol(client, session, user):
    """Sign in by enrolling on the spot (tests only need a session)."""
    await password_step(client, user.email)
    page = await client.get("/login/enroll")
    secret = page.text.split('id="totp-secret">')[1].split("<")[0]
    await client.post("/login/enroll", data={"code": now_code(secret), "csrf_token": csrf_of(page.text)})
    page = await client.get("/")
    client.headers["X-CSRF-Token"] = page.text.split('name="csrf-token" content="')[1].split('"')[0]


# ---------------------------------------------------------------- passkeys
def soft_key():
    s = get_settings()
    return SoftPasskey(s.webauthn_rp_id, s.webauthn_origins.split(",")[0])


async def add_passkey(client, key, name="Laptop"):
    options = (await client.post("/api/account/passkeys/options")).json()
    assert options["rp"]["id"] == key.rp_id and options["authenticatorSelection"]["residentKey"] == "preferred"
    r = await client.post("/api/account/passkeys", json={"name": name, "credential": key.register(options)})
    assert r.status_code == 200, r.text
    return r.json()


async def test_passkey_register_test_and_sign_in_without_password(client, session, twg):
    key = soft_key()
    await login(client, twg[1].email)
    row = await add_passkey(client, key)
    opts = (await client.post(f"/api/account/passkeys/{row['id']}/test/options")).json()
    r = await client.post(f"/api/account/passkeys/{row['id']}/test", json={"credential": key.sign(opts)})
    assert r.status_code == 200 and r.json()["tested_at"]
    # Sign in with only the passkey: no email, password or code.
    client.cookies.clear()
    page = await client.get("/login")
    client.headers["X-CSRF-Token"] = csrf_of(page.text)
    opts = (await client.post("/login/passkey/options")).json()
    assert opts["allowCredentials"] == []                      # usernameless: nothing reveals who has passkeys
    r = await client.post("/login/passkey/verify", json={"credential": key.sign(opts)})
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    assert (await client.get("/api/account")).json()["sessions"][0]["method"] == "passkey"


async def test_passkey_rejections(client, session, twg):
    key = soft_key()
    await login(client, twg[1].email)
    await add_passkey(client, key)
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = csrf_of((await client.get("/login")).text)
    opts = (await client.post("/login/passkey/options")).json()
    other = soft_key()                                         # not registered here
    r = await client.post("/login/passkey/verify", json={"credential": other.sign(opts)})
    assert r.status_code == 400 and "isn't registered" in r.text
    opts = (await client.post("/login/passkey/options")).json()
    r = await client.post("/login/passkey/verify", json={"credential": key.sign(opts, origin="https://evil.example")})
    assert r.status_code == 400 and "couldn't be verified" in r.text
    opts = (await client.post("/login/passkey/options")).json()
    good = key.sign(opts)
    assert (await client.post("/login/passkey/verify", json={"credential": good})).status_code == 200
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = csrf_of((await client.get("/login")).text)
    await client.post("/login/passkey/options")
    r = await client.post("/login/passkey/verify", json={"credential": good})          # replayed assertion
    assert r.status_code == 400
    fails = (await session.scalars(select(AuthEvent).where(AuthEvent.kind == "passkey", AuthEvent.outcome == "failure"))).all()
    assert len(fails) == 3


async def test_passkey_as_second_step_when_required(client, session, twg):
    key = soft_key()
    await login(client, twg[1].email)
    await add_passkey(client, key)
    await settings_row(session, mfa_require_twg=True)
    r = await password_step(client, twg[1].email)
    assert r.headers["location"] == "/login/2fa"
    page = await client.get("/login/2fa")
    assert "Use a passkey" in page.text and 'name="code"' not in page.text
    client.headers["X-CSRF-Token"] = csrf_of(page.text) if 'name="csrf_token"' in page.text else \
        page.text.split('name="csrf-token" content="')[1].split('"')[0]
    opts = (await client.post("/login/passkey/options")).json()
    assert [c["id"] for c in opts["allowCredentials"]]          # only this user's keys
    assert (await client.post("/login/passkey/verify", json={"credential": key.sign(opts)})).status_code == 200
    page = await client.get("/")
    client.headers["X-CSRF-Token"] = page.text.split('name="csrf-token" content="')[1].split('"')[0]
    assert (await client.get("/api/sites")).status_code == 200
    # The only second step can't be removed while 2FA is required.
    kid = (await client.get("/api/account")).json()["passkeys"][0]["id"]
    assert (await client.delete(f"/api/account/passkeys/{kid}")).status_code == 400


# ---------------------------------------------------------------- sessions
async def two_browsers(email):
    from app.main import app
    a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://portal.test")
    b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://portal.test")
    await login(a, email)
    await login(b, email)
    return a, b


async def test_end_one_session_and_sign_out_everywhere(session, twg):
    a, b = await two_browsers(twg[1].email)
    try:
        listed = (await a.get("/api/account")).json()["sessions"]
        assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
        other = next(s for s in listed if not s["current"])
        assert (await a.delete(f"/api/account/sessions/{other['id']}")).status_code == 200
        assert (await b.get("/api/sites")).status_code == 401      # cut off on its very next request
        assert (await a.get("/api/sites")).status_code == 200
        await login(b, twg[1].email)
        uid = twg[1].id
        sid_b = (await session.scalars(select(UserSession.id).where(UserSession.user_id == uid, UserSession.revoked_at.is_(None))
                                       .order_by(UserSession.created_at.desc()))).first()
        assert (await a.post("/api/account/sign-out-everywhere")).status_code == 200
        assert (await a.get("/api/sites")).status_code == 401 and (await b.get("/api/sites")).status_code == 401
        await session.refresh(twg[1])
        assert sessions.stream_revoked(uid, sid_b, twg[1].token_version - 1)    # open live streams get told
    finally:
        await a.aclose()
        await b.aclose()


async def test_password_change_signs_out_other_browsers_only(session, twg):
    a, b = await two_browsers(twg[1].email)
    try:
        r = await a.post("/api/account/password", json={"current": PASSWORD, "new": "weak"})
        assert r.status_code == 400 and "uppercase" in r.text and "symbol" in r.text
        r = await a.post("/api/account/password", json={"current": "nope", "new": "Str0ng!Passw0rd"})
        assert r.status_code == 400
        r = await a.post("/api/account/password", json={"current": PASSWORD, "new": "Str0ng!Passw0rd"})
        assert r.status_code == 200 and r.json()["other_sessions_ended"] == 1
        assert (await a.get("/api/sites")).status_code == 200 and (await b.get("/api/sites")).status_code == 401
        assert (await session.scalar(select(AuditLog).where(AuditLog.action == "password.changed"))) is not None
    finally:
        await a.aclose()
        await b.aclose()


async def test_admin_sees_and_ends_a_users_sessions_and_resets_2fa(client, session, twg):
    op = User(tenant_id=twg[0].id, email="night@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    await session.commit()
    secret, _ = await enable_totp(client, op.email)             # client is now the operator
    await login(client, twg[1].email)
    listed = {u["email"]: u for u in (await client.get("/api/users")).json()}
    assert listed[op.email]["totp_enabled"] and listed[op.email]["sessions"] == 1
    sec = (await client.get(f"/api/users/{op.id}/security")).json()
    assert sec["totp_enabled"] and len(sec["sessions"]) == 1
    assert (await client.post(f"/api/users/{op.id}/reset-2fa", json={})).status_code == 200
    await session.refresh(op)
    assert not op.totp_enabled and op.totp_secret_enc == ""
    assert (await client.delete(f"/api/users/{op.id}/sessions")).json()["ended"] == 1
    # Admin password reset also ends their sessions.
    assert (await client.put(f"/api/users/{op.id}", json={"password": "An0ther!Password"})).status_code == 200


async def test_sessions_from_before_this_are_adopted_not_signed_out(client, session, twg):
    data = base64.b64encode(json.dumps({"uid": twg[1].id, "tid": twg[0].id, "csrf": "x"}).encode())
    cookie = itsdangerous.TimestampSigner(get_settings().secret_key).sign(data).decode()
    client.cookies.set("twg_portal", cookie)
    assert (await client.get("/api/sites")).status_code == 200
    row = await session.scalar(select(UserSession).where(UserSession.user_id == twg[1].id))
    assert row.method == "legacy" and row.revoked_at is None
    assert (await client.get("/api/sites")).status_code == 200
    assert len((await session.scalars(select(UserSession))).all()) == 1      # adopted once


async def test_a_browser_closed_too_long_must_sign_in_again(client, session, twg):
    await login(client, twg[1].email)
    row = await session.scalar(select(UserSession).where(UserSession.user_id == twg[1].id))
    row.last_seen_at = sessions.utcnow() - timedelta(hours=13)
    await session.commit()
    assert (await client.get("/api/sites")).status_code == 401
    await session.refresh(row)
    assert row.revoked_reason == "inactive"


async def test_platform_two_factor_and_session_settings(client, session, twg):
    await login(client, twg[1].email)
    r = await client.put("/api/platform/settings/two-factor", json={"require_twg": True, "customers": "all", "passkeys_enabled": False})
    assert r.status_code == 200 and r.json()["two_factor"] == {"require_twg": True, "customers": "all", "passkeys_enabled": False}
    assert mfa.required_for(twg[0]) and not mfa.passkeys_enabled()
    assert (await client.post("/api/account/passkeys/options")).status_code == 400
    r = await client.put("/api/platform/settings/sessions", json={"closed_h": 24, "max_h": 0, "idle_min": 30})
    assert r.status_code == 200 and platform_settings.current().idle_timeout_min == 30
    assert '"idleMin": 30' in (await client.get("/")).text
    assert (await client.put("/api/platform/settings/sessions", json={"closed_h": 0, "max_h": 0, "idle_min": 0})).status_code == 422


async def test_cli_reset_mfa(client, session, twg, capsys):
    from app import cli
    await enable_totp(client, twg[1].email)
    await cli.reset_mfa(twg[1].email.upper(), passkeys=True)
    await session.refresh(twg[1])
    assert not twg[1].totp_enabled
    assert (await password_step(client, twg[1].email)).headers["location"] == "/"
    assert "reset" in capsys.readouterr().out

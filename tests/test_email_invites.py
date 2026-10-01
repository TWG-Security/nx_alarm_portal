"""Email, invites, forgot/reset password, password rules and expiry. A local SMTP sink catches the mails
and the tests follow the links in them."""
import email
import re
import socket
from datetime import timedelta

import httpx
import pytest
from aiosmtpd.controller import Controller
from sqlalchemy import select

from app import mail, passwords, platform_settings
from app import security_guard as guard
from app.models import AuditLog, EmailLog, PlatformSettings, Tenant, User, UserToken
from app.security import hash_password
from tests.conftest import PASSWORD, login

GOOD = "Fresh-Start-2026!"


class Sink:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(email.message_from_bytes(envelope.content))
        return "250 OK"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def smtp():
    sink = Sink()
    ctl = Controller(sink, hostname="127.0.0.1", port=free_port())
    ctl.start()
    yield ctl.port, sink.messages
    ctl.stop()


@pytest.fixture
async def twg(session):
    t = Tenant(name="TWG Security", kind="platform")
    session.add(t)
    await session.flush()
    u = User(tenant_id=t.id, email="boss@twgsecurity.com", display_name="Boss", password_hash=hash_password(PASSWORD), role="admin")
    session.add(u)
    await session.commit()
    return t, u


async def email_on(client, admin, port):
    await login(client, admin.email)
    r = await client.put("/api/platform/settings/email", json={
        "enabled": True, "host": "127.0.0.1", "port": port, "tls": "none", "user": "", "password": "smtp-secret-x",
        "sender": "TWG Alarm Portal <alerts@twgsecurity.com>", "portal_url": "https://alarmportal.twgsecurity.net"})
    assert r.status_code == 200, r.text
    assert "smtp-secret-x" not in r.text and r.json()["email"]["password_set"]


def html_of(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode()


def link_in(msg, path):
    m = re.search(rf"https://alarmportal\.twgsecurity\.net{path}\?token=([A-Za-z0-9_\-]+)", html_of(msg))
    return m.group(1)


def csrf_of(html):
    return html.split('name="csrf_token" value="')[1].split('"')[0]


async def form_post(client, path, data, page_path=None):
    page = await client.get(page_path or path)
    return await client.post(path, data={**data, "csrf_token": csrf_of(page.text)})


# ---------------------------------------------------------------- invites
async def test_invite_email_link_sets_password_once(client, session, twg, smtp):
    port, inbox = smtp
    await email_on(client, twg[1], port)
    r = await client.post("/api/users", json={"email": "Night@TWGsecurity.com", "display_name": "Night", "role": "operator"})
    assert r.status_code == 200, r.text
    assert r.json()["invited"] and r.json()["email"] == "sending" and "/setup?token=" in r.json()["setup_link"]
    await guard.drain()
    [msg] = inbox
    assert msg["To"] == "night@twgsecurity.com" and "invited" in msg["Subject"] and "TWG Alarm Portal" in msg["From"]
    html = html_of(msg)
    # TWG email branding: white header with a 5px charcoal border, enlarged logo, red accents, orange button.
    assert "border:5px solid #1A1A1A" in html and "d4fc57_b28e915a1b924e33b12eb77028f74f62" in html
    assert "#C0392B" in html and "background:#E08A30" in html and "background:#1A1A1A" not in html
    token = link_in(msg, "/setup")
    assert token in r.json()["setup_link"]
    listed = {u["email"]: u for u in (await client.get("/api/users")).json()}
    assert listed["night@twgsecurity.com"]["invited"] and listed["night@twgsecurity.com"]["invite_email"]["outcome"] == "sent"

    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    page = await client.get(f"/setup?token={token}")
    assert "night@twgsecurity.com" in page.text and "TWG Security alarm portal" in page.text
    r = await form_post(client, "/setup", {"token": token, "password": "short", "password2": "short"}, f"/setup?token={token}")
    assert "needs at least 12 characters" in r.text
    r = await form_post(client, "/setup", {"token": token, "password": GOOD, "password2": GOOD}, f"/setup?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await client.get("/api/sites")).status_code == 200                # signed in
    # The link worked once.
    client.cookies.clear()
    assert "already used" in (await client.get(f"/setup?token={token}")).text
    user = await session.scalar(select(User).where(User.email == "night@twgsecurity.com"))
    await session.refresh(user)
    assert not user.is_invited and user.password_changed_at is not None


async def test_invite_without_email_returns_a_link_and_new_link_retires_the_old(client, session, twg):
    await login(client, twg[1].email)
    r = (await client.post("/api/users", json={"email": "day@twgsecurity.com", "role": "operator"})).json()
    assert r["email"] == "off"
    first = r["setup_link"].split("token=")[1]
    uid = r["id"]
    second = (await client.post(f"/api/users/{uid}/invite")).json()["setup_link"].split("token=")[1]
    client.cookies.clear()
    assert "already used" in (await client.get(f"/setup?token={first}")).text
    assert 'name="password"' in (await client.get(f"/setup?token={second}")).text
    assert "isn't valid" in (await client.get("/setup?token=nonsense")).text
    row = await session.scalar(select(UserToken).where(UserToken.user_id == uid, UserToken.used_at.is_(None)))
    row.expires_at = row.created_at - timedelta(seconds=1)
    await session.commit()
    assert "expired" in (await client.get(f"/setup?token={second}")).text


async def test_new_company_invites_its_first_admin(client, session, twg):
    await login(client, twg[1].email)
    r = await client.post("/api/tenants", json={"name": "Acme Security", "admin_email": "boss@acme.test"})
    assert r.status_code == 200 and "/setup?token=" in r.json()["setup_link"]
    assert (await client.post("/api/tenants", json={"name": "Weak Co", "admin_email": "w@weak.test",
                                                    "admin_password": "password1234"})).status_code == 400


async def test_invited_user_with_2fa_required_enrols_after_setting_the_password(client, session, twg):
    await login(client, twg[1].email)
    await client.put("/api/platform/settings/two-factor", json={"require_twg": True, "customers": "company", "passkeys_enabled": True})
    token = (await client.post("/api/users", json={"email": "x@twgsecurity.com"})).json()["setup_link"].split("token=")[1]
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = await form_post(client, "/setup", {"token": token, "password": GOOD, "password2": GOOD}, f"/setup?token={token}")
    assert r.status_code == 303, r.text[r.text.find('<div class="alert'):][:300]
    assert r.headers["location"] == "/login/enroll"
    assert (await client.get("/api/sites")).status_code == 401


# ---------------------------------------------------------------- forgot / reset
async def test_forgot_password_never_reveals_accounts_and_reset_works_once(client, session, twg, smtp):
    port, inbox = smtp
    await email_on(client, twg[1], port)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    a = await form_post(client, "/forgot", {"email": "nobody@example.com"})
    b = await form_post(client, "/forgot", {"email": twg[1].email})
    strip = lambda t, e: re.sub(r'(content|value)="[A-Za-z0-9_\-]{40,}"', "", t).replace(e, "X")  # noqa: E731
    assert strip(a.text, "nobody@example.com") == strip(b.text, twg[1].email) and "we've emailed it" in a.text
    await guard.drain()
    assert [m["To"] for m in inbox] == [twg[1].email]
    token = link_in(inbox[0], "/reset")
    # Opening the link doesn't use it up.
    assert 'name="password"' in (await client.get(f"/reset?token={token}")).text
    assert 'name="password"' in (await client.get(f"/reset?token={token}")).text
    # A browser signed in before the reset is signed out by it.
    from app.main import app
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://portal.test")
    await login(other, twg[1].email)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = await form_post(client, "/reset", {"token": token, "password": GOOD, "password2": GOOD + "x"}, f"/reset?token={token}")
    assert "don&#39;t match" in r.text
    r = await form_post(client, "/reset", {"token": token, "password": GOOD, "password2": GOOD}, f"/reset?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/login?reason=reset"
    assert (await other.get("/api/sites")).status_code == 401
    await other.aclose()
    assert "already used" in (await client.get(f"/reset?token={token}")).text
    assert "password was changed" in (await client.get("/login?reason=reset")).text
    client.cookies.clear()
    page = await client.get("/login")
    r = await client.post("/login", data={"email": twg[1].email, "password": GOOD, "csrf_token": csrf_of(page.text)})
    assert r.status_code == 303
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "password.reset")) is not None


async def test_forgot_is_rate_limited_and_resends_invites(client, session, twg, smtp):
    port, inbox = smtp
    await email_on(client, twg[1], port)
    await client.post("/api/users", json={"email": "new@twgsecurity.com"})
    await guard.drain()
    inbox.clear()
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    await form_post(client, "/forgot", {"email": "new@twgsecurity.com"})                     # never set up: a new invite
    for _ in range(4):
        await form_post(client, "/forgot", {"email": twg[1].email})
    await guard.drain()
    assert [m["To"] for m in inbox if "invited" in m["Subject"]] == ["new@twgsecurity.com"]
    assert sum(m["To"] == twg[1].email for m in inbox) == 3                                  # 3 an hour
    expired = await session.scalar(select(UserToken).where(UserToken.purpose == "reset", UserToken.used_at.is_(None)))
    expired.expires_at = expired.created_at - timedelta(minutes=1)
    await session.commit()


async def test_reset_link_expires(client, session, twg, smtp):
    port, inbox = smtp
    await email_on(client, twg[1], port)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    await form_post(client, "/forgot", {"email": twg[1].email})
    await guard.drain()
    token = link_in(inbox[0], "/reset")
    row = await session.scalar(select(UserToken).where(UserToken.purpose == "reset"))
    row.expires_at = row.created_at - timedelta(seconds=1)
    await session.commit()
    assert "expired" in (await client.get(f"/reset?token={token}")).text
    r = await form_post(client, "/reset", {"token": token, "password": GOOD, "password2": GOOD}, "/forgot")
    assert "expired" in r.text


# ---------------------------------------------------------------- email failures, settings
async def test_smtp_down_is_retried_logged_and_shown(client, session, twg, monkeypatch):
    monkeypatch.setattr(mail, "RETRY_DELAYS_S", (0, 0))
    await email_on(client, twg[1], free_port())                       # nothing listens there
    r = await client.post("/api/users", json={"email": "z@twgsecurity.com"})
    assert r.json()["email"] == "sending" and r.json()["setup_link"]     # the admin still has the link
    await guard.drain()
    log = await session.scalar(select(EmailLog))
    assert log.outcome == "failed" and log.attempts == 3 and "ConnectionRefused" in log.error
    s = (await client.get("/api/platform/settings")).json()["email"]
    assert "ConnectionRefused" in s["last_error"]
    t = (await client.post("/api/platform/email/test", json={"to": "me@twgsecurity.com"})).json()
    assert t["outcome"] == "failed"
    logs = (await client.get("/api/platform/email-log")).json()
    assert {x["purpose"] for x in logs} == {"invite", "test"}


async def test_email_settings_validation_and_audit(client, session, twg):
    await login(client, twg[1].email)
    bad = {"enabled": True, "host": "", "port": 587, "tls": "starttls", "sender": "x@y.z", "portal_url": "https://a.b"}
    assert (await client.put("/api/platform/settings/email", json=bad)).status_code == 400
    assert (await client.put("/api/platform/settings/email", json={**bad, "host": "smtp.x", "portal_url": "https://a.b/path"})).status_code == 400
    assert (await client.put("/api/platform/settings/email", json={**bad, "host": "smtp.x", "sender": "nobody"})).status_code == 400
    ok = await client.put("/api/platform/settings/email", json={**bad, "host": "smtp.x", "password": "pw-123-secret"})
    assert ok.status_code == 200
    row = await session.get(PlatformSettings, 1)
    assert row.smtp_password_enc and "pw-123-secret" not in row.smtp_password_enc
    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "platform.settings"))
    assert "pw-123-secret" not in str(audit.detail) and "password" in audit.detail["fields"]
    assert (await client.post("/api/platform/email/test", json={"to": "a@b.c"})).status_code == 200


# ---------------------------------------------------------------- password rules and expiry
def test_password_rules_say_what_is_missing():
    assert passwords.check("Str0ng!Passw0rd") is None
    msg = passwords.check("password")
    assert "at least 12 characters (this has 8)" in msg and "an uppercase letter" in msg and "a number" in msg and "a symbol" in msg
    assert passwords.check("aaaaaaaaaaaa", passwords.Policy(min_length=12, upper=False, number=False, symbol=False)) is None
    assert "12 characters" in passwords.hint()


async def test_rules_are_editable_and_enforced_everywhere(client, session, twg):
    await login(client, twg[1].email)
    r = await client.put("/api/platform/settings/passwords", json={"min_length": 16, "upper": True, "lower": True,
                                                                   "number": True, "symbol": False, "expiry_days": 0})
    assert r.status_code == 200
    assert "16 characters" in (await client.post("/api/users", json={"email": "q@twgsecurity.com", "password": "Short1abcdefg"})).text
    assert (await client.post("/api/users", json={"email": "q@twgsecurity.com", "password": "Long1abcdefghijkl"})).status_code == 200
    uid = (await session.scalar(select(User).where(User.email == "q@twgsecurity.com"))).id
    assert (await client.put(f"/api/users/{uid}", json={"password": "weak"})).status_code == 400


async def test_expired_password_must_be_changed_after_sign_in(client, session, twg):
    await login(client, twg[1].email)
    await client.put("/api/platform/settings/passwords", json={"min_length": 12, "upper": True, "lower": True,
                                                               "number": True, "symbol": True, "expiry_days": 30})
    twg[1].password_changed_at = twg[1].created_at - timedelta(days=40)
    await session.commit()
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    page = await client.get("/login")
    r = await client.post("/login", data={"email": twg[1].email, "password": PASSWORD, "csrf_token": csrf_of(page.text)})
    assert r.headers["location"] == "/login/password-expired"
    assert (await client.get("/api/sites")).status_code == 401
    r = await form_post(client, "/login/password-expired", {"password": PASSWORD, "password2": PASSWORD})
    assert "must be different" in r.text
    r = await form_post(client, "/login/password-expired", {"password": GOOD, "password2": GOOD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await client.get("/api/sites")).status_code == 200


def test_cli_refuses_weak_passwords():
    from app import cli
    with pytest.raises(SystemExit) as e:
        cli._check("password1234")
    assert "uppercase" in str(e.value)
    cli._check(GOOD)

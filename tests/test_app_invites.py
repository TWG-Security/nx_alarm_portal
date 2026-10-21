"""Phone app invites: GET /api/apps, POST /api/apps/invite (app/appstore.py against a fake App Store Connect), the
Platform page's Phone apps settings."""

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import select

from app import appstore, mail
from app.config import get_settings
from app.models import AuditLog, PlatformSettings, Tenant, UserGroup, UserGroupMember
from tests.conftest import login, make_tenant_user
from tests.test_mobile import app_login, bearer

ISSUER, KEY_ID, APP_ID, GROUP_ID = "69a6de7f-1111-4a2b-8c3d-123456789abc", "ABCDE12345", "6739000000", "grp-1"


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeASC:
    """Enough of App Store Connect: beta groups by app + name, testers by email (+ group), create, add to group."""

    def __init__(self, public_key):
        self.public_key, self.testers, self.calls, self.groups = public_key, {}, [], {GROUP_ID: "TWG Testers"}

    def _auth(self, request):
        head, body, sig = request.headers["authorization"].removeprefix("Bearer ").split(".")
        h, b = json.loads(_unb64(head)), json.loads(_unb64(body))
        assert h == {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
        assert b["iss"] == ISSUER and b["aud"] == "appstoreconnect-v1" and b["exp"] - b["iat"] <= 1200
        raw = _unb64(sig)
        self.public_key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                               f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))       # raises if forged

    def handler(self, request: httpx.Request) -> httpx.Response:
        self._auth(request)
        q, path = dict(request.url.params), request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "/v1/betaGroups":
            assert q["filter[app]"] == APP_ID
            return httpx.Response(200, json={"data": [{"type": "betaGroups", "id": gid, "attributes": {"name": n}}
                                                      for gid, n in self.groups.items() if n == q["filter[name]"]]})
        if request.method == "GET" and path == "/v1/betaTesters":
            t = self.testers.get(q["filter[email]"])
            hit = t and ("filter[betaGroups]" not in q or q["filter[betaGroups]"] in t["groups"])
            return httpx.Response(200, json={"data": [{"type": "betaTesters", "id": t["id"]}] if hit else []})
        if request.method == "POST" and path == "/v1/betaTesters":
            d = json.loads(request.content)["data"]
            email = d["attributes"]["email"]
            if email in self.testers:
                return httpx.Response(409, json={"errors": [{"status": "409", "detail": "Tester already exists"}]})
            self.testers[email] = {"id": f"t-{len(self.testers) + 1}", "name": (d["attributes"]["firstName"], d["attributes"]["lastName"]),
                                   "groups": {g["id"] for g in d["relationships"]["betaGroups"]["data"]}}
            return httpx.Response(201, json={"data": {"type": "betaTesters", "id": self.testers[email]["id"]}})
        if request.method == "POST" and path == f"/v1/betaGroups/{GROUP_ID}/relationships/betaTesters":
            tid = json.loads(request.content)["data"][0]["id"]
            next(t for t in self.testers.values() if t["id"] == tid)["groups"].add(GROUP_ID)
            return httpx.Response(204)
        return httpx.Response(404, json={"errors": [{"detail": "not found"}]})


@pytest.fixture
def asc(monkeypatch, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / f"ASC_AuthKey_{KEY_ID}.p8"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cfg = get_settings()
    monkeypatch.setattr(cfg, "asc_key_path", str(pem))
    monkeypatch.setattr(cfg, "asc_key_id", KEY_ID)
    fake = FakeASC(key.public_key())
    real = appstore._client
    monkeypatch.setattr(appstore, "_client", lambda c: httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url="https://asc.test", headers=real(c).headers))
    appstore._token, appstore._groups = None, {}
    sent = []
    monkeypatch.setattr(mail, "send_later", lambda *a, **k: sent.append((a, k)))
    fake.sent = sent
    return fake


async def _configure(session, testflight="https://testflight.apple.com/join/AbC123xY"):
    row = await session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        session.add(row)
    row.testflight_url, row.asc_issuer_id, row.asc_app_id, row.asc_beta_group = testflight, ISSUER, APP_ID, "TWG Testers"
    await session.commit()


async def test_apps_info_and_ios_invites(client, session, asc):
    t, u = await make_tenant_user(session)
    await login(client, u.email)
    r = (await client.get("/api/apps")).json()
    assert r["ios"] == {"testflight_url": None, "invites": False}
    assert r["android"]["page"] == "/apps" and r["android"]["apk"] == "/apps/android.apk" and r["can_invite"] is True
    out = (await client.post("/api/apps/invite", json={"email": "Guard@Example.com", "platform": "ios"})).json()
    assert out == {"ios": "App Store Connect isn't set up yet (Platform → Phone apps)"}

    await _configure(session)
    r = (await client.get("/api/apps")).json()
    assert r["ios"] == {"testflight_url": "https://testflight.apple.com/join/AbC123xY", "invites": True}
    assert "Join on TestFlight" in (await client.get("/apps")).text
    out = (await client.post("/api/apps/invite", json={"email": "Guard@Example.com", "name": "Pat Guard Smith", "platform": "ios"})).json()
    assert out == {"ios": "invited"}
    assert asc.testers["guard@example.com"]["name"] == ("Pat", "Guard Smith") and GROUP_ID in asc.testers["guard@example.com"]["groups"]
    out = (await client.post("/api/apps/invite", json={"email": "guard@example.com", "platform": "ios"})).json()
    assert out == {"ios": "already_tester"}
    # A tester of another group: added to ours.
    asc.testers["old@example.com"] = {"id": "t-old", "name": ("O", ""), "groups": {"other"}}
    assert (await client.post("/api/apps/invite", json={"email": "old@example.com", "platform": "ios"})).json() == {"ios": "invited"}
    assert GROUP_ID in asc.testers["old@example.com"]["groups"]
    # Apple's error text comes back as is.
    asc.groups = {}
    appstore._groups = {}
    out = (await client.post("/api/apps/invite", json={"email": "x@example.com", "platform": "ios"})).json()
    assert out["ios"] == 'App Store Connect has no beta group named "TWG Testers" for this app'
    rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "app.invited").order_by(AuditLog.id))).all()
    assert [r.detail["outcome"]["ios"] for r in rows][1:3] == ["invited", "already_tester"]
    assert rows[1].detail["target"] == "guard@example.com" and rows[1].user_id == u.id


async def test_android_invite_email_or_share_link(client, session, asc, monkeypatch):
    t, u = await make_tenant_user(session)
    await login(client, u.email)

    async def no_smtp():
        return None
    monkeypatch.setattr(mail, "config", no_smtp)
    out = (await client.post("/api/apps/invite", json={"email": "a@example.com", "platform": "android"})).json()
    assert out == {"android": "share_link", "url": "https://alarmportal.twgsecurity.net/apps"} and not asc.sent

    async def smtp():
        return object()
    monkeypatch.setattr(mail, "config", smtp)
    out = (await client.post("/api/apps/invite", json={"email": "a@example.com", "platform": "both"})).json()
    assert out["android"] == "emailed" and out["url"].endswith("/apps") and "ios" in out
    (to, subject, html, text), kw = asc.sent[0]
    assert to == "a@example.com" and subject == "Get the TWG Alarms app" and "/apps" in text and kw["purpose"] == "app_invite"


async def test_who_may_invite_bearer_and_rate_limit(client, session, asc):
    t, admin = await make_tenant_user(session)
    _, op = await make_tenant_user(session, tenant_name="tmp", email="op@twgsecurity.com", role="operator")
    op.tenant_id = t.id
    await session.commit()
    token = (await app_login(client, op.email)).json()["token"]
    assert (await client.get("/api/apps", headers=bearer(token))).json()["can_invite"] is False
    r = await client.post("/api/apps/invite", json={"email": "b@example.com", "platform": "android"}, headers=bearer(token))
    assert r.status_code == 403
    g = UserGroup(tenant_id=t.id, name="Inviters", permissions=["users.invite"])
    session.add(g)
    await session.flush()
    session.add(UserGroupMember(group_id=g.id, user_id=op.id))
    await session.commit()
    r = await client.post("/api/apps/invite", json={"email": "b@example.com", "platform": "android"}, headers=bearer(token))
    assert r.status_code == 200                                 # Bearer: no CSRF token needed
    assert (await client.post("/api/apps/invite", json={"email": "not-an-email", "platform": "ios"}, headers=bearer(token))).status_code == 422
    now = datetime.now(timezone.utc)
    for _ in range(29):
        session.add(AuditLog(tenant_id=t.id, user_id=op.id, action="app.invited", detail={}, ts=now - timedelta(minutes=5)))
    await session.commit()
    r = await client.post("/api/apps/invite", json={"email": "c@example.com", "platform": "android"}, headers=bearer(token))
    assert r.status_code == 429


async def test_platform_phone_apps_settings(client, session, asc):
    t, u = await make_tenant_user(session)
    t.kind = "platform"
    await session.commit()
    await login(client, u.email)
    put = lambda body: client.put("/api/platform/settings/apps", json=body)  # noqa: E731
    assert (await put({"testflight_url": "https://evil.example/join/x"})).status_code == 400
    assert (await put({"issuer_id": "nope"})).status_code == 400
    assert (await put({"app_id": "com.twgsecurity.alarms"})).status_code == 400
    r = await put({"testflight_url": "https://testflight.apple.com/join/AbC123xY", "issuer_id": ISSUER.upper(),
                   "app_id": APP_ID, "beta_group": "TWG Testers"})
    assert r.status_code == 200 and r.json()["apps"]["issuer_id"] == ISSUER
    assert r.json()["apps"]["key"] == {"installed": True, "key_id": KEY_ID}
    test = await client.post("/api/platform/apps/test")
    assert test.status_code == 200 and test.json()["group_id"] == GROUP_ID
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "platform.settings"))
    assert row.detail["section"] == "apps" and set(row.detail["fields"]) == {"testflight_url", "issuer_id", "app_id"}

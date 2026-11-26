"""Phone apps: /apps and /apps/android.apk (app/app_releases.py) against a fake GitHub."""

import json
import time

import httpx
import pytest
from sqlalchemy import select

from app import app_releases
from app.config import get_settings
from app.models import AuditLog
from tests.conftest import login, make_tenant_user
from tests.test_mobile import app_login, bearer

TOKEN = "ghp_fake_secret_token_for_tests"
APK = b"PK\x03\x04" + b"fake-android-package" * 1000


class FakeGitHub:
    def __init__(self):
        self.calls, self.down, self.asset_id, self.version = [], False, 1001, "0.1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.down:
            return httpx.Response(503)
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={
                "tag_name": f"v{self.version}-1", "name": f"TWG Alarms for Android {self.version}",
                "published_at": "2026-10-21T16:33:26Z", "assets": [
                    {"id": self.asset_id, "name": f"TWG-Alarms-{self.version}.apk", "size": len(APK)}]})
        if request.url.path.endswith(f"/releases/assets/{self.asset_id}"):
            assert request.headers["accept"] == "application/octet-stream"
            return httpx.Response(200, content=APK)
        return httpx.Response(404)


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "github_releases_token", TOKEN)
    monkeypatch.setattr(cfg, "media_cache_dir", str(tmp_path))
    monkeypatch.setattr(app_releases, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url="https://api.github.test",
        headers={"Authorization": f"Bearer {TOKEN}"}))
    return fake


def _age(seconds):
    meta = json.loads(app_releases._meta_path().read_text())
    meta["checked_at"] -= seconds
    app_releases._meta_path().write_text(json.dumps(meta))


async def test_page_download_cache_and_audit(client, session, github):
    t, u = await make_tenant_user(session)
    await login(client, u.email)
    page = await client.get("/apps")
    assert page.status_code == 200
    assert "Version 0.1" in page.text and "released October 21, 2026" in page.text and "TestFlight" in page.text
    r = await client.get("/apps/android.apk")
    assert r.status_code == 200 and r.content == APK
    assert r.headers["content-type"] == "application/vnd.android.package-archive"
    assert r.headers["content-disposition"] == 'attachment; filename="TWG-Alarms-0.1.apk"'
    for text in (r.headers.__repr__(), page.text):           # nothing about GitHub or the token leaves the server
        assert TOKEN not in text and "github" not in text.lower()
    assert github.calls == ["/repos/TWG-Security/twg-alarm-android/releases/latest",
                            "/repos/TWG-Security/twg-alarm-android/releases/assets/1001"]
    # Within the hour: from the disk cache, GitHub isn't asked.
    assert (await client.get("/apps/android.apk")).content == APK and len(github.calls) == 2
    rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "app.downloaded"))).all()
    assert len(rows) == 2 and rows[0].user_id == u.id and rows[0].detail["version"] == "0.1"
    assert rows[0].detail["via"] == "website"

    # An hour later the same release: one look, no new download. A new release: downloaded.
    _age(3601)
    await client.get("/apps/android.apk")
    assert github.calls[-1].endswith("/releases/latest") and len(github.calls) == 3
    _age(3601)
    github.asset_id, github.version = 1002, "0.2"
    r = await client.get("/apps/android.apk")
    assert r.headers["content-disposition"] == 'attachment; filename="TWG-Alarms-0.2.apk"'


async def test_github_down_keeps_the_last_apk(client, session, github):
    t, u = await make_tenant_user(session)
    await login(client, u.email)
    github.down = True
    assert (await client.get("/apps/android.apk")).status_code == 503       # nothing cached yet
    assert "can't be downloaded right now" in (await client.get("/apps")).text
    n = len(github.calls)
    await client.get("/apps/android.apk")
    assert len(github.calls) == n                         # a failure isn't retried for 5 minutes
    github.down = False
    meta = json.loads(app_releases._meta_path().read_text())
    meta["failed_at"] = time.time() - 301
    app_releases._meta_path().write_text(json.dumps(meta))
    assert (await client.get("/apps/android.apk")).status_code == 200
    github.down = True
    _age(3601)
    meta = json.loads(app_releases._meta_path().read_text())
    r = await client.get("/apps/android.apk")
    assert r.status_code == 200 and r.content == APK      # GitHub down: the cached APK still downloads


async def test_signed_out_and_the_app_bearer(client, session, github):
    t, u = await make_tenant_user(session)
    client.cookies.clear()
    r = await client.get("/apps/android.apk")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")      # a browser goes to sign-in
    r = await client.get("/apps/android.apk", headers={"Authorization": "Bearer twgm_nope"})
    assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}    # an app gets a 401
    token = (await app_login(client, u.email)).json()["token"]
    r = await client.get("/apps/android.apk", headers=bearer(token))
    assert r.status_code == 200 and r.content == APK
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "app.downloaded"))
    assert row.detail["via"] == "app"

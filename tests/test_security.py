import httpx
import respx
from sqlalchemy import select

from app.models import Site
from app.security import decrypt
from app.services.poller import ingest
from tests.conftest import NX, login, make_site, make_tenant_user, nx_row


async def test_pages_require_login(client):
    r = await client.get("/alarms")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.get("/api/alarms")).status_code == 401


async def test_bad_password_rejected(client, admin):
    r = await client.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = await client.post("/login", data={"email": admin[1].email, "password": "wrong", "csrf_token": csrf})
    assert r.status_code == 200 and "Invalid email or password" in r.text


async def test_mutations_require_csrf(client, admin):
    await login(client, admin[1].email)
    del client.headers["X-CSRF-Token"]
    r = await client.post("/api/sites/test", json={"host": NX, "nx_user": "x", "nx_pass": "y"})
    assert r.status_code == 403


@respx.mock
async def test_site_password_encrypted_and_never_returned(client, session, admin):
    await login(client, admin[1].email)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/site/info").mock(return_value=httpx.Response(200, json={"name": "HQ", "version": "6.1.2"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[{"id": "d1", "name": "Cam"}]))

    r = await client.post("/api/sites", json={"name": "HQ", "host": NX, "nx_user": "portal", "nx_pass": "s3cret-nx",
                                              "lat": 40.1, "lng": -75.2})
    assert r.status_code == 200, r.text
    assert "s3cret-nx" not in r.text and "nx_pass" not in r.json()
    listing = await client.get("/api/sites")
    assert "s3cret-nx" not in listing.text
    site = await session.scalar(select(Site))
    assert site.nx_pass_enc != "s3cret-nx" and decrypt(site.nx_pass_enc) == "s3cret-nx"
    assert (site.nx_site_name, site.camera_count, site.status) == ("HQ", 1, "online")


async def test_cloud_id_becomes_relay_host():
    from app.services.sites import resolve_host
    host, cloud = resolve_host("{ABCDEF01-2345-6789-ABCD-EF0123456789}")
    assert host == "https://abcdef01-2345-6789-abcd-ef0123456789.relay.vmsproxy.com"
    assert cloud == "abcdef01-2345-6789-abcd-ef0123456789"


async def test_tenant_isolation(client, session, admin):
    tenant_a, _ = admin
    tenant_b, user_b = await make_tenant_user(session, "Other Co", "ops@other.test", role="operator")
    site_a = await make_site(session, tenant_a, name="A-site")
    [alarm_a] = await ingest(session, site_a, [nx_row(9_000_000)], {})
    await session.commit()

    await login(client, user_b.email)
    assert (await client.get("/api/alarms?state=all")).json() == []
    assert (await client.get("/api/sites")).json() == []
    assert (await client.get(f"/api/alarms/{alarm_a.id}")).status_code == 404
    assert (await client.post(f"/api/alarms/{alarm_a.id}/ack", json={"note": "x"})).status_code == 404
    assert (await client.get(f"/media/alarms/{alarm_a.id}/snapshot.jpg")).status_code == 404


async def test_operator_cannot_manage_sites(client, session, admin):
    _, op = await make_tenant_user(session, "Ops Co", "op@twg.test", role="operator")
    await login(client, op.email)
    r = await client.post("/api/sites", json={"name": "X", "host": NX, "nx_user": "u", "nx_pass": "p"})
    assert r.status_code == 403

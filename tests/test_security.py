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


@respx.mock
async def test_geocode_proxies_nominatim_with_user_agent(client, admin):
    await login(client, admin[1].email)
    route = respx.get("https://nominatim.openstreetmap.org/search").mock(return_value=httpx.Response(200, json=[
        {"display_name": "Harrisburg, PA", "lat": "40.2732", "lon": "-76.8867"}]))
    r = await client.get("/api/geocode", params={"q": "Harrisburg PA"})
    assert r.status_code == 200 and r.json() == [{"label": "Harrisburg, PA", "lat": 40.2732, "lng": -76.8867}]
    assert "TWG-Alarm-Portal" in route.calls.last.request.headers["user-agent"]
    # Repeat is served from cache: no second upstream call.
    await client.get("/api/geocode", params={"q": "harrisburg pa "})
    assert route.call_count == 1


@respx.mock
async def test_tile_proxy_requires_login_and_caches(client, admin, tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "tile_cache_dir", str(tmp_path / "tiles"))
    upstream = respx.get("https://tile.openstreetmap.org/3/2/1.png").mock(
        return_value=httpx.Response(200, content=b"\x89PNG-fake", headers={"content-type": "image/png"}))

    assert (await client.get("/tiles/3/2/1.png")).status_code == 401
    await login(client, admin[1].email)
    r1 = await client.get("/tiles/3/2/1.png")
    r2 = await client.get("/tiles/3/2/1.png")
    assert r1.status_code == r2.status_code == 200 and r2.content == b"\x89PNG-fake"
    assert upstream.call_count == 1                       # second request served from disk
    assert "TWG-Alarm-Portal" in upstream.calls.last.request.headers["user-agent"]
    assert (await client.get("/tiles/3/9/1.png")).status_code == 404   # x out of range for z=3


async def test_html_errors_keep_their_status_code(client, session, admin):
    _, op = await make_tenant_user(session, "Ops2", "op2@twg.test", role="operator")
    await login(client, op.email)
    assert (await client.get("/users")).status_code == 403          # admin-only page
    assert (await client.get("/sites/999/edit")).status_code == 403


async def test_alarm_levels_settings_relevel_open_alarms(client, session, admin):
    tenant, user = admin
    site = await make_site(session, tenant)
    [a] = await ingest(session, site, [nx_row(9_500_000, type_="deviceDisconnected")], {})
    await session.commit()
    assert a.priority == 3
    await login(client, user.email)
    cur = (await client.get("/api/settings/alarm-levels")).json()
    levels = {t["type"]: t["level"] for t in cur["types"]}
    levels["deviceDisconnected"] = "critical"
    r = await client.put("/api/settings/alarm-levels", json={"levels": levels, "force_ack_critical": True})
    assert r.status_code == 200
    assert next(t for t in r.json()["types"] if t["type"] == "deviceDisconnected")["level"] == "critical"
    await session.refresh(a)
    assert a.priority == 1
    bad = await client.put("/api/settings/alarm-levels", json={"levels": {"motion": "loud"}})
    assert bad.status_code == 400

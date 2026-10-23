import httpx
import respx
from sqlalchemy import select

from app.models import Site
from app.security import decrypt
from app.services.poller import ingest
from tests.conftest import NX, login, make_site, make_tenant_user, nx_row


async def test_pages_require_login(client):
    r = await client.get("/alarms")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/alarms"
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
                                              "lat": 40.1, "lng": -75.2, "address": "  "})
    assert r.status_code == 400 and "address" in r.text                  # a site needs its address
    assert await session.scalar(select(Site)) is None
    r = await client.post("/api/sites", json={"name": "HQ", "host": NX, "nx_user": "portal", "nx_pass": "s3cret-nx",
                                              "lat": 40.1, "lng": -75.2, "address": "100 Main St, Emmaus, PA"})
    assert r.status_code == 200, r.text
    assert "s3cret-nx" not in r.text and "nx_pass" not in r.json()
    listing = await client.get("/api/sites")
    assert "s3cret-nx" not in listing.text
    site = await session.scalar(select(Site))
    assert site.nx_pass_enc != "s3cret-nx" and decrypt(site.nx_pass_enc) == "s3cret-nx"
    assert site.address == "100 Main St, Emmaus, PA"
    # Editing: the address can't be cleared once set; a site that never had one can still be saved.
    edit = {"name": "HQ", "host": NX, "nx_user": "portal", "address": "", "connect": False}
    r = await client.put(f"/api/sites/{site.id}", json=edit)
    assert r.status_code == 400 and "address" in r.text
    site.address = ""
    await session.commit()
    assert (await client.put(f"/api/sites/{site.id}", json=edit)).status_code == 200
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


async def test_static_files_are_versioned_so_deploys_never_mix_old_and_new(client, admin):
    from app.static_version import VERSION
    await login(client, admin[1].email)
    page = (await client.get("/sites")).text
    assert f'src="/static/v/{VERSION}/js/sites.js"' in page and f"/static/v/{VERSION}/css/theme.css" in page
    r = await client.get(f"/static/v/{VERSION}/js/common.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and "export" in r.text
    assert (await client.get("/static/v/old-build/js/common.js")).status_code == 200       # stale pages still load
    assert (await client.get("/static/v/x/js/nope.js")).status_code == 404
    assert (await client.get("/static/js/common.js")).headers["cache-control"] == "no-cache"


@respx.mock
async def test_sat_tile_proxy_tiled_then_hires(client, admin, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.routers.tiles import tile_bbox
    monkeypatch.setattr(get_settings(), "tile_cache_dir", str(tmp_path / "tiles"))
    tiled = respx.get("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/16/24671/19014").mock(
        return_value=httpx.Response(200, content=b"JPEG-tiled", headers={"content-type": "image/jpeg"}))
    hires = respx.get(url__startswith="https://imagery.nationalmap.gov/").mock(
        return_value=httpx.Response(200, content=b"JPEG-naip", headers={"content-type": "image/jpeg"}))

    assert (await client.get("/tiles/sat/16/19014/24671.jpg")).status_code == 401
    await login(client, admin[1].email)
    r = await client.get("/tiles/sat/16/19014/24671.jpg")          # z <= 16: the tiled service (note y/x order)
    assert r.status_code == 200 and r.content == b"JPEG-tiled" and r.headers["content-type"] == "image/jpeg"
    for _ in range(2):
        assert (await client.get("/tiles/sat/18/76057/98686.jpg")).content == b"JPEG-naip"
    assert tiled.call_count == 1 and hires.call_count == 1           # repeats come from disk
    assert f"bbox={tile_bbox(18, 76057, 98686)}" in str(hires.calls.last.request.url)
    assert (await client.get("/tiles/sat/20/1/1.jpg")).status_code == 404   # deeper than NAIP goes


@respx.mock
async def test_sat_tile_upstream_timeout_is_a_502(client, admin, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.routers import tiles
    monkeypatch.setattr(get_settings(), "tile_cache_dir", str(tmp_path / "tiles"))
    monkeypatch.setattr(tiles, "_naip_rest_until", 0.0)
    monkeypatch.setattr(tiles, "_naip_fails", [])
    respx.get(url__startswith="https://imagery.nationalmap.gov/").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(url__startswith="https://basemap.nationalmap.gov/").mock(side_effect=httpx.ConnectError("down"))
    await login(client, admin[1].email)
    assert (await client.get("/tiles/sat/18/76057/98686.jpg")).status_code == 502     # both USGS services down


def _jpeg(color=(30, 120, 40)) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(out, "JPEG")
    return out.getvalue()


@respx.mock
async def test_slow_naip_never_holds_up_the_coarse_tiles(client, admin, tmp_path, monkeypatch):
    """2026-10-16: NAIP took 24-30 s+ a tile; with one shared queue of 6 the coarse tiles waited behind it and the 3D
    and top-down views showed nothing. Now NAIP has its own slots and a 12 s limit, and a z17+ tile it can't give
    is cut from the z16 tile at once; after 3 failures NAIP rests for 2 minutes (not asked at all)."""
    import asyncio
    import time
    from app.config import get_settings
    from app.routers import tiles
    monkeypatch.setattr(get_settings(), "tile_cache_dir", str(tmp_path / "tiles"))
    monkeypatch.setattr(tiles, "NAIP_TIMEOUT_S", 0.3)
    monkeypatch.setattr(tiles, "_naip_rest_until", 0.0)
    monkeypatch.setattr(tiles, "_naip_fails", [])

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=b"never", headers={"content-type": "image/jpeg"})
    naip = respx.get(url__startswith="https://imagery.nationalmap.gov/").mock(side_effect=slow)
    tiled = respx.get(url__startswith="https://basemap.nationalmap.gov/").mock(
        return_value=httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"}))
    await login(client, admin[1].email)

    # Ten sharp tiles in flight (more than NAIP's slots) while a coarse tile is asked for: the coarse one is immediate.
    sharp = [asyncio.create_task(client.get(f"/tiles/sat/18/{76057 + i}/98686.jpg")) for i in range(10)]
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    r = await client.get("/tiles/sat/15/9507/12335.jpg")
    assert r.status_code == 200 and time.monotonic() - t0 < 0.5
    done = await asyncio.gather(*sharp)
    assert all(x.status_code == 200 and x.content[:2] == b"\xff\xd8" for x in done)          # pictures, cut from z16
    assert all(x.headers["cache-control"] == "private, max-age=300" for x in done)           # asked again later
    assert tiles.naip_resting()
    asked = naip.call_count
    t0 = time.monotonic()
    r = await client.get("/tiles/sat/19/152114/197372.jpg")                                  # resting: NAIP not asked
    assert r.status_code == 200 and time.monotonic() - t0 < 0.5 and naip.call_count == asked
    assert tiled.call_count >= 2


def test_tile_bbox_is_web_mercator():
    from app.routers.tiles import tile_bbox
    assert tile_bbox(0, 0, 0) == "-20037508.343,-20037508.343,20037508.343,20037508.343"
    minx, miny, maxx, maxy = map(float, tile_bbox(1, 1, 0).split(","))
    assert (minx, miny) == (0.0, 0.0) and maxx == maxy == 20037508.343


async def test_map_page_offers_3d_view_and_cesium_is_served(client, admin):
    await login(client, admin[1].email)
    page = (await client.get("/")).text
    assert 'id="globe"' in page and "/tiles/sat/{z}/{x}/{y}.jpg" in page
    r = await client.get("/static/vendor/cesium/Cesium.js")
    assert r.status_code == 200 and len(r.content) > 1_000_000


def test_tiles_around_covers_the_radius():
    from app.routers.tiles import tiles_around
    tiles = tiles_around(40.51527, -75.523403, 19, 250)     # TWG office; z19 tiles are ~58 m wide here
    assert len(tiles) == 100 and (152155, 197501) in tiles   # the tile the office is on, plus ±4-5 around it
    assert len(tiles_around(40.51527, -75.523403, 17, 250)) == 9


@respx.mock
async def test_prefetch_fills_the_cache_once(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.routers import tiles
    monkeypatch.setattr(get_settings(), "tile_cache_dir", str(tmp_path / "tiles"))
    route = respx.get(url__startswith="https://imagery.nationalmap.gov/").mock(
        return_value=httpx.Response(200, content=b"JPEG", headers={"content-type": "image/jpeg"}))
    got, failed = await tiles.prefetch_site(40.5, -75.5)
    assert failed == 0 and got == route.call_count > 50
    assert await tiles.prefetch_site(40.5, -75.5) == (0, 0)          # all cached now: nothing fetched again


async def test_plain_http_visitors_are_sent_to_https(client):
    """Cloudflare's tunnel answered http:// too; the secure session cookie then never stuck (CSRF failures)."""
    r = await client.get("/login?next=%2Falarms", headers={"cf-visitor": '{"scheme":"http"}'})
    assert r.status_code == 301 and r.headers["location"] == "https://portal.test/login?next=%2Falarms"
    r = await client.post("/login", data={"email": "x"}, headers={"cf-visitor": '{"scheme":"http"}'})
    assert r.status_code == 308 and r.headers["location"].startswith("https://")
    assert (await client.get("/login", headers={"cf-visitor": '{"scheme":"https"}'})).status_code == 200
    assert (await client.get("/login")).status_code == 200                 # LAN / no Cloudflare: untouched


async def test_browser_tab_icon(client):
    r = await client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon" and r.content[:4] == b"\x00\x00\x01\x00"
    page = (await client.get("/login")).text
    assert 'rel="icon"' in page and 'rel="apple-touch-icon"' in page


def test_address_search_retries_without_unit_and_postal_code():
    """OpenStreetMap has buildings, not units: '100 King St W #5600' found nothing (2026-10-23, Toronto)."""
    from app.routers.api import address_variants as v
    assert v("100 King St W #5600, Toronto, ON M5X 1C9, Canada") == [
        "100 King St W #5600, Toronto, ON M5X 1C9, Canada", "100 King St W, Toronto, ON M5X 1C9, Canada",
        "100 King St W, Toronto, ON, Canada"]
    assert v("Unit 4, 2 Rothmere Dr, North York, ON M4N 1V2")[1] == "2 Rothmere Dr, North York, ON M4N 1V2"
    assert v("2 Rothmere Dr Suite 100, Toronto, ON M4N1V2")[1:] == ["2 Rothmere Dr, Toronto, ON M4N1V2", "2 Rothmere Dr, Toronto, ON"]
    assert v("5-123 Main St, Toronto, ON") == ["5-123 Main St, Toronto, ON", "123 Main St, Toronto, ON"]
    assert v("Building 7, 400 Steeles Ave W")[1] == "400 Steeles Ave W"            # "Ste" inside Steeles stays
    assert v("226 West Woodhaven Drive, White Haven, PA 18661") == [
        "226 West Woodhaven Drive, White Haven, PA 18661", "226 West Woodhaven Drive, White Haven, PA"]


async def test_address_search_tries_the_variants_and_never_caches_a_miss(client, session, monkeypatch):
    import httpx
    from app.routers import api
    from tests.conftest import login, make_tenant_user
    asked = []

    class FakeNominatim:                       # only the geocoder's client (the test's own client stays real)
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, **kw):
            asked.append(params["q"])
            found = [] if "#" in params["q"] or "nothing" in params["q"] else [{"display_name": "King St W", "lat": "43.6", "lon": "-79.3"}]
            return httpx.Response(200, json=found, request=httpx.Request("GET", url))
    t, u = await make_tenant_user(session)
    await login(client, u.email)
    monkeypatch.setattr(api.httpx, "AsyncClient", FakeNominatim)
    monkeypatch.setattr(api, "_last_geocode", 0.0)
    api._GEOCODE_CACHE.clear()
    r = (await client.get("/api/geocode", params={"q": "100 King St W #5600, Toronto"})).json()
    assert r[0]["lat"] == 43.6 and asked == ["100 King St W #5600, Toronto", "100 King St W, Toronto"]
    assert (await client.get("/api/geocode", params={"q": "nothing here"})).json() == []
    assert "nothing here" not in api._GEOCODE_CACHE                   # a miss isn't remembered: the next Find asks again


def test_nx_cloud_account_error_says_what_to_do():
    import httpx
    from app.services.sites import describe_http_error
    req = httpx.Request("POST", "https://x.relay.vmsproxy.com/rest/v3/login/sessions")
    forbidden = httpx.Response(403, request=req, json={"error": "4", "errorId": "forbidden",
                               "errorString": "This authorization method is forbidden. Please contact your Site administrator."})
    e = describe_http_error(httpx.HTTPStatusError("403", request=req, response=forbidden))
    assert e.status == "auth_error" and "local NX user" in str(e) and "Nx Cloud account" in str(e)
    plain = httpx.Response(401, request=req)
    assert str(describe_http_error(httpx.HTTPStatusError("401", request=req, response=plain))) == "NX rejected the username or password"

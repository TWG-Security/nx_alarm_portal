"""Cameras placed on a site's 3D view: who can place them, what's stored, and the live view per camera."""
import pytest

from app.models import AuditLog
from sqlalchemy import select
from tests.conftest import login, make_site, make_tenant_user

CAMS = {"cam-1": "Front Gate", "cam-2": "Loading Dock"}


@pytest.fixture(autouse=True)
def nx_cameras(monkeypatch):
    async def fake(site):
        return dict(CAMS)
    monkeypatch.setattr("app.routers.api._site_cameras", fake)


async def test_admin_places_moves_and_removes_a_camera(client, session, admin):
    tenant, user = admin
    site = await make_site(session, tenant)
    await login(client, user.email)

    cams = (await client.get(f"/api/sites/{site.id}/cameras")).json()
    assert [(c["device_id"], c["name"], c["place"]) for c in cams] == [("cam-1", "Front Gate", None), ("cam-2", "Loading Dock", None)]

    r = await client.put(f"/api/sites/{site.id}/cameras/cam-1", json={"lat": 40.1, "lng": -75.2, "heading": 370 - 10, "fov": 90})
    assert r.status_code == 200, r.text
    assert r.json()["camera_places"]["cam-1"] == {"name": "Front Gate", "lat": 40.1, "lng": -75.2, "heading": 0.0, "fov": 90.0, "range_m": 40.0}
    r = await client.put(f"/api/sites/{site.id}/cameras/cam-1", json={"lat": 40.2, "lng": -75.3, "heading": 180})
    assert r.json()["camera_places"]["cam-1"]["lat"] == 40.2 and r.json()["camera_places"]["cam-1"]["heading"] == 180
    assert [c["place"] is not None for c in (await client.get(f"/api/sites/{site.id}/cameras")).json()] == [True, False]
    # A placed camera shows up in the site list the pages load.
    assert "cam-1" in next(s for s in (await client.get("/api/sites")).json() if s["id"] == site.id)["camera_places"]

    assert (await client.put(f"/api/sites/{site.id}/cameras/not-here", json={"lat": 1, "lng": 1})).status_code == 404
    assert (await client.put(f"/api/sites/{site.id}/cameras/cam-2", json={"lat": 91, "lng": 1})).status_code == 422

    assert (await client.delete(f"/api/sites/{site.id}/cameras/cam-1")).json()["camera_places"] == {}
    assert (await client.delete(f"/api/sites/{site.id}/cameras/cam-1")).status_code == 404
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.site_id == site.id))).all()
    assert actions.count("site.camera_placed") == 2 and actions.count("site.camera_removed") == 1


async def test_operators_see_cameras_but_cannot_place_them(client, session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    from app.models import User
    from app.security import hash_password
    from tests.conftest import PASSWORD
    op = User(tenant_id=tenant.id, email="op@twg.test", display_name="op", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    await session.commit()
    await login(client, op.email)
    assert (await client.get(f"/api/sites/{site.id}/cameras")).status_code == 200
    assert (await client.put(f"/api/sites/{site.id}/cameras/cam-1", json={"lat": 1, "lng": 1})).status_code == 403
    assert (await client.delete(f"/api/sites/{site.id}/cameras/cam-1")).status_code == 403


async def test_live_view_only_for_placed_cameras(client, session, admin, monkeypatch):
    tenant, user = admin
    site = await make_site(session, tenant)
    await login(client, user.email)
    assert (await client.get(f"/media/sites/{site.id}/cameras/cam-1/live.jpg")).status_code == 404

    asked = []

    class FakeClient:
        async def get_thumbnail(self, device_id, ts):
            asked.append((device_id, ts))
            return b"\xff\xd8JPEG"
    monkeypatch.setattr("app.routers.stream.manager.client_for", lambda s: FakeClient())
    await client.put(f"/api/sites/{site.id}/cameras/cam-1", json={"lat": 40, "lng": -75})
    r = await client.get(f"/media/sites/{site.id}/cameras/cam-1/live.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8JPEG" and asked == [("cam-1", -1)]

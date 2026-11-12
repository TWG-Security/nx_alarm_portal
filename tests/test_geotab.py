"""Geotab / MyGeotab vehicles on the map (app/geotab.py, trackers.poll_fleetio, /api/tracking/geotab): ZenduONE and
GoFleet customers (the OZINT SOC, 2026-10-26: "ZenduitONE/Go Fleet, which has gps on all 15 cars")."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app import geotab
from app.config import get_settings
from app.models import AuditLog, Tracker, TrackerPoint
from app.services import trackers as svc
from tests.conftest import login, make_tenant_user
from tools import fake_nx

ON = {"enabled": True, "database": "dev", "username": "dev-user", "password": "dev-pass", "poll_s": 60}


@pytest.fixture(autouse=True)
def fake_geotab(monkeypatch):
    monkeypatch.setattr(get_settings(), "geotab_api_base", "http://geotab.test/_geotab")
    monkeypatch.setattr(svc, "fleetio_transport", httpx.ASGITransport(app=fake_nx.app))
    monkeypatch.setattr(svc, "_fleetio_clients", {})
    monkeypatch.setattr(svc, "_fleetio_due", {})
    monkeypatch.setattr(svc, "_refreshed_at", {})
    fake_nx.GEO.update(mode="ok", auth=0, reads=0, calls=[], sessions=set())
    yield fake_nx.GEO
    fake_nx.GEO.update(mode="ok")


async def _admin(client, session):
    tenant, admin = await make_tenant_user(session)
    tenant.kind = "platform"
    await session.commit()
    await login(client, admin.email)
    tenant.id
    return tenant, admin


async def _cars(session, tenant_id):
    session.expire_all()
    return {t.ext_id: t for t in (await session.scalars(select(Tracker).where(Tracker.tenant_id == tenant_id,
                                                                              Tracker.source == "geotab"))).all()}


def test_a_geotab_device_becomes_a_vehicle():
    dev = {"id": "b1", "name": "Patrol 1", "serialNumber": "G9X", "licensePlate": "CVTZ 101"}
    v = geotab.vehicle(dev, {"latitude": 43.7, "longitude": -79.4, "dateTime": "2026-10-26T18:00:00.000Z", "speed": 36})
    assert (v.id, v.name, v.describe, v.lat, v.lng, v.speed) == ("b1", "Patrol 1", "CVTZ 101 · GO G9X", 43.7, -79.4, 10.0)
    assert v.at == datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)                     # km/h -> m/s above
    assert geotab.vehicle(dev, None).at is None and geotab.vehicle(dev, {"latitude": 0, "longitude": 0}).lat is None
    assert geotab.vehicle(dev, {"latitude": 43.7, "longitude": -79.4, "dateTime": "2026-10-26T18:00:00Z",
                                "speed": float("nan")}).speed is None
    assert geotab.vehicle({"name": "no id"}, None) is None
    assert geotab._error_name({"errors": [{"name": "InvalidUserException"}]}) == "InvalidUserException"


async def test_switching_geotab_on_puts_every_vehicle_on_the_map(client, session, fake_geotab):
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    r = await client.put("/api/tracking/geotab", json={**ON, "password": None})
    assert r.status_code == 400                                       # no password saved yet
    r = await client.put("/api/tracking/geotab", json=ON)
    assert r.status_code == 200, r.text
    view = r.json()["geotab"]
    assert view["enabled"] and view["password_set"] and view["database"] == "dev" and view["last_ok_at"]
    assert "dev-pass" not in json.dumps(r.json())                     # the password never comes back
    cars = await _cars(session, tid)
    assert set(cars) == {"b1", "b2", "b3"}                            # the retired device is left out
    assert cars["b1"].name == "Patrol 1" and cars["b1"].device_name == "CVTZ 101 · GO G9A1B2C3D4E5"
    assert cars["b1"].speed == pytest.approx(46 / 3.6, abs=0.01) and cars["b1"].lat == pytest.approx(40.2655)
    assert (datetime.now(timezone.utc) - svc.aware(cars["b2"].fix_at)).total_seconds() > 10000
    assert cars["b3"].lat is None and "Geotab has no position" in cars["b3"].error
    pts = (await session.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id == cars["b1"].id))).all()
    assert len(pts) == 6                                              # first sight: its logged path + the newest
    assert fake_geotab["auth"] == 1
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "tracking.geotab_settings"))
    assert set(row.detail["fields"]) >= {"database", "username", "password", "enabled"} and "dev-pass" not in json.dumps(row.detail)

    # Next round: one session reused (Authenticate is limited to 10 a minute), Patrol 1 moved.
    before = cars["b1"].lat
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    cars = await _cars(session, tid)
    assert cars["b1"].lat > before and fake_geotab["auth"] == 1
    listed = {t["name"]: t for t in (await client.get("/api/trackers")).json()}
    assert listed["Patrol 1"]["source"] == "geotab"


async def test_expired_session_moved_database_and_failures(client, session, fake_geotab):
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    # The database lives on another Geotab server: Authenticate's path is used from then on.
    fake_geotab["mode"] = "moved"
    await client.put("/api/tracking/geotab", json=ON)
    assert set(await _cars(session, tid)) == {"b1", "b2", "b3"}
    paths = {p for p, m, _ in fake_geotab["calls"] if m == "Get"}
    assert paths == {"/apiv1"}
    # Session no longer valid: signed in again once, and the round still works.
    fake_geotab["mode"] = "expire"
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    assert fake_geotab["auth"] == 2
    assert (await client.get("/api/tracking")).json()["geotab"]["last_error"] == ""

    # Over the limit: reported, the next read waits a minute.
    fake_geotab["mode"] = "overlimit"
    svc._fleetio_due.clear()
    wait = await svc.poll_fleetio()
    assert 55 <= wait <= 60 and "rate limit" in (await client.get("/api/tracking")).json()["geotab"]["last_error"]
    assert set(await _cars(session, tid)) == {"b1", "b2", "b3"}       # positions kept

    # Wrong password: said once, and not tried again every round (it could lock the user in Geotab).
    fake_geotab["mode"] = "ok"
    await client.put("/api/tracking/geotab", json={**ON, "password": "wrong"})
    view = (await client.get("/api/tracking")).json()["geotab"]
    assert "refused the database, user name or password" in view["last_error"]
    auth = fake_geotab["auth"]
    for _ in range(3):
        svc._fleetio_due.clear()
        await svc.poll_fleetio()
    assert fake_geotab["auth"] == auth
    r = (await client.post("/api/tracking/geotab/test")).json()
    assert not r["ok"] and "refused" in r["error"] and fake_geotab["auth"] == auth
    await client.put("/api/tracking/geotab", json=ON)                 # saved again: tried at once
    assert (await client.get("/api/tracking")).json()["geotab"]["last_error"] == ""


async def test_test_connection_refresh_and_remove(client, session, fake_geotab):
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    assert (await client.post("/api/tracking/geotab/test")).status_code == 400
    await client.put("/api/tracking/geotab", json={**ON, "enabled": False})
    r = (await client.post("/api/tracking/geotab/test")).json()
    assert r["ok"] and r["vehicles"] == 3 and r["with_position"] == 2 and r["why_none"] == ""
    assert r["detail"] == {"devices": 4, "active": 3, "statuses": 3, "with_position": 2}
    assert await _cars(session, tid) == {}                            # off: the test reads, nothing is added

    await client.put("/api/tracking/geotab", json={**ON, "password": None})        # password kept
    car = (await _cars(session, tid))["b1"]
    before = car.lat
    out = (await client.post(f"/api/trackers/{car.id}/refresh")).json()
    assert out["lat"] != before and out["source"] == "geotab"
    r = await client.delete(f"/api/tracking/trackers/{car.id}")
    assert r.status_code == 400 and "in Geotab" in r.json()["detail"]
    monkey_devices = [d for d in fake_nx._GEO_DEVICES if d["id"] != "b1"]
    saved = list(fake_nx._GEO_DEVICES)
    fake_nx._GEO_DEVICES[:] = monkey_devices
    try:
        svc._fleetio_clients.clear()                                  # the device list is cached for 10 min
        svc._fleetio_due.clear()
        await svc.poll_fleetio()
        assert "Not in Geotab" in (await _cars(session, tid))["b1"].error
        assert (await client.delete(f"/api/tracking/trackers/{car.id}")).status_code == 200
    finally:
        fake_nx._GEO_DEVICES[:] = saved


async def test_fleetio_and_geotab_run_side_by_side(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "fleetio_api_base", "http://fleetio.test/_fleetio")
    fake_nx.FLEET.update(mode="ok", reads=0, archived=set(), entry_reads=0, history_reads=0, version=None)
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account", "api_key": "dev-key"})
    await client.put("/api/tracking/geotab", json=ON)
    session.expire_all()
    sources = {t.source for t in (await session.scalars(select(Tracker).where(Tracker.tenant_id == tid))).all()}
    assert sources == {"fleetio", "geotab"}
    view = (await client.get("/api/tracking")).json()
    assert view["fleetio"]["last_ok_at"] and view["geotab"]["last_ok_at"]

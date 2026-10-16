"""Fleetio vehicles on the map (app/fleetio.py, app/services/trackers.py poll_fleetio, /api/tracking/fleetio)."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app import fleetio
from app.config import get_settings
from app.models import Tracker, TrackerPoint, TrackerSource
from app.services import trackers as svc
from app.services.bus import bus
from tests.conftest import login, make_tenant_user
from tools import fake_nx


@pytest.fixture(autouse=True)
def fake_fleetio(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleetio_api_base", "http://fleetio.test/_fleetio")
    monkeypatch.setattr(svc, "fleetio_transport", httpx.ASGITransport(app=fake_nx.app))
    monkeypatch.setattr(svc, "_fleetio_clients", {})
    monkeypatch.setattr(svc, "_fleetio_due", {})
    monkeypatch.setattr(svc, "_refreshed_at", {})
    fake_nx.FLEET.update(mode="ok", reads=0, archived=set(), entry_reads=0, history_reads=0, version=None)
    yield fake_nx.FLEET
    fake_nx.FLEET.update(mode="ok", archived=set())


async def _admin(client, session):
    tenant, admin = await make_tenant_user(session)
    tenant.kind = "platform"
    await session.commit()
    await login(client, admin.email)
    tenant.id                                 # loaded now: _vans() expires the test session's objects
    return tenant, admin


async def _vans(session, tenant_id):
    session.expire_all()
    return {t.ext_id: t for t in (await session.scalars(select(Tracker).where(Tracker.tenant_id == tenant_id,
                                                                              Tracker.source == "fleetio"))).all()}


def test_a_fleetio_record_becomes_a_vehicle():
    v = fleetio.vehicle({"id": 7, "name": "Van 1", "year": 2020, "make": "Ford", "model": "Transit", "license_plate": "ABC",
                         "current_location_entry": {"date": "2026-10-14T12:00:00-04:00", "address": "Main St",
                                                    "geolocation": {"latitude": 40.1, "longitude": -76.5}}})
    assert (v.id, v.name, v.describe, v.lat, v.lng) == ("7", "Van 1", "2020 Ford Transit · ABC", 40.1, -76.5)
    assert v.at == datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
    assert fleetio.vehicle({"id": 8, "name": "No GPS", "current_location_entry": None}).at is None
    bad = fleetio.vehicle({"id": 9, "current_location_entry": {"date": "2026-10-14T12:00:00Z", "geolocation": {"latitude": 0, "longitude": 0}}})
    assert bad.lat is None and bad.name == "Vehicle 9"               # 0,0 is "no position", not the Gulf of Guinea
    assert fleetio.vehicle({"id": 10, "is_sample": True}) is None and fleetio.vehicle({"name": "no id"}) is None


async def test_switching_fleetio_on_puts_every_van_on_the_map(client, session):
    """The user, 2026-10-14: "we use that here at TWG and want to see all the vans on the map"."""
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    assert (await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account"})).status_code == 400
    sub = bus.subscribe({tid})
    r = await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account", "api_key": "dev-key", "poll_s": 60})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["fleetio"]["enabled"] and view["fleetio"]["api_key_set"] and view["fleetio"]["last_ok_at"]
    assert "dev-key" not in json.dumps(view)                          # the key never comes back
    vans = await _vans(session, tid)
    assert set(vans) == {"501", "502", "503", "504"}                  # three pages of 2 read; the sample vehicle left out
    assert fake_nx.FLEET["version"] == fleetio.API_VERSION            # the version this client was written for
    # Van 20's list record has only the id of its last location: that entry was read, once.
    assert vans["504"].lat == pytest.approx(40.2398) and vans["504"].error == "" and fake_nx.FLEET["entry_reads"] == 1
    assert vans["501"].name == "Van 12" and vans["501"].kind == "vehicle" and vans["501"].enabled
    assert vans["501"].device_name == "2022 Ford Transit 250 · ZKT-4412" and vans["501"].lat == pytest.approx(40.2704)
    assert vans["502"].fix_at and (datetime.now(timezone.utc) - svc.aware(vans["502"].fix_at)).total_seconds() > 7000
    assert vans["503"].lat is None and "no position" in vans["503"].error
    moved = []
    while not sub.queue.empty():
        name, data = sub.queue.get_nowait()
        if name == "tracker.moved":
            moved.append(data["name"])
    assert sorted(moved) == ["Service Truck 3", "Van 12", "Van 20", "Van 7"]   # the map adds them at once
    listed = {t["name"]: t for t in (await client.get("/api/trackers")).json()}
    assert listed["Van 12"]["source"] == "fleetio" and listed["Van 12"]["lat"]

    # The next round: Van 12 moved (a trail point, a heading); Van 7's position is the same (nothing written).
    fix_before = vans["501"].fix_at
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    vans = await _vans(session, tid)
    assert svc.aware(vans["501"].fix_at) >= svc.aware(fix_before) and vans["501"].heading is not None
    points = (await session.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id == vans["501"].id)
                                    .order_by(TrackerPoint.ts))).all()
    # First sight: Fleetio's last hour of Van 12 (5 positions) drawn as its trail; then one point per new read
    # (the user, 2026-10-16: "the fleetio cars do not show trailing"). A read soon after the last: no history call.
    assert len(points) == 7 and fake_nx.FLEET["history_reads"] == 1 + 2          # Van 12 (5 points); Van 7 and 20: none
    assert all(a.ts < b.ts for a, b in zip(points, points[1:]))
    assert len((await session.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id == vans["502"].id))).all()) == 1

    assert fake_nx.FLEET["entry_reads"] == 1                          # the same entry id isn't read again
    # Not due yet: no call to Fleetio.
    reads = fake_nx.FLEET["reads"]
    wait = await svc.poll_fleetio()
    assert fake_nx.FLEET["reads"] == reads and 1 <= wait <= 60

    # Renamed in the portal: kept. Archived in Fleetio: says so, stays on the list with its last position.
    await client.put(f"/api/tracking/trackers/{vans['502'].id}", json={"name": "Matt's van"})
    fake_nx.FLEET["archived"].add(502)
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    vans = await _vans(session, tid)
    assert vans["502"].name == "Matt's van" and "Not in Fleetio" in vans["502"].error and vans["502"].lat is not None


async def test_test_connection_refresh_and_remove(client, session):
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    assert (await client.post("/api/tracking/fleetio/test")).status_code == 400            # nothing saved yet
    await client.put("/api/tracking/fleetio", json={"enabled": False, "account_token": "dev-account", "api_key": "dev-key"})
    r = (await client.post("/api/tracking/fleetio/test")).json()
    assert r["ok"] and r["vehicles"] == 4 and r["with_position"] == 3 and r["newest_position_at"] > r["oldest_position_at"]
    assert r["detail"] == {"vehicles": 4, "entry_in_list": 2, "entry_id_only": 1, "no_entry": 1, "field_missing": 0,
                           "looked_up": 1, "lookup_failed": 0, "with_position": 3} and r["why_none"] == ""
    assert await _vans(session, tid) == {}                     # off: the test reads, nothing is added

    await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account"})   # key kept
    van = (await _vans(session, tid))["501"]
    before = van.lat
    out = (await client.post(f"/api/trackers/{van.id}/refresh")).json()                   # ↻ asks Fleetio for that van
    assert out["lat"] != before and out["source"] == "fleetio"
    r = await client.post(f"/api/trackers/{van.id}/locate")
    assert r.status_code == 200 and r.json()["kind"] == "refreshed"
    # A van that's still in Fleetio can't be removed (it would come straight back): switch it off instead.
    r = await client.delete(f"/api/tracking/trackers/{van.id}")
    assert r.status_code == 400 and "Switch it off" in r.json()["detail"]
    assert (await client.put(f"/api/tracking/trackers/{van.id}", json={"enabled": False})).status_code == 200
    fake_nx.FLEET["archived"].add(501)
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    assert (await client.delete(f"/api/tracking/trackers/{van.id}")).status_code == 200    # gone from Fleetio: can go


async def test_fleetio_failures_are_reported_and_keep_the_vans(client, session, fake_fleetio):
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account", "api_key": "dev-key"})
    vans = await _vans(session, tid)
    fake_fleetio["mode"] = "unauthorized"
    r = (await client.post("/api/tracking/fleetio/test")).json()
    assert not r["ok"] and "refused the API key" in r["error"]
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    view = (await client.get("/api/tracking")).json()["fleetio"]
    assert "refused" in view["last_error"]
    assert set(await _vans(session, tid)) == set(vans)          # positions kept, nothing removed

    # Rate limited: the next read waits for Retry-After (40 s), not the 60 s interval... nor hammers Fleetio.
    fake_fleetio["mode"] = "ratelimit"
    svc._fleetio_due.clear()
    wait = await svc.poll_fleetio()
    assert 30 <= wait <= 60 and "rate limit" in (await client.get("/api/tracking")).json()["fleetio"]["last_error"]
    fake_fleetio["mode"] = "ok"
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    view = (await client.get("/api/tracking")).json()["fleetio"]
    assert view["last_ok_at"] and view["last_error"] == ""


async def test_fleetio_and_incontrol2_status_stay_separate(client, session):
    """An InControl2 round writes InControl2's status only (not Fleetio's), and the IC2 poll never touches vans."""
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account", "api_key": "dev-key"})
    session.expire_all()
    fl = await session.scalar(select(TrackerSource).where(TrackerSource.tenant_id == tid, TrackerSource.kind == "fleetio"))
    ok_at, fl_id = fl.last_ok_at, fl.id
    await svc._store([{"id": -1, "tenant": tid, "error": "IC2 down"}])
    session.expire_all()
    fl = await session.get(TrackerSource, fl_id)
    assert fl.last_ok_at == ok_at and fl.last_error == ""
    assert (await client.get("/api/tracking")).json()["enabled"] is False          # InControl2 itself still off


async def test_test_connection_says_why_there_are_no_positions(client, session, monkeypatch):
    """TWG's first live read: 19 vehicles, none placed. The test tells which of the causes it is."""
    await _admin(client, session)
    await client.put("/api/tracking/fleetio", json={"enabled": False, "account_token": "dev-account", "api_key": "dev-key"})
    bare = lambda: [{"id": 1, "name": "#01 Bucket"}, {"id": 2, "name": "#02 Van"}]                     # no location fields
    monkeypatch.setattr(fake_nx, "_fleet_vehicles", bare)
    r = (await client.post("/api/tracking/fleetio/test")).json()
    assert r["with_position"] == 0 and r["detail"]["field_missing"] == 2 and "no location fields" in r["why_none"]
    monkeypatch.setattr(fake_nx, "_fleet_vehicles", lambda: [dict(v, current_location_entry=None, current_location_entry_id=None) for v in bare()])
    r = (await client.post("/api/tracking/fleetio/test")).json()
    assert r["detail"]["no_entry"] == 2 and "telematics integration" in r["why_none"]
    monkeypatch.setattr(fake_nx, "_fleet_vehicles", lambda: [dict(v, current_location_entry_id=99) for v in bare()])
    r = (await client.post("/api/tracking/fleetio/test")).json()       # the entry reads answer 404
    assert r["detail"]["lookup_failed"] == 2 and "wouldn't let this key read it" in r["why_none"]


async def test_a_long_gap_between_reads_is_filled_from_fleetio_history(client, session):
    """A van read again after longer than the interval (the portal restarted, Fleetio was down, rate limited): the
    positions between are fetched, so the trail shows the road it took, not a straight jump."""
    from datetime import timedelta
    tenant, _ = await _admin(client, session)
    tid = tenant.id
    await client.put("/api/tracking/fleetio", json={"enabled": True, "account_token": "dev-account", "api_key": "dev-key", "poll_s": 60})
    van = (await _vans(session, tid))["501"]
    await session.execute(TrackerPoint.__table__.delete().where(TrackerPoint.tracker_id == van.id))
    van.fix_at = svc.utcnow() - timedelta(minutes=10)          # last read 10 minutes ago
    await session.commit()
    reads = fake_nx.FLEET["history_reads"]
    svc._fleetio_due.clear()
    await svc.poll_fleetio()
    assert fake_nx.FLEET["history_reads"] == reads + 1
    pts = (await session.scalars(select(TrackerPoint).where(TrackerPoint.tracker_id == van.id))).all()
    assert len(pts) == 6                                        # 5 between + the newest
    trail = {t["name"]: t for t in (await client.get("/api/trackers?trail_min=60")).json()}["Van 12"]["trail"]
    assert len(trail) == 6

"""Guard tracking through Peplink InControl2: setup on the Platform page, polling (against the fake IC2 in
tools/fake_nx.py), local-time conversion, trails, failure modes, and who sees which trackers."""
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app import peplink
from app.config import get_settings
from app.models import Tenant, Tracker, TrackerPoint
from app.services import trackers as svc
from app.services.bus import bus
from tests.conftest import login, make_tenant_user
from tools import fake_nx


@pytest.fixture(autouse=True)
def fake_ic2(monkeypatch):
    monkeypatch.setattr(get_settings(), "peplink_api_base", "http://ic2.test/_ic2")
    monkeypatch.setattr(svc, "transport", httpx.ASGITransport(app=fake_nx.app))
    monkeypatch.setattr(svc, "_client", None)
    monkeypatch.setattr(svc, "_per_device_orgs", {})
    fake_nx.IC2.update(mode="ok", stopped_at=None, calls=[])
    yield fake_nx.IC2
    fake_nx.IC2.update(mode="ok", stopped_at=None)


async def _setup(client, session, *, track=True):
    tenant, admin = await make_tenant_user(session)
    tenant.kind = "platform"
    await session.commit()
    await login(client, admin.email)
    r = await client.put("/api/platform/peplink", json={"enabled": True, "client_id": "dev-client", "secret": "dev-secret", "poll_s": 30})
    assert r.status_code == 200, r.text
    if track:
        r = await client.post("/api/platform/trackers", json={"tenant_id": tenant.id, "name": "Patrol 1", "kind": "vehicle",
                                                               "org": "devorg", "group": 38, "device": 3, "serial": "293B-1E37-276B"})
        assert r.status_code == 200, r.text
    return tenant, admin


def test_ic2_local_time_becomes_utc():
    assert peplink.to_utc("2026-10-13T13:57:52", "America/New_York") == datetime(2026, 10, 13, 17, 57, 52, tzinfo=timezone.utc)
    assert peplink.to_utc("2026-01-13T13:57:52", "America/New_York") == datetime(2026, 1, 13, 18, 57, 52, tzinfo=timezone.utc)
    assert peplink.to_utc("2026-10-13T13:57:52", "Not/AZone") == datetime(2026, 10, 13, 13, 57, 52, tzinfo=timezone.utc)
    pts = [{"isExist": False}, {"la": 1, "lo": 2, "sp": 1.5, "ts": "2026-10-13T10:00:00"}, {"la": 3, "lo": 4, "ts": "2026-10-13T10:00:30"}]
    fix = peplink.newest_fix(pts, "UTC")
    assert (fix.lat, fix.lng, fix.speed) == (3, 4, None)
    assert peplink.newest_fix([{"isExist": False}], "UTC") is None


async def test_setup_discovery_and_tracking(client, session):
    tenant, _ = await _setup(client, session, track=False)
    assert (await client.post("/api/platform/peplink/test")).json() == {"ok": True, "organizations": ["TWG Dev (fake)"]}
    devs = (await client.get("/api/platform/peplink/devices")).json()["devices"]
    assert [d["name"] for d in devs] == ["Patrol Car 1", "Parked Trailer"]          # online first; the AP has no GPS
    car = devs[0]
    r = await client.post("/api/platform/trackers", json={"tenant_id": tenant.id, "name": "Patrol 1", "org": car["org"],
                                                           "group": car["group"], "device": car["device"], "serial": car["serial"]})
    assert r.status_code == 200
    assert (await client.post("/api/platform/trackers", json={"tenant_id": tenant.id, "name": "Again", "org": "devorg",
                                                               "group": 38, "device": 3})).status_code == 409
    assert (await client.get("/api/platform/peplink/devices")).json()["devices"][0]["tracked_as"] == "Patrol 1"

    sub = bus.subscribe({tenant.id})
    fake_nx.IC2["stopped_at"] = time.time()
    assert await svc.poll_once() == 30
    t = (await client.get("/api/trackers")).json()[0]
    assert t["name"] == "Patrol 1" and t["error"] == "" and t["lat"] and t["speed"] == 1.57
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(t["fix_at"])).total_seconds()
    assert -1 < age < 25, age                         # New York local time read as UTC would be 4 h off
    event, data = sub.queue.get_nowait()
    assert event == "tracker.moved" and data["id"] == t["id"]

    # It drives on: a new trail point, a heading; the same position again adds nothing.
    fake_nx.IC2["stopped_at"] = time.time() + 60
    await svc.poll_once()
    await svc.poll_once()
    t2 = (await client.get("/api/trackers")).json()[0]
    assert len(t2["trail"]) == 2 and t2["heading"] is not None and t2["fix_at"] > t["fix_at"]
    trail = (await client.get(f"/api/trackers/{t['id']}/trail?minutes=60")).json()["points"]
    assert len(trail) == 2
    # One org-wide call per round (plus the device detail once, for its time zone).
    calls = fake_nx.IC2["calls"]
    assert calls.count("/_ic2/rest/o/devorg/loc") >= 3 and calls.count("/_ic2/rest/o/devorg/g/38/d/3") == 1
    bus.unsubscribe(sub)

    row = await session.scalar(select(Tracker))
    await session.refresh(row)
    assert "must-never-be-stored" not in json.dumps({c.name: str(getattr(row, c.name)) for c in Tracker.__table__.columns})


async def test_failures_mark_trackers_stale_without_losing_them(client, session, monkeypatch):
    await _setup(client, session)
    fake_nx.IC2["stopped_at"] = time.time()
    await svc.poll_once()
    good = (await client.get("/api/trackers")).json()[0]

    fake_nx.IC2["mode"] = "fail"
    await svc.poll_once()
    t = (await client.get("/api/trackers")).json()[0]
    assert "Internal error" in t["error"] and t["lat"] == good["lat"] and t["fix_at"] == good["fix_at"]
    assert "Internal error" in (await client.get("/api/platform/peplink")).json()["last_error"]

    # The org-wide call missing (404): falls back to asking per device.
    fake_nx.IC2.update(mode="no_org_loc", calls=[])
    fake_nx.IC2["stopped_at"] = time.time() + 40
    await svc.poll_once()
    t = (await client.get("/api/trackers")).json()[0]
    assert t["error"] == "" and t["fix_at"] > good["fix_at"] and "/_ic2/rest/o/devorg/g/38/d/3/loc" in fake_nx.IC2["calls"]

    # IC2 hanging: a round ends at the timeout and the alarm loop is never involved.
    monkeypatch.setattr(peplink, "REQUEST_MAX_S", 0.5)
    monkeypatch.setattr(svc, "_client", None)
    monkeypatch.setattr(svc, "_per_device_orgs", {})
    fake_nx.IC2["mode"] = "hang"
    t0 = time.monotonic()
    task = asyncio.create_task(svc.poll_once())
    await asyncio.sleep(0.1)
    probe = time.monotonic()
    await asyncio.sleep(0.01)
    assert time.monotonic() - probe < 0.1                    # the event loop stays free meanwhile
    await task
    assert time.monotonic() - t0 < 5
    assert "didn't answer" in (await client.get("/api/trackers")).json()[0]["error"]


async def test_bad_credentials_and_switching_on(client, session):
    tenant, admin = await make_tenant_user(session)
    tenant.kind = "platform"
    await session.commit()
    await login(client, admin.email)
    r = await client.put("/api/platform/peplink", json={"enabled": True, "client_id": "x"})
    assert r.status_code == 400
    await client.put("/api/platform/peplink", json={"enabled": False, "client_id": "dev-client", "secret": "wrong"})
    r = (await client.post("/api/platform/peplink/test")).json()
    assert not r["ok"] and "refused the Client ID" in r["error"]
    view = (await client.get("/api/platform/peplink")).json()
    assert view["secret_set"] and "secret" not in json.dumps({k: v for k, v in view.items() if k != "secret_set"}).replace("secret_set", "")


async def test_companies_only_see_their_own_trackers(client, session):
    from tests.test_users import _user
    twg, _ = await _setup(client, session)
    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.commit()
    acme_admin = await _user(session, acme, "boss@acme.test", role="admin")
    tid = (await client.get("/api/trackers")).json()[0]["id"]

    await login(client, acme_admin.email)
    assert (await client.get("/api/trackers")).json() == []
    assert (await client.get(f"/api/trackers/{tid}/trail")).status_code == 404
    for method, path in (("GET", "/api/platform/peplink"), ("GET", "/api/platform/peplink/devices"),
                         ("PUT", f"/api/platform/trackers/{tid}"), ("DELETE", f"/api/platform/trackers/{tid}")):
        assert (await client.request(method, path, json={} if method == "PUT" else None)).status_code == 403, path

    # Moving the tracker to Acme: now it's theirs (and gone from TWG's map).
    await login(client, "admin@twgsecurity.com")
    assert (await client.put(f"/api/platform/trackers/{tid}", json={"tenant_id": acme.id})).status_code == 200
    assert (await client.get("/api/trackers")).json() == []
    await login(client, acme_admin.email)
    assert [t["name"] for t in (await client.get("/api/trackers")).json()] == ["Patrol 1"]


async def test_old_points_are_pruned(client, session):
    await _setup(client, session)
    t = await session.scalar(select(Tracker))
    session.add(TrackerPoint(tracker_id=t.id, ts=datetime(2020, 1, 1, tzinfo=timezone.utc), lat=1, lng=1))
    await session.commit()
    svc._last_prune = None
    await svc.poll_once()
    assert (await session.scalar(select(TrackerPoint).where(TrackerPoint.lat == 1))) is None

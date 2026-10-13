"""Guards & vehicles through Peplink InControl2: per-company setup (Sites page), polling against the fake IC2 in
tools/fake_nx.py (shapes copied from the real one), local-time conversion, trails, failure modes, isolation."""
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
    monkeypatch.setattr(svc, "_clients", {})
    fake_nx.IC2.update(mode="ok", stopped_at=None, calls=[], names={})
    monkeypatch.setattr(svc, "_names_at", {})
    monkeypatch.setattr(svc, "_refreshed_at", {})
    yield fake_nx.IC2
    fake_nx.IC2.update(mode="ok", stopped_at=None)


async def _setup(client, session, *, track=True):
    tenant, admin = await make_tenant_user(session)
    tenant.kind = "platform"
    await session.commit()
    await login(client, admin.email)
    r = await client.put("/api/tracking/source", json={"enabled": True, "client_id": "dev-client", "secret": "dev-secret", "poll_s": 30})
    assert r.status_code == 200, r.text
    if track:
        r = await client.post("/api/tracking/trackers", json={"name": "Patrol 1", "kind": "vehicle",
                                                               "org": "devorg", "group": 38, "device": 3, "serial": "293B-1E37-276B"})
        assert r.status_code == 200, r.text
    return tenant, admin


def test_ic2_local_time_becomes_utc():
    assert peplink.to_utc("2026-10-13T13:57:52", "America/New_York") == datetime(2026, 10, 13, 17, 57, 52, tzinfo=timezone.utc)
    assert peplink.to_utc("2026-01-13T13:57:52", "America/New_York") == datetime(2026, 1, 13, 18, 57, 52, tzinfo=timezone.utc)
    assert peplink.to_utc("2026-10-13T13:57:52", "Not/AZone") == datetime(2026, 10, 13, 13, 57, 52, tzinfo=timezone.utc)
    pts = [{"isExist": False}, {"la": 1, "lo": 2, "sp": 1.5, "ts": "2026-10-13T10:00:00", "isExist": True},
           {"la": 3, "lo": 4, "ts": "2026-10-13T10:00:30", "isExist": True},
           {"la": 5, "lo": 6, "ts": "2026-10-13T10:00:59", "isExist": False, "isStatic": True}]      # no GPS: configured address
    fix = peplink.newest_fix(pts, "UTC")
    assert (fix.lat, fix.lng, fix.speed) == (3, 4, None)
    assert peplink.newest_fix([{"isExist": False}], "UTC") is None


async def test_setup_discovery_and_tracking(client, session):
    tenant, _ = await _setup(client, session, track=False)
    assert (await client.post("/api/tracking/test")).json()["organizations"] == ["TWG Dev (fake)", "Blocked Customer (fake)"]
    # Organization → Group → Router.
    orgs = (await client.get("/api/tracking/orgs")).json()
    assert [o["name"] for o in orgs] == ["Blocked Customer (fake)", "TWG Dev (fake)"]
    r = await client.get("/api/tracking/orgs/blocked/groups")
    assert r.status_code == 502 and "API access blocked" in r.json()["detail"]
    groups = (await client.get("/api/tracking/orgs/devorg/groups")).json()
    assert [(g["id"], g["name"], g["online"]) for g in groups] == [(2, "Office", 1), (38, "Security Pod", 1)]
    devs = (await client.get("/api/tracking/orgs/devorg/groups/38/devices")).json()
    assert [(d["name"], d["gps"]) for d in devs] == [("Patrol Car 1", "live"), ("Parked Trailer", "live")]
    seen = (datetime.now(timezone.utc) - datetime.fromisoformat(devs[0]["fix_at"])).total_seconds()
    assert -1 < seen < 25                             # local time converted with the group's clock
    office = (await client.get("/api/tracking/orgs/devorg/groups/2/devices")).json()
    assert [(d["name"], d["gps"]) for d in office] == [("Office AP", "none")]        # its configured address isn't GPS
    assert (await client.get("/api/tracking/orgs/..%2Fx/groups")).status_code in (404, 422)
    assert "/_ic2/rest/o/devorg/g/38/d" not in fake_nx.IC2["calls"]                 # the list that carries admin passwords
    car = devs[0]
    r = await client.post("/api/tracking/trackers", json={"name": "Patrol 1", "org": car["org"],
                                                           "group": car["group"], "device": car["device"], "serial": car["serial"]})
    assert r.status_code == 200
    assert (await client.post("/api/tracking/trackers", json={"name": "Again", "org": "devorg",
                                                               "group": 38, "device": 3})).status_code == 409
    assert (await client.get("/api/tracking/orgs/devorg/groups/38/devices")).json()[0]["tracked_as"] == "Patrol 1"

    sub = bus.subscribe({tenant.id})
    fake_nx.IC2["calls"].clear()
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
    # One live per-device call per round; never the org-wide list (a stale cache), never the device detail
    # (it carries the router's admin password, and the time zone comes with the location).
    calls = fake_nx.IC2["calls"]
    assert calls.count("/_ic2/rest/o/devorg/g/38/d/3/loc") >= 3
    assert "/_ic2/rest/o/devorg/loc" not in calls and "/_ic2/rest/o/devorg/g/38/d/3" not in calls
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
    assert "Internal error" in (await client.get("/api/tracking")).json()["last_error"]

    # Recovery.
    fake_nx.IC2.update(mode="ok")
    fake_nx.IC2["stopped_at"] = time.time() + 40
    await svc.poll_once()
    t = (await client.get("/api/trackers")).json()[0]
    assert t["error"] == "" and t["fix_at"] > good["fix_at"]

    # IC2 hanging: a round ends at the timeout and the alarm loop is never involved.
    monkeypatch.setattr(peplink, "REQUEST_MAX_S", 0.5)
    monkeypatch.setattr(svc, "_clients", {})
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
    r = await client.put("/api/tracking/source", json={"enabled": True, "client_id": "x"})
    assert r.status_code == 400
    await client.put("/api/tracking/source", json={"enabled": False, "client_id": "dev-client", "secret": "wrong"})
    r = (await client.post("/api/tracking/test")).json()
    assert not r["ok"] and "refused the Client ID" in r["error"]
    view = (await client.get("/api/tracking")).json()
    assert view["secret_set"] and "secret" not in json.dumps({k: v for k, v in view.items() if k != "secret_set"}).replace("secret_set", "")


async def test_each_company_has_its_own_credentials_and_guards(client, session):
    """Acme's admins set up their own InControl2 account and see only their guards; nobody else's settings
    or trackers are reachable; TWG reaches Acme's only in support mode, like everything else."""
    from tests.test_users import _user
    twg, _ = await _setup(client, session)
    twg_tracker = (await client.get("/api/trackers")).json()[0]["id"]
    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.commit()
    acme_admin = await _user(session, acme, "boss@acme.test", role="admin")
    acme_op = await _user(session, acme, "guard@acme.test", role="operator")

    await login(client, acme_admin.email)
    view = (await client.get("/api/tracking")).json()
    assert view["company"] == "Acme" and view["client_id"] == "" and view["trackers"] == []      # not TWG's
    assert (await client.get("/api/trackers")).json() == []
    assert (await client.get(f"/api/trackers/{twg_tracker}/trail")).status_code == 404
    for method, path in (("PUT", f"/api/tracking/trackers/{twg_tracker}"), ("DELETE", f"/api/tracking/trackers/{twg_tracker}")):
        assert (await client.request(method, path, json={"name": "x"} if method == "PUT" else None)).status_code == 404, path
    assert (await client.post("/api/tracking/test")).status_code == 400                # no credentials of their own yet
    r = await client.put("/api/tracking/source", json={"enabled": True, "client_id": "dev-client", "secret": "dev-secret"})
    assert r.status_code == 200
    r = await client.post("/api/tracking/trackers", json={"name": "Acme Patrol", "org": "devorg", "group": 38, "device": 3})
    assert r.status_code == 200 and [t["name"] for t in r.json()["trackers"]] == ["Acme Patrol"]
    fake_nx.IC2["stopped_at"] = time.time()
    await svc.poll_once()
    assert [t["name"] for t in (await client.get("/api/trackers")).json()] == ["Acme Patrol"]

    await login(client, acme_op.email)                    # operators see the guards, not the setup
    assert [t["name"] for t in (await client.get("/api/trackers")).json()] == ["Acme Patrol"]
    assert (await client.get("/api/tracking")).status_code == 403

    await login(client, "admin@twgsecurity.com")          # TWG: its own, unless it opens Acme in support mode
    assert [t["name"] for t in (await client.get("/api/trackers")).json()] == ["Patrol 1"]
    await client.post("/api/scope", json={"scope": str(acme.id)})
    assert (await client.get("/api/tracking")).json()["company"] == "Acme"


async def test_platform_credentials_move_to_twg(session):
    """Migration 0020: the InControl2 client saved on the Platform page becomes TWG's own."""
    import importlib.util
    from sqlalchemy import text
    from app import db as db_module
    from app.security import encrypt
    from app import platform_settings
    tenant, _ = await make_tenant_user(session)
    tenant.kind = "platform"
    row = await platform_settings.get_row(session)
    row.peplink_enabled, row.peplink_client_id, row.peplink_secret_enc, row.peplink_poll_s = True, "cid", encrypt("sec"), 45
    await session.execute(text("drop table tracker_sources"))
    await session.commit()
    spec = importlib.util.spec_from_file_location("m0020", "migrations/versions/0020_tracker_sources.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def run(conn):
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
    async with db_module.engine().begin() as conn:
        await conn.run_sync(run)
    from app.models import TrackerSource
    src = await session.scalar(select(TrackerSource))
    assert src.tenant_id == tenant.id and src.client_id == "cid" and src.enabled and src.poll_s == 45


async def test_old_points_are_pruned(client, session):
    await _setup(client, session)
    t = await session.scalar(select(Tracker))
    session.add(TrackerPoint(tracker_id=t.id, ts=datetime(2020, 1, 1, tzinfo=timezone.utc), lat=1, lng=1))
    await session.commit()
    svc._last_prune = None
    await svc.poll_once()
    assert (await session.scalar(select(TrackerPoint).where(TrackerPoint.lat == 1))) is None


async def test_refresh_now_and_router_renames(client, session, monkeypatch):
    """↻ asks InControl2 at once (anyone who sees the guard; at most every 5 s); a router renamed in InControl2
    renames a guard still called after it, but a name given in the portal stays."""
    from tests.test_users import _user
    tenant, admin = await _setup(client, session, track=False)
    for name, dev in (("Patrol Car 1", 3), ("Night Trailer", 7)):          # 7 is named "Parked Trailer" in IC2
        await client.post("/api/tracking/trackers", json={"name": name, "org": "devorg", "group": 38, "device": dev,
                                                           "device_name": "Patrol Car 1" if dev == 3 else "Parked Trailer"})
    car = next(t for t in (await client.get("/api/trackers")).json() if t["name"] == "Patrol Car 1")
    op = await _user(session, tenant, "noc@twgsecurity.com")
    await login(client, op.email)
    fake_nx.IC2["calls"].clear()
    fake_nx.IC2["stopped_at"] = time.time()
    r = await client.post(f"/api/trackers/{car['id']}/refresh")
    assert r.status_code == 200 and r.json()["lat"] is not None
    assert fake_nx.IC2["calls"].count("/_ic2/rest/o/devorg/g/38/d/3/loc") == 1
    await client.post(f"/api/trackers/{car['id']}/refresh")                  # within 5 s: not asked again
    assert fake_nx.IC2["calls"].count("/_ic2/rest/o/devorg/g/38/d/3/loc") == 1

    # Renamed in InControl2: picked up on the next name check (forced by a refresh here).
    fake_nx.IC2["names"].update({3: "Patrol Car 7", 7: "Trailer West"})
    monkeypatch.setattr(svc, "_refreshed_at", {})
    names = {t["id"]: t for t in (await client.get("/api/trackers")).json()}
    for tid in names:
        await client.post(f"/api/trackers/{tid}/refresh")
    after = {t["id"]: (t["name"], t["device_name"]) for t in (await client.get("/api/trackers")).json()}
    trailer_id = next(i for i, t in names.items() if t["name"] == "Night Trailer")
    assert after[car["id"]] == ("Patrol Car 7", "Patrol Car 7")             # followed the router
    assert after[trailer_id] == ("Night Trailer", "Trailer West")            # the portal name stays

    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.commit()
    other = await _user(session, acme, "x@acme.test", role="admin")
    await login(client, other.email)
    assert (await client.post(f"/api/trackers/{car['id']}/refresh")).status_code == 404


async def test_tracking_a_router_switches_tracking_on(client, session):
    tenant, admin = await make_tenant_user(session)
    await login(client, admin.email)
    await client.put("/api/tracking/source", json={"enabled": False, "client_id": "dev-client", "secret": "dev-secret"})
    assert not (await client.get("/api/tracking")).json()["enabled"]
    r = await client.post("/api/tracking/trackers", json={"name": "Mikey Mobile", "org": "devorg", "group": 38, "device": 3})
    assert r.json()["enabled"] is True
    fake_nx.IC2["stopped_at"] = time.time()
    await svc.poll_once()
    assert (await client.get("/api/trackers")).json()[0]["lat"] is not None

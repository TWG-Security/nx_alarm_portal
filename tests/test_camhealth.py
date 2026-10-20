"""Camera image health (app/camhealth): the checks on synthetic scenes, the raise / clear rules, the running
background, the worker against a fake NX, and the page's API (permissions, Accept view, mute)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.camhealth import analyze as A
from app.camhealth import state as S
from app.camhealth.worker import Cam, SiteConn, Worker
from app.models import AuditLog, CameraHealth, CameraHealthEvent, CameraHealthImage, Site
from tests.conftest import login, make_site, make_tenant_user
from tools import health_scene as H


def frame(mode="normal", seed=None):
    return A.load(H.scene(mode, seed))


def kinds(res):
    return sorted(i["kind"] for i in res.issues)


# ---- the checks --------------------------------------------------------------------------------------------------
REF = None


def ref():
    global REF
    REF = REF or frame("normal", 1)
    return REF


@pytest.mark.parametrize("mode,expected,severity", [
    ("blur", ["blur"], "problem"), ("covered", ["covered"], "problem"), ("dark", ["dark"], "problem"),
    ("glare", ["glare"], "problem"), ("moved", ["moved"], "problem"), ("change", ["scene_change"], "warning"),
    ("blocked", ["blocked"], "warning"),
])
def test_each_defect_is_found(mode, expected, severity):
    res = A.analyze(frame(mode, 2), ref())
    assert kinds(res) == expected, res.issues
    assert res.issues[0]["severity"] == severity


@pytest.mark.parametrize("mode", ["normal", "dusk"])
def test_noise_and_lighting_are_not_problems(mode):
    assert A.analyze(frame(mode, 3), ref()).issues == []


def test_boxes_sit_on_the_changed_area():
    [i] = A.analyze(frame("change", 4), ref()).issues
    [box] = i["boxes"]
    x, y, w, h = H.CHANGE_BOX
    truth = [x / H.W, y / H.H, w / H.W, h / H.H]
    assert S.iou(box, truth) > 0.6, (box, truth)
    [blocked] = A.analyze(frame("blocked", 4), ref()).issues
    x, y, w, h = H.BLOCKED_BOX
    assert S.iou(blocked["boxes"][0], [x / H.W, y / H.H, w / H.W, h / H.H]) > 0.7


def test_moved_reports_where_the_reference_view_went():
    [i] = A.analyze(frame("moved", 5), ref()).issues
    assert "right" in i["detail"] and "down" in i["detail"]
    (x0, y0) = i["poly"][0]                       # the reference's top-left corner, now 90 px right / 40 px down
    assert abs(x0 - 90 / H.W) < 0.01 and abs(y0 - 40 / H.H) < 0.01


def test_black_or_flat_pictures_are_compared_with_the_day_reference():
    """No colour doesn't make a black picture "night (infrared)": it would dodge the reference."""
    assert frame("dark").mode == frame("covered").mode == "day"
    assert frame("night").mode == "night"


def test_without_a_reference_only_the_plain_checks_run():
    res = A.analyze(frame("normal", 6), None)
    assert res.issues == [] and res.usable_as_reference
    assert kinds(A.analyze(frame("covered"), None)) == ["covered"]
    assert not A.analyze(frame("covered"), None).usable_as_reference


def test_frozen_means_the_very_same_picture():
    a, b = frame("frozen"), frame("frozen")
    assert A.frozen(b, a.small)
    assert not A.frozen(frame("normal", 7), frame("normal", 8).small)


def test_ir_in_daylight():
    noon = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)          # 13:00 in Pennsylvania
    night = datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc)
    nref = frame("night", 1)
    assert kinds(A.analyze(frame("night", 2), nref, lat=40.6, lng=-75.4, now=noon)) == ["ir_day"]
    assert A.analyze(frame("night", 2), nref, lat=40.6, lng=-75.4, now=night).issues == []
    assert A.sun_elevation(40.6, -75.4, noon) > 60 and A.sun_elevation(40.6, -75.4, night) < -10


def test_ptz_cameras_skip_the_view_checks():
    """Looking elsewhere: only the plain checks. Back at the reference position: blur etc. still count."""
    assert A.analyze(frame("moved", 9), ref(), view_checks=False).issues == []
    assert A.analyze(frame("change", 9), ref(), view_checks=False).issues == []
    assert kinds(A.analyze(frame("blocked", 9), ref(), view_checks=False)) == ["blocked"]
    assert kinds(A.analyze(frame("covered", 9), ref(), view_checks=False)) == ["covered"]


def test_the_background_follows_slow_light_but_keeps_a_real_change():
    """Ten checks while the light fades (dusk-like gain sweeping across): nothing; then an object that stays: it's
    raised after three checks and stays raised (its cells are never taken into the background)."""
    import cv2
    import numpy as np
    base, bg, st, raised = ref(), None, None, []
    for i in range(16):
        img = H.render("change" if i >= 10 else "normal", 100 + i).astype(np.float32)
        img *= np.linspace(1.0 - i * 0.03, 1.0, H.W, dtype=np.float32)[None, :, None]
        f = A.load(cv2.imencode(".jpg", np.clip(img, 0, 255).astype(np.uint8))[1].tobytes())
        res = A.analyze(f, base, bg=bg)
        if res.absorb is not None:
            bg = A.frame_from_bgr(A.compose_background(bg or base, f, res.absorb))
        issues, st, events = S.update(st, res.issues, i * 300_000)
        raised += [(i, e["kind"]) for e in events if e["change"] == "raised"]
        if i == 15:
            assert [x["kind"] for x in issues] == ["scene_change"]
    assert raised == [(12, "scene_change")], raised


# ---- raise / clear -----------------------------------------------------------------------------------------------
def test_an_issue_shows_after_two_checks_and_clears_after_two_clean_ones():
    blur = A.issue("blur", boxes=[[0, 0, 1, 1]])
    st, seen = None, []
    for i, found in enumerate([[blur], [blur], [blur], [], [blur], [], []]):
        issues, st, events = S.update(st, found, i)
        seen.append(([x["kind"] for x in issues], [e["change"] for e in events]))
    assert seen == [([], []), (["blur"], ["raised"]), (["blur"], []), (["blur"], []), (["blur"], []), (["blur"], []),
                    ([], ["cleared"])]


def test_a_scene_change_must_stay_in_one_place():
    """A person walking through is in a different spot every check: never raised."""
    st = None
    for i, x in enumerate([0.1, 0.4, 0.7, 0.2, 0.8]):
        issues, st, events = S.update(st, [A.issue("scene_change", boxes=[[x, 0.5, 0.1, 0.3]])], i)
        assert issues == [] and events == []
    raised = []
    for i in range(3):                     # something that stays put: raised on its 3rd check
        issues, st, events = S.update(st, [A.issue("scene_change", boxes=[[0.5, 0.5, 0.12, 0.3]])], 10 + i)
        raised += [(i, e["change"]) for e in events]
    assert raised == [(2, "raised")] and issues[0]["boxes"] == [[0.5, 0.5, 0.12, 0.3]]


# ---- the worker against a fake NX --------------------------------------------------------------------------------
class FakeNX:
    def __init__(self):
        self.mode = "normal"
        self.devices = [{"id": "{cam-rec}", "name": "Gate", "deviceType": "Camera", "status": "Recording",
                         "mediaStreams": [{"resolution": "1920x1080"}]},
                        {"id": "{cam-live}", "name": "Dock", "deviceType": "Camera", "status": "Online",
                         "mediaStreams": [{"resolution": "1280x720"}]},
                        {"id": "{cam-off}", "name": "Roof", "deviceType": "Camera", "status": "Offline"},
                        {"id": "{horn}", "name": "Horn", "deviceType": "HornSpeaker", "status": "Online"}]
        self.calls = []

    async def list_devices(self):
        return self.devices

    def _frame(self):
        if self.mode == "error":
            import httpx
            raise httpx.ConnectError("relay down")
        return b"" if self.mode == "none" else H.scene(self.mode)

    async def get_thumbnail(self, dev, ts, width=640):
        self.calls.append(("image", dev, width))
        return self._frame()

    async def grab_live_jpeg(self, dev, resolution):
        self.calls.append(("mjpeg", dev, resolution))
        return self._frame()


async def _worker(session, site):
    w = Worker()
    nx = FakeNX()
    conn = SiteConn(site, nx)
    w.sites[site.id] = conn
    await w.refresh_devices(conn)
    return w, nx


async def _row(session, site, dev):
    q = select(CameraHealth).where(CameraHealth.site_id == site.id, CameraHealth.device_id == dev)
    return await session.scalar(q.execution_options(populate_existing=True))      # the worker wrote it elsewhere


async def test_worker_learns_a_reference_then_raises_and_clears(session):
    tenant, _ = await make_tenant_user(session)
    site = await make_site(session, tenant)
    w, nx = await _worker(session, site)
    assert sorted(dev for _, dev in w.cams) == ["cam-live", "cam-off", "cam-rec"]       # video only, no horn
    assert (await _row(session, site, "cam-off")).status == "offline"
    cam = w.cams[(site.id, "cam-rec")]

    await w.check(cam)                     # 1st frame: a candidate; 2nd agrees -> the day reference
    assert (await _row(session, site, "cam-rec")).learning
    await w.check(cam)
    row = await _row(session, site, "cam-rec")
    assert not row.learning and row.status == "ok"
    refs = (await session.scalars(select(CameraHealthImage.kind).where(CameraHealthImage.device_id == "cam-rec"))).all()
    assert sorted(refs) == ["last", "ref_day"]

    nx.mode = "blur"
    await w.check(cam)
    assert (await _row(session, site, "cam-rec")).status == "ok"                # one frame never raises
    await w.check(cam)
    row = await _row(session, site, "cam-rec")
    assert row.status == "problem" and [i["kind"] for i in row.issues] == ["blur"]
    nx.mode = "normal"
    await w.check(cam)
    await w.check(cam)
    row = await _row(session, site, "cam-rec")
    assert row.status == "ok" and row.issues == []
    events = (await session.scalars(select(CameraHealthEvent.change).where(CameraHealthEvent.device_id == "cam-rec")
                                    .order_by(CameraHealthEvent.id))).all()
    assert events == ["reference", "raised", "cleared"]
    # Recording camera: NX's /image; the others: one frame off the live stream at their own aspect.
    await w.check(w.cams[(site.id, "cam-live")])
    assert ("image", "cam-rec", 1280) in nx.calls and ("mjpeg", "cam-live", "1280x720") in nx.calls


async def test_worker_no_picture_and_nx_errors(session):
    tenant, _ = await make_tenant_user(session)
    site = await make_site(session, tenant)
    w, nx = await _worker(session, site)
    cam = w.cams[(site.id, "cam-rec")]
    nx.mode = "none"
    await w.check(cam)
    nx.mode = "error"
    await w.check(cam)
    row = await _row(session, site, "cam-rec")
    assert row.status == "problem" and row.issues[0]["kind"] == "no_picture" and "reach" in row.error


async def test_worker_drops_cameras_nx_no_longer_has(session):
    tenant, _ = await make_tenant_user(session)
    site = await make_site(session, tenant)
    w, nx = await _worker(session, site)
    nx.devices = nx.devices[:1]
    await w.refresh_devices(w.sites[site.id])
    devs = (await session.scalars(select(CameraHealth.device_id).where(CameraHealth.site_id == site.id))).all()
    assert devs == ["cam-rec"] and list(w.cams) == [(site.id, "cam-rec")]


# ---- the page's API ----------------------------------------------------------------------------------------------
async def _seed(session, tenant, site):
    row = CameraHealth(tenant_id=tenant.id, site_id=site.id, device_id="cam-1", name="Gate", status="warning",
                       nx_status="Recording", mode="day", learning=False, settings={}, metrics={},
                       state={"streak": {"scene_change": 3}, "miss": {}, "shown": {}, "tracks": {}},
                       issues=[A.issue("scene_change", boxes=[[0.1, 0.1, 0.2, 0.2]]) | {"since_ms": 1}],
                       checked_at=datetime.now(timezone.utc))
    session.add(row)
    session.add(CameraHealthImage(site_id=site.id, device_id="cam-1", kind="last", jpeg=H.scene("change", 1)))
    session.add(CameraHealthImage(site_id=site.id, device_id="cam-1", kind="ref_day", jpeg=H.scene("normal", 1)))
    await session.commit()
    return row


async def test_page_api_and_who_may_do_what(client, session):
    tenant, admin = await make_tenant_user(session)
    _, op = await make_tenant_user(session, tenant_name="x", email="op@twgsecurity.com", role="operator")
    op.tenant_id = tenant.id
    await session.commit()
    site = await make_site(session, tenant)
    await _seed(session, tenant, site)

    await login(client, op.email)
    r = (await client.get("/api/camera-health")).json()
    assert [c["name"] for c in r["cameras"]] == ["Gate"] and r["summary"]["warning"] == 1
    assert (await client.get("/api/camera-health/summary")).json() == {"total": {"warning": 1}, "sites": {str(site.id): {"warning": 1}}}
    d = (await client.get(f"/api/camera-health/{site.id}/cam-1")).json()
    assert d["issues"][0]["boxes"] == [[0.1, 0.1, 0.2, 0.2]] and set(d["refs"]) == {"last", "ref_day"}
    img = await client.get(f"/media/camera-health/{site.id}/cam-1/last.jpg")
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    assert (await client.get(f"/media/camera-health/{site.id}/cam-1/secret.jpg")).status_code == 404
    assert (await client.post(f"/api/camera-health/{site.id}/cam-1/check")).status_code == 200
    # Accepting a view or muting could hide tampering: administrators only.
    for method, path, body in (("POST", "accept", None), ("POST", "mute", {"hours": 24}), ("PUT", "settings", {"skip": True})):
        assert (await client.request(method, f"/api/camera-health/{site.id}/cam-1/{path}", json=body)).status_code == 403
    assert (await client.get("/health")).status_code == 200

    await login(client, admin.email)
    assert (await client.post(f"/api/camera-health/{site.id}/cam-1/accept")).status_code == 200
    row = await _row(session, site, "cam-1")
    assert row.issues == [] and row.status == "ok" and row.check_requested_at is not None
    assert "scene_change" not in row.state["streak"]
    ref = await session.scalar(select(CameraHealthImage).where(CameraHealthImage.kind == "ref_day")
                               .execution_options(populate_existing=True))
    assert ref.jpeg == H.scene("change", 1) and ref.source == "user" and ref.by_user_id == admin.id
    r = await client.post(f"/api/camera-health/{site.id}/cam-1/mute", json={"hours": 24})
    assert r.json()["muted_until"] > 0
    assert (await client.get("/api/camera-health")).json()["cameras"][0]["muted"]
    r = await client.put(f"/api/camera-health/{site.id}/cam-1/settings", json={"view_checks": False})
    assert r.json()["settings"] == {"view_checks": False}
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.action.like("camera.%")))).all()
    assert sorted(actions) == ["camera.health_muted", "camera.health_settings", "camera.reference_accepted"]


async def test_watch_publishes_changes(session, monkeypatch):
    import asyncio
    from app.routers import camhealth
    from app.services.bus import bus
    tenant, _ = await make_tenant_user(session)
    site = await make_site(session, tenant)
    sub = bus.subscribe(tenant.id)
    monkeypatch.setattr(camhealth.get_settings(), "camhealth_watch_s", 0.05)
    task = asyncio.create_task(camhealth.watch())
    await asyncio.sleep(0.1)
    await _seed(session, tenant, site)
    event, data = await asyncio.wait_for(sub.queue.get(), 2)
    task.cancel()
    bus.unsubscribe(sub)
    assert event == "camera.health" and data["device_id"] == "cam-1" and data["kinds"] == ["scene_change"]

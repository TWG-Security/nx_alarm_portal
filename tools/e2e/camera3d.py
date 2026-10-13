"""3D cameras on the 3D view: a camera body at its mounting height, a 3D view cone, its live picture at the cone's
end, View from camera, and Calibrate (drag / keys / numbers, Save), with alarms timed throughout.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.camera3d [screenshot_dir]

Headless Chromium only has software WebGL: twg-3d-force=2 (no graphics check, no frame watchdog) drives the UI;
tools/e2e/globe.py covers the safety checks.
"""
import json
import math
import sys
import time

from playwright.sync_api import sync_playwright

from tools.e2e.common import inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
SITE = (40.2665, -76.8838)


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


API = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
    return [r.status, await r.json()]; }"""
# Where things are in the scene: the camera body's height above the ground, and the view's pose.
BODY = """() => { const v = window.__globe, e = v.entities.values.find(x => x.box && x.camera);
    if (!e) return null; const c = Cesium.Cartographic.fromCartesian(e.position.getValue(v.clock.currentTime));
    return { h: c.height, lat: Cesium.Math.toDegrees(c.latitude), lng: Cesium.Math.toDegrees(c.longitude) }; }"""
VIEW = """() => { const c = window.__globe.camera, g = c.positionCartographic, f = c.frustum;
    const hfov = 2 * Math.atan(Math.tan(f.fovy / 2) * f.aspectRatio);          // across the view's width
    return { h: g.height, heading: Cesium.Math.toDegrees(c.heading), pitch: Cesium.Math.toDegrees(c.pitch), fov: Cesium.Math.toDegrees(hfov) }; }"""


def alarm_ms(pg):
    t = time.time()
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = round((time.time() - t) * 1000)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    return ms


with sync_playwright() as p:
    b = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = b.new_context(viewport={"width": 1500, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    cams = pg.evaluate(API, ["/api/sites/1/cameras", "GET", None])[1]
    dev = cams[0]["device_id"]
    # A camera 25 m south of the site pin on a 6 m pole, facing north, tilted 12° down, 70° wide.
    st, _ = pg.evaluate(API, [f"/api/sites/1/cameras/{dev}", "PUT", {"lat": SITE[0] - 25 / 111320, "lng": SITE[1], "heading": 0,
                                                                      "fov": 70, "range_m": 45, "height_m": 6, "tilt": 12}])
    check(st == 200, "a camera placed with a 3D pose (6 m up, tilted 12°)")
    pg.evaluate("localStorage.setItem('twg-3d-force', '2')")
    pg.reload()
    pg.wait_for_timeout(2000)
    pg.click(".site-item")
    pg.click("[data-globe]")
    pg.wait_for_function("!!window.__globe && document.getElementById('globe-msg').hidden", timeout=30000)
    pg.wait_for_timeout(5000)

    body = pg.evaluate(BODY)
    check(body is not None and abs(body["h"] - 6) < 0.3, f"camera body at its mounting height ({body and round(body['h'], 2)} m)")
    faces = pg.evaluate("() => window.__globe.entities.values.filter(x => x.camera && x.polygon && x.polygon.perPositionHeight?.getValue()).length")
    check(faces == 5, f"a 3D view cone: 4 sides + the far end ({faces} faces)")
    # The cone's far corners: 45 m out, the top edge above the bottom, centred on north.
    geo = pg.evaluate("""() => { const v = window.__globe, e = v.entities.values.find(x => x.camera && x.polyline && x.polyline.positions?.getValue(v.clock.currentTime)?.length === 10);
        return e.polyline.positions.getValue(v.clock.currentTime).slice(0, 4).map(p => { const c = Cesium.Cartographic.fromCartesian(p);
          return [Cesium.Math.toDegrees(c.latitude), Cesium.Math.toDegrees(c.longitude), c.height]; }); }""")
    tl, tr, br, bl = geo
    north_m = ((tl[0] + tr[0] + br[0] + bl[0]) / 4 - (SITE[0] - 25 / 111320)) * 111320
    print(f"      far end: centre {north_m:.1f} m north, top {tl[2]:.1f} m, bottom {bl[2]:.1f} m")
    check(tl[2] > bl[2] and tr[2] > br[2] and 35 < north_m < 46, "the cone points north, tilted down, 45 m long")
    pg.screenshot(path=f"{out}/cam3d-1-cone.png")

    # Click the camera: the live window, and its picture on the far end of the cone.
    prims0 = pg.evaluate("window.__globe.scene.primitives.length")
    xy = pg.evaluate("""() => { const v = window.__globe, e = v.entities.values.find(x => x.box && x.camera);
        const c = Cesium.SceneTransforms.worldToWindowCoordinates(v.scene, e.position.getValue(v.clock.currentTime)); return [c.x, c.y]; }""")
    box = pg.locator("#globe canvas").bounding_box()
    pg.mouse.click(box["x"] + xy[0], box["y"] + xy[1])
    pg.wait_for_selector("#cam-panel:not([hidden]) [data-cam-view]", timeout=8000)
    pg.wait_for_function(f"window.__globe.scene.primitives.length > {prims0}", timeout=10000)
    check(True, "clicking the camera opens it live with View from camera / Calibrate, and puts its picture on the cone")
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{out}/cam3d-2-picture.png")

    # View from camera: the 3D view at the camera's pose, the picture over it.
    before = pg.evaluate(VIEW)
    pg.click("[data-cam-view]")
    pg.wait_for_selector("#cam-view:not([hidden])", timeout=5000)
    pg.wait_for_function("document.getElementById('cam-view-img').naturalWidth > 0", timeout=10000)
    v = pg.evaluate(VIEW)
    check(abs(v["h"] - 6) < 0.3 and min(abs(v["heading"]), abs(v["heading"] - 360)) < 0.5 and abs(v["pitch"] + 12) < 0.5 and abs(v["fov"] - 70) < 0.5,
          f"View from camera: 6 m up, facing north, 12° down, 70° wide ({ {k: round(x, 2) for k, x in v.items()} })")
    img = pg.eval_on_selector("#cam-view-img", "e => { const r = e.getBoundingClientRect(); return [r.width, r.height, e.naturalWidth / e.naturalHeight]; }")
    cw = pg.eval_on_selector("#cam-view", "e => e.getBoundingClientRect().width")
    check(abs(img[0] - cw) < 2 and abs(img[0] / img[1] - img[2]) < 0.02, f"the picture spans the view's width at its own shape {img}")
    pg.screenshot(path=f"{out}/cam3d-3-view.png")
    ms = alarm_ms(pg)
    check(ms < 1000, f"alarm on screen in {ms} ms while looking through the camera")
    pg.click("#cam-view-exit")
    pg.wait_for_selector("#cam-view", state="hidden", timeout=5000)
    after = pg.evaluate(VIEW)
    check(abs(after["h"] - before["h"]) < 1 and abs(after["fov"] - before["fov"]) < 0.1, "Exit view puts the previous view back")

    # Calibrate: drag, keys, a typed height, Save.
    pg.wait_for_selector("#cam-panel:not([hidden]) [data-cam-calibrate]", timeout=5000)
    pg.click("[data-cam-calibrate]")
    pg.wait_for_selector("#cam-cal:not([hidden]) [data-cal-save]", timeout=5000)
    d = pg.locator("#cam-view-drag").bounding_box()
    sx, sy = d["x"] + d["width"] / 2, d["y"] + d["height"] / 2
    pg.mouse.move(sx, sy); pg.mouse.down()
    for i in range(1, 11):
        pg.mouse.move(sx - 6 * i, sy + 3 * i)                           # drag left 60 px and down 30 px
    pg.mouse.up()
    for _ in range(4):
        pg.keyboard.press("ArrowDown")                                   # +0.25° tilt each
    pg.fill("[data-cal=height_m]", "6.5"); pg.press("[data-cal=height_m]", "Enter")
    pose = pg.evaluate(VIEW)
    print(f"      after drag/keys: heading {pose['heading']:.2f}, pitch {pose['pitch']:.2f}, height {pose['h']:.2f}")
    deg_px = 70 / pg.eval_on_selector("#cam-view", "e => e.getBoundingClientRect().width")
    want_heading, want_tilt = 60 * deg_px, 12 - 30 * deg_px + 1.0
    check(abs(pose["heading"] - want_heading) < 0.2 and abs(-pose["pitch"] - want_tilt) < 0.2 and abs(pose["h"] - 6.5) < 0.05,
          f"drag turns / tilts by the picture's own scale, arrows step 0.25°, height typed (heading {pose['heading']:.2f} ≈ {want_heading:.2f}, tilt {-pose['pitch']:.2f} ≈ {want_tilt:.2f})")
    pg.screenshot(path=f"{out}/cam3d-4-calibrate.png")
    pg.click("[data-cal-save]")
    pg.wait_for_function("document.getElementById('cam-cal-say')?.textContent.includes('Saved')", timeout=8000)
    saved = pg.evaluate(API, ["/api/sites", "GET", None])[1][0]["camera_places"][dev]
    print("      saved:", json.dumps({k: saved[k] for k in ("heading", "tilt", "height_m", "fov", "roll")}))
    check(abs(saved["heading"] - want_heading) < 0.3 and abs(saved["tilt"] - want_tilt) < 0.3 and saved["height_m"] == 6.5,
          "Save stores the calibrated pose")
    pg.click("#cam-view-exit")
    pg.wait_for_selector("#cam-view", state="hidden", timeout=5000)
    body2 = pg.evaluate(BODY)
    check(abs(body2["h"] - 6.5) < 0.3, f"the camera body moved to the calibrated height ({body2['h']:.2f} m)")

    # Moving the camera on the 2D map keeps the calibration.
    st, site = pg.evaluate(API, [f"/api/sites/1/cameras/{dev}", "PUT", {"lat": saved["lat"], "lng": saved["lng"], "heading": 10, "fov": saved["fov"], "range_m": 45}])
    kept = site["camera_places"][dev]
    check(kept["tilt"] == saved["tilt"] and kept["height_m"] == 6.5, "aiming it again on the map keeps height and tilt")
    ms = alarm_ms(pg)
    check(ms < 1000, f"alarm on screen in {ms} ms after all of it")
    check(not errors, f"no page errors {errors}")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

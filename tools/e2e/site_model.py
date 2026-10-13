"""Site 3D models in a real browser: upload a (synthetic) DJI Terra OBJ export through the 3D view's
"Site model" panel, check where it lands and which way it faces by measuring the model in the 3D scene,
turn / raise / drag it, hide it, delete it, and time alarms while it's on screen.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.site_model [screenshot_dir]

The sample (tools/sample_model.py) is a 20 m (east-west) x 12 m x 6 m box standing on a 40 x 40 m ground
patch (only its top face is in the file, so the model's floor is ground level). Points are measured off the
editor's guide line (centre -> orange dot), which the height sampling would otherwise hit.
Headless Chromium only has software WebGL, so the 3D view is forced on with its watchdog off
(localStorage twg-3d-force=2): latency here is the worst case.
"""
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site
from tools.sample_model import DEV_SITE, make

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp")
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


# Heights of whatever is drawn at points east/north of the site, measured in the scene (ground = 0).
SAMPLE = """async (pts) => {
  const v = window.__globe, C = window.Cesium;
  const carto = pts.map(([e, n]) => C.Cartographic.fromDegrees(%f + e / (111320 * Math.cos(%f * Math.PI / 180)), %f + n / 111320));
  v.scene.requestRenderMode = false;                 // most-detailed sampling advances only while frames render
  try { var got = await Promise.race([v.scene.sampleHeightMostDetailed(carto), new Promise((_, no) => setTimeout(() => no(new Error("sampling timed out")), 30000))]); }
  finally { v.scene.requestRenderMode = true; }
  return got.map((c) => (c && Number.isFinite(c.height) ? Math.round(c.height * 100) / 100 : null));
}""" % (DEV_SITE[1], DEV_SITE[0], DEV_SITE[0])
TILESET = """() => { const p = window.__globe.scene.primitives; for (let i = 0; i < p.length; i++) { const x = p.get(i);
  if (x instanceof window.Cesium.Cesium3DTileset) return { show: x.show, loaded: x.statistics.numberOfLoadedTilesTotal, ready: x.tilesLoaded }; } return null; }"""


def heights(pg, pts):
    return pg.evaluate(SAMPLE, pts)


def model(pg, site_id):
    return pg.evaluate(f"async () => (await fetch('/api/sites/{site_id}/model')).json()")


def alarm_ms(pg):
    t = time.time()
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = round((time.time() - t) * 1000)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    return ms


with sync_playwright() as p:
    b = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = b.new_context(viewport={"width": 1600, "height": 950})
    ctx.add_init_script(COUNT_TONES)
    ctx.add_init_script("localStorage.setItem('twg-3d-force', '2')")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg); pg.wait_for_timeout(2000)
    site_id = pg.evaluate("async () => (await (await fetch('/api/sites')).json())[0].id")
    zip_path = out / "TWG_Office_sample.zip"
    zip_path.write_bytes(make())

    if pg.locator("#critical-dialog[open]").count():
        pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    pg.click(".site-item"); pg.wait_for_timeout(800)
    pg.click("[data-globe]")
    pg.wait_for_function("document.querySelector('#globe canvas') && document.getElementById('globe-msg').hidden", timeout=40000)
    pg.wait_for_timeout(4000)
    check(pg.is_visible("#globe-model"), "admins get a Site model button in the 3D view")
    check(not pg.is_visible("#globe-model-show"), "no Model on/off button while no site has a model")
    print(f"  alarm with the 3D view open, no model: {alarm_ms(pg)} ms")

    pg.click("#globe-model")
    pg.wait_for_selector("#model-panel [data-drop]")
    pg.screenshot(path=str(out / "model-upload-panel.png"))
    t = time.time()
    pg.set_input_files("#model-panel [data-file]", str(zip_path))
    pg.wait_for_selector("#model-panel [data-mp=edit]", timeout=90000)
    check(True, f"uploaded and converted in {time.time() - t:.1f} s")
    check("Terra metadata.xml (EPSG:32618)" in pg.inner_text("#model-panel"), "panel says it was positioned from Terra's metadata.xml")
    pg.wait_for_function(f"({TILESET})()?.loaded >= 1", timeout=30000)
    check(pg.is_visible("#globe-model-show"), "Model on/off button appears")
    pg.click("#model-panel [data-mp=fly]"); pg.wait_for_timeout(2500)
    pg.screenshot(path=str(out / "model-placed.png"))

    # Where it is and which way it faces: the box is 20 m along east-west, 12 m north-south, 6 m tall.
    h = heights(pg, [(0, 0), (8, -3), (-3, 8), (15, 15)])
    print("  heights at centre, 8 m E, 8 m N, ground patch corner:", h)
    check(h[0] is not None and abs(h[0] - 6) < 0.3, "roof 6 m up, at the site's position (UTM from metadata.xml), floor on the ground")
    check(h[1] is not None and abs(h[1] - 6) < 0.3 and h[2] is not None and abs(h[2]) < 0.3, "long side runs east-west (axes right)")
    check(h[3] is not None and abs(h[3]) < 0.3, "ground patch at ground level")

    print(f"  alarm with the model on screen: {alarm_ms(pg)} ms")

    # Turn 90°: the long side now runs north-south.
    pg.click("#model-panel [data-mp=edit]")
    pg.fill("#model-panel [data-mpn=heading]", "90"); pg.press("#model-panel [data-mpn=heading]", "Tab")
    pg.wait_for_function("document.querySelector('#model-panel [data-save]').textContent.includes('Saved')", timeout=5000)
    h = heights(pg, [(8, -3), (-3, 8)])
    check(h[0] is not None and abs(h[0]) < 0.3 and abs((h[1] or 0) - 6) < 0.3, f"turned 90°: long side north-south {h}")
    check(model(pg, site_id)["ready"]["placement"]["heading"] == 90, "turn saved")

    # Raise it 0.5 m with the ▲ nudge twice.
    for _ in range(2):
        pg.click("#model-panel [data-nudge=height][data-by='0.25']"); pg.wait_for_timeout(200)
    pg.wait_for_timeout(800)
    h = heights(pg, [(0, 0)])
    check(abs((h[0] or 0) - 6.5) < 0.3, f"raised 0.5 m: roof at {h[0]}")
    check(model(pg, site_id)["ready"]["placement"]["height"] == 0.5, "height saved")

    # Drag the white centre dot 25 m east.
    pos = pg.evaluate("""() => { const v = window.__globe, C = window.Cesium;
      const w = (lng, lat) => C.SceneTransforms.worldToWindowCoordinates(v.scene, C.Cartesian3.fromDegrees(lng, lat, 0.5));
      const r = v.canvas.getBoundingClientRect(), a = w(%f, %f), b = w(%f + 25 / (111320 * Math.cos(%f * Math.PI / 180)), %f);
      return [r.left + a.x, r.top + a.y, r.left + b.x, r.top + b.y]; }""" % (DEV_SITE[1], DEV_SITE[0], DEV_SITE[1], DEV_SITE[0], DEV_SITE[0]))
    pg.mouse.move(pos[0], pos[1]); pg.mouse.down()
    for i in range(1, 11):
        pg.mouse.move(pos[0] + (pos[2] - pos[0]) * i / 10, pos[1] + (pos[3] - pos[1]) * i / 10); pg.wait_for_timeout(60)
    pg.mouse.up(); pg.wait_for_timeout(1200)
    pl = model(pg, site_id)["ready"]["placement"]
    moved = (pl["lng"] - DEV_SITE[1]) * 111320 * math.cos(math.radians(DEV_SITE[0]))
    check(15 < moved < 35 and abs(pl["lat"] - DEV_SITE[0]) * 111320 < 5, f"dragged the model {moved:.1f} m east (saved)")
    h = heights(pg, [(moved - 3, 3), (-3, 3)])
    check(abs((h[0] or 0) - 6.5) < 0.4 and abs(h[1] or 0) < 0.3, f"model is drawn where it was dragged {h}")
    pg.screenshot(path=str(out / "model-moved.png"))

    # Reset puts it back where the file said.
    pg.click("#model-panel [data-mp=reset]"); pg.wait_for_timeout(1200)
    pl = model(pg, site_id)["ready"]["placement"]
    check(abs(pl["lat"] - DEV_SITE[0]) < 1e-5 and pl["height"] == 0 and abs(pl["heading"] - 358.79) < 0.05, "Reset position goes back to metadata.xml")
    pg.click("#model-panel [data-mp=done]")

    # Another screen (operator view) gets the move live through the event stream.
    pg2 = ctx.new_page()
    pg2.goto(f"{PORTAL}/"); pg2.wait_for_timeout(2000)
    got = pg2.evaluate(f"async () => (await (await fetch('/api/sites')).json()).find((s) => s.id === {site_id}).model.ready.placement.height")
    check(got == 0, "site list carries the model for other screens")
    pg2.close()

    pg.click("#globe-model-show"); pg.wait_for_timeout(500)
    ts, label = pg.evaluate(TILESET), pg.inner_text("#globe-model-show")
    check(ts["show"] is False and "off" in label.lower(), f"Model: off hides it ({ts}, {label!r})")
    pg.click("#globe-model-show")

    lat = [alarm_ms(pg) for _ in range(3)]
    print(f"  alarms with the model on screen after editing: {lat} ms")
    check(max(lat) < 1500, "alarms still pop up promptly with the model on screen (software WebGL, worst case)")

    pg.once("dialog", lambda d: d.accept())
    pg.click("#model-panel [data-mp=delete]"); pg.wait_for_timeout(1500)
    check(pg.evaluate(TILESET) is None, "Delete model removes it from the 3D view")
    check(model(pg, site_id) == {"ready": None, "job": None}, "and from the server")
    check(not errors, f"no page errors {errors[:3]}")
    b.close()

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

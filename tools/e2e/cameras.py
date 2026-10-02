"""Place a camera on the 3D view and watch it live; an alarm on it turns it red.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.cameras /tmp
Headless Chromium only has software WebGL, so this sets twg-3d-force=2 (no graphics check, no
frame watchdog) to drive the UI; tools/e2e/globe.py covers the safety checks.
"""
import sys
import time
from playwright.sync_api import sync_playwright
from tools.e2e.common import COUNT_TONES, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"

with sync_playwright() as p:
    b = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg); pg.wait_for_timeout(2500)
    pg.evaluate("localStorage.setItem('twg-3d-force', '2')")
    pg.click(".site-item"); pg.click("[data-globe]")
    pg.wait_for_function("!!window.__globe && document.getElementById('globe-msg').hidden", timeout=30000)
    pg.wait_for_timeout(6000)
    print("HUD:", pg.inner_text("#hud-site").replace("\n", " | "), "||", pg.inner_text("#hud-counts"), "||", pg.inner_text("#hud-clock"))
    print("coords readout:", pg.inner_text("#hud-coords").replace("\n", " | "))

    pg.click("#globe-cams")
    pg.wait_for_selector("#cam-place li[data-dev]", timeout=10000)
    names = pg.eval_on_selector_all("#cam-place li[data-dev] .nm", "els => els.map(e => e.textContent.trim())")
    print("cameras listed:", names)
    box = pg.locator("#globe canvas").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.locator("#cam-place li[data-dev]").first.locator("[data-place]").click()
    print("hint:", pg.inner_text("#place-hint"))
    pg.mouse.click(cx - 60, cy + 40); pg.wait_for_timeout(500)
    print("hint:", pg.inner_text("#place-hint"))
    pg.mouse.move(cx + 80, cy - 60); pg.wait_for_timeout(300)
    pg.mouse.click(cx + 80, cy - 60)
    pg.wait_for_selector("#cam-place .placed", timeout=10000)
    places = pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].camera_places)")
    print("saved:", places)
    # Second camera as a 360° dome (one click)
    li2 = pg.locator("#cam-place li[data-dev]").nth(1)
    li2.locator("select").select_option("360"); li2.locator("[data-place]").click()
    pg.mouse.click(cx + 120, cy + 90)
    pg.wait_for_function("document.querySelectorAll('#cam-place .placed').length === 2", timeout=10000)
    pg.click("#globe-cams")      # close the list
    pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{out}/cams_placed.png")

    # Click the first camera -> live window
    dev = list(places)[0]
    pos = pg.evaluate("""(dev) => { const v = window.__globe; const e = v.entities.values.find(x => x.camera && x.camera.deviceId === dev && x.point);
        const c = Cesium.SceneTransforms.worldToWindowCoordinates(v.scene, e.position.getValue(v.clock.currentTime)); return c && [c.x, c.y]; }""", dev)
    pg.mouse.click(box["x"] + pos[0], box["y"] + pos[1])
    pg.wait_for_selector("#cam-panel:not([hidden]) video", timeout=10000)
    t = time.time()
    pg.wait_for_function("(() => { const v = document.querySelector('#cam-panel video'); const s = document.querySelector('#cam-panel .live-still');"
                         " return (v && v.readyState >= 2) || (s && !s.hidden && s.complete && s.naturalWidth > 0); })()", timeout=20000)
    print(f"live picture in the camera window after {round(time.time() - t, 1)} s:", pg.inner_text("#cam-panel .cam-head").replace("\n", " "))

    # An alarm from that camera turns its cone and tag red
    n = pg.locator(".feed-item.p2").count(); t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
    print(f"alarm on screen in {round((time.time() - t) * 1000)} ms (3D open, live video playing)")
    pg.wait_for_timeout(1000)
    tags = pg.evaluate("window.__globe.entities.values.filter(e => e.camera && e.label).map(e => e.label.text.getValue())")
    print("camera tags after the alarm:", tags)
    pg.screenshot(path=f"{out}/cams_alarm.png")
    print("page errors:", errors or "none")
    b.close()

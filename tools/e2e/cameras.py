"""Place a camera on the 3D view and watch it live; an alarm on it turns it red.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.cameras /tmp
Headless Chromium only has software WebGL, so this sets twg-3d-force=2 (no graphics check, no
frame watchdog) to drive the UI; tools/e2e/globe.py covers the safety checks.
"""
import sys
import time
from playwright.sync_api import sync_playwright
from tools.e2e.common import COUNT_TONES, inject, login_and_add_site, stub_aerial_tiles

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"

with sync_playwright() as p:
    b = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    stub_aerial_tiles(pg)
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
    saved = lambda: pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].camera_places)")
    first = lambda: list(saved().values())[0]

    def handle_xy(kind):
        return pg.evaluate("""(k) => { const v = window.__globe; const e = v.entities.values.find(x => x.editHandle === k);
            const c = Cesium.SceneTransforms.worldToWindowCoordinates(v.scene, e.position.getValue(v.clock.currentTime)); return [c.x, c.y]; }""", kind)

    def drag(kind, dx, dy):
        x, y = handle_xy(kind)
        pg.mouse.move(box["x"] + x, box["y"] + y); pg.mouse.down()
        for i in range(1, 9): pg.mouse.move(box["x"] + x + dx * i / 8, box["y"] + y + dy * i / 8)
        pg.mouse.up()
        pg.wait_for_function("document.getElementById('ed-save')?.textContent.includes('Saved')", timeout=10000)
        pg.wait_for_timeout(300)

    # 1. place: click the spot, aim with the mouse (direction AND distance follow it), click
    pg.locator("#cam-place li[data-dev]").first.locator("[data-edit]").click()
    print("hint:", pg.inner_text("#cam-editor .ed-hint"))
    pg.mouse.click(cx - 60, cy + 40); pg.wait_for_timeout(300)
    print("hint:", pg.inner_text("#cam-editor .ed-hint"))
    for i in range(1, 6): pg.mouse.move(cx - 60 + 30 * i, cy + 40 - 25 * i); pg.wait_for_timeout(40)
    pg.mouse.click(cx + 90, cy - 85)
    pg.wait_for_function("document.getElementById('ed-save')?.textContent.includes('Saved')", timeout=10000)
    a = first(); print("placed:", a)
    near = dict(a)
    pg.wait_for_timeout(300)
    # 2. drag the tip further out: longer distance
    drag("aim", 40, -40); b2 = first(); print(f"tip dragged: distance {near['range_m']} -> {b2['range_m']} m, aim {near['heading']} -> {b2['heading']}")
    # 3. drag a side handle sideways: any width, not a preset
    drag("right", 60, 50); c2 = first(); print(f"side dragged: width {b2['fov']} -> {c2['fov']}°")
    # 4. drag the centre: moves the camera
    drag("move", -30, 20); d2 = first(); print(f"centre dragged: moved {round(abs(d2['lat'] - c2['lat']) * 111320 + abs(d2['lng'] - c2['lng']) * 85000, 1)} m")
    # 5. type an exact width
    pg.fill("[data-ed=fov]", "137"); pg.press("[data-ed=fov]", "Enter")
    pg.wait_for_function("document.getElementById('ed-save')?.textContent.includes('Saved')", timeout=10000)
    pg.wait_for_timeout(500); print("typed width -> saved fov:", first()["fov"])
    pg.click("[data-ed-done]")
    # 6. second camera as a 360 dome
    pg.locator("#cam-place li[data-dev]").nth(1).locator("[data-edit]").click()
    pg.mouse.click(cx + 120, cy + 90); pg.mouse.move(cx + 150, cy + 110); pg.mouse.click(cx + 150, cy + 110)
    pg.wait_for_function("document.getElementById('ed-save')?.textContent.includes('Saved')", timeout=10000)
    pg.click("[data-ed-dome]")
    pg.wait_for_timeout(1500)
    print("dome:", [round(v["fov"]) for v in saved().values()])
    pg.screenshot(path=f"{out}/cams_edit.png")
    pg.click("[data-ed-done]")
    pg.click("#globe-cams")      # close the list
    pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{out}/cams_placed.png")
    places = saved()

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

"""Top-down view: place and edit cameras on the flat aerial map, live window, an alarm turns a camera red,
and alarm latency while the view is open (also mid-drag).

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.topdown /tmp
No WebGL involved, so unlike tools/e2e/cameras.py this needs no test switches. Aerial tiles are stand-ins.
"""
import sys
import time
from playwright.sync_api import sync_playwright
from tools.e2e.common import COUNT_TONES, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
SAVED = "document.getElementById('ed-save')?.textContent.includes('Saved')"

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg); pg.wait_for_timeout(2500)
    pg.click(".site-item")          # clicking a site goes straight to its top-down view
    pg.wait_for_function("!!window.__plan && document.querySelector('#map.plan-sat')", timeout=10000)
    pg.wait_for_function("[...document.querySelectorAll('.leaflet-tile-pane img')].every(e => e.complete)", timeout=20000)
    sat = pg.eval_on_selector_all(".leaflet-tile-pane img", "els => els.filter(e => e.src.includes('/tiles/sat/') && e.complete && e.naturalWidth).length")
    street = pg.eval_on_selector_all(".leaflet-tile-pane img", "els => els.filter(e => !e.src.includes('/tiles/sat/')).length")
    print(f"top-down open: bar button {pg.inner_text('#globe-close')!r}; aerial tiles loaded {sat}, street tiles {street}")
    pg.screenshot(path=f"{out}/topdown_open.png")

    pg.click("#globe-cams")
    pg.wait_for_selector("#cam-place li[data-dev]", timeout=10000)
    print("cameras listed:", pg.eval_on_selector_all("#cam-place li[data-dev] .nm", "els => els.map(e => e.textContent.trim())"))
    box = pg.locator("#map").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    saved = lambda: pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].camera_places)")
    first = lambda: list(saved().values())[0]

    def drag(kind, dx, dy):
        h = pg.locator(f".plan-handle.{kind}").bounding_box()
        x, y = h["x"] + h["width"] / 2, h["y"] + h["height"] / 2
        pg.mouse.move(x, y); pg.mouse.down()
        for i in range(1, 9): pg.mouse.move(x + dx * i / 8, y + dy * i / 8)
        pg.mouse.up()
        pg.wait_for_function(SAVED, timeout=10000)
        pg.wait_for_timeout(300)

    # 1. place: click the spot, aim with the mouse (direction and distance follow it), click
    pg.locator("#cam-place li[data-dev]").first.locator("[data-edit]").click()
    print("hint:", pg.inner_text("#cam-editor .ed-hint"))
    pg.mouse.click(cx - 60, cy + 40); pg.wait_for_timeout(200)
    print("hint:", pg.inner_text("#cam-editor .ed-hint"), "| cursor:", pg.evaluate("getComputedStyle(document.getElementById('map')).cursor"))
    for i in range(1, 6): pg.mouse.move(cx - 60 + 30 * i, cy + 40 - 25 * i); pg.wait_for_timeout(40)
    pg.mouse.click(cx + 90, cy - 85)
    pg.wait_for_function(SAVED, timeout=10000)
    a = first(); print("placed:", a)
    # 2. tip further out: longer distance, new aim
    drag("aim", 40, -40); b2 = first(); print(f"tip dragged: distance {a['range_m']} -> {b2['range_m']} m, aim {a['heading']} -> {b2['heading']}")
    # 3. a side handle sideways: any width
    drag("right", 60, 50); c2 = first(); print(f"side dragged: width {b2['fov']} -> {c2['fov']}°")
    # 4. the centre: moves the camera
    drag("move", -30, 20); d2 = first(); print(f"centre dragged: moved {round(abs(d2['lat'] - c2['lat']) * 111320 + abs(d2['lng'] - c2['lng']) * 85000, 1)} m")
    # 5. an exact width typed in
    pg.fill("[data-ed=fov]", "137"); pg.press("[data-ed=fov]", "Enter")
    pg.wait_for_function(SAVED, timeout=10000)
    pg.wait_for_timeout(500); print("typed width -> saved fov:", first()["fov"])
    pg.click("[data-ed-done]")
    # 6. a second camera as a 360° dome
    pg.locator("#cam-place li[data-dev]").nth(1).locator("[data-edit]").click()
    pg.mouse.click(cx + 120, cy + 90); pg.mouse.move(cx + 150, cy + 110); pg.mouse.click(cx + 150, cy + 110)
    pg.wait_for_function(SAVED, timeout=10000)
    pg.click("[data-ed-dome]")
    pg.wait_for_timeout(1000)
    print("widths saved:", [round(v["fov"]) for v in saved().values()])
    pg.screenshot(path=f"{out}/topdown_edit.png")
    pg.click("[data-ed-done]")
    pg.click("#globe-cams")      # close the list
    pg.wait_for_timeout(800)
    print("cameras drawn:", pg.locator(".plan-cam").count(), "cones:", pg.locator(".leaflet-planCones-pane path").count())
    pg.screenshot(path=f"{out}/topdown_placed.png")

    # Re-center fits the view to the placed cameras
    pg.mouse.move(cx, cy); pg.mouse.wheel(0, 1500); pg.wait_for_timeout(800)
    pg.click("#globe-recenter"); pg.wait_for_timeout(1500)
    inside = pg.eval_on_selector_all(".plan-cam", "els => els.map(e => { const r = e.getBoundingClientRect(), m = document.getElementById('map').getBoundingClientRect();"
                                     " return r.left >= m.left && r.right <= m.right && r.top >= m.top && r.bottom <= m.bottom; })")
    print("after Re-center, cameras on screen:", inside)

    # Mouse over a camera: a live preview beside it, gone when the mouse leaves
    dot = pg.locator(".plan-cam i").first.bounding_box()
    dx, dy = dot["x"] + dot["width"] / 2, dot["y"] + dot["height"] / 2
    pg.mouse.move(dx - 300, dy + 200); pg.wait_for_timeout(300)
    for i in range(1, 11): pg.mouse.move(dx - 300 + 60 * i, dy + 200 - 40 * i)   # a quick sweep across the cameras
    pg.wait_for_timeout(100)
    print("quick sweep across the map opened a preview:", pg.evaluate("!document.getElementById('cam-hover').hidden"))
    pg.mouse.move(dx - 300, dy + 200); pg.wait_for_timeout(400)
    t = time.time(); pg.mouse.move(dx, dy)
    pg.wait_for_selector("#cam-hover:not([hidden]) .live-still", state="attached", timeout=5000)
    pg.wait_for_function("""(() => { const v = document.querySelector('#cam-hover video'), s = document.querySelector('#cam-hover .live-still');
        return (v && v.readyState >= 2) || (s && !s.hidden && s.complete && s.naturalWidth > 0); })()""", timeout=10000, polling=20)
    shown = round((time.time() - t) * 1000)
    pg.wait_for_function("(() => { const v = document.querySelector('#cam-hover video'); return v && v.readyState >= 2; })()", timeout=10000, polling=20)
    print(f"hover preview: picture after {shown} ms, live video after {round((time.time() - t) * 1000)} ms:", pg.inner_text("#cam-hover .cam-head"),
          "| inside the map:", pg.evaluate("""(() => { const r = document.getElementById('cam-hover').getBoundingClientRect(), m = document.getElementById('map').getBoundingClientRect();
            return r.left >= m.left && r.right <= m.right && r.top >= m.top && r.bottom <= m.bottom; })()"""))
    pg.screenshot(path=f"{out}/topdown_hover.png")
    n = pg.locator(".feed-item").count(); t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {n}", timeout=10000, polling=10)
    print(f"alarm on screen with the hover preview playing: {round((time.time() - t) * 1000)} ms")
    pg.mouse.move(dx - 300, dy + 200); pg.wait_for_timeout(500)
    print("mouse left: preview hidden", pg.evaluate("document.getElementById('cam-hover').hidden"), "| video elements left", pg.locator("#cam-hover video").count())

    # Click the first camera -> live window
    pg.locator(".plan-cam i").first.click()
    pg.wait_for_selector("#cam-panel:not([hidden]) video", timeout=10000)
    t = time.time()
    pg.wait_for_function("(() => { const v = document.querySelector('#cam-panel video'); const s = document.querySelector('#cam-panel .live-still');"
                         " return (v && v.readyState >= 2) || (s && !s.hidden && s.complete && s.naturalWidth > 0); })()", timeout=20000)
    print(f"live picture in the camera window after {round(time.time() - t, 1)} s:", pg.inner_text("#cam-panel .cam-head").replace("\n", " "))

    # Alarms with the view open and live video playing: an alarm from a placed camera turns it red
    lat = []
    for _ in range(3):
        n = pg.locator(".feed-item").count(); t = time.time(); inject("line")
        pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {n}", timeout=10000, polling=10)
        lat.append(round((time.time() - t) * 1000)); pg.wait_for_timeout(700)
    print(f"alarm on screen (top-down open, live video): {lat} ms")
    pg.wait_for_timeout(500)
    print("red cameras:", pg.eval_on_selector_all(".plan-cam--alarm span", "els => els.map(e => e.textContent)"))
    # ...and while an operator is panning the map
    print("live window with an alarm: header above the video:", pg.evaluate("""(() => { const h = document.querySelector('#cam-panel .cam-head').getBoundingClientRect(),
        v = document.querySelector('#cam-panel .cam-live').getBoundingClientRect(); return h.bottom <= v.top + 1 && h.width > 300; })()"""))
    pg.click("#cam-panel [data-cam-close]")
    lat = []
    for k in range(3):
        n = pg.locator(".feed-item").count()
        pg.mouse.move(cx, cy); pg.mouse.down()
        t = time.time(); inject("line")
        i = 0
        while pg.locator(".feed-item").count() <= n and time.time() - t < 10:
            i += 1; pg.mouse.move(cx + 80 * ((i % 10) / 10), cy + 40 * ((i % 7) / 7))
        lat.append(round((time.time() - t) * 1000))
        pg.mouse.up(); pg.wait_for_timeout(700)
    print(f"alarm on screen while panning the top-down map: {lat} ms (includes the test's own mouse-move steps)")
    pg.screenshot(path=f"{out}/topdown_alarm.png")

    # Open the alarm: its details show the site top-down, centred on the camera, cone red and flashing
    t = time.time()
    pg.locator(".feed-item").first.click()
    pg.wait_for_selector(".drawer-where:not([hidden]) .plan-cone--alarm", timeout=10000)
    print(f"alarm details: top-down map with the red cone after {round((time.time() - t) * 1000)} ms;",
          "note:", pg.inner_text(".drawer-where .where-note"),
          "| flashing:", pg.evaluate("getComputedStyle(document.querySelector('.drawer-where .plan-cone--alarm')).animationName"),
          "| cone on screen:", pg.evaluate("""(() => { const r = document.querySelector('.drawer-where .plan-cone--alarm').getBoundingClientRect(),
              m = document.querySelector('.where-mount').getBoundingClientRect(); return r.left >= m.left - 1 && r.right <= m.right + 1 && r.top >= m.top - 1 && r.bottom <= m.bottom + 1; })()"""))
    print("main top-down jumped to the camera:", pg.evaluate("""(() => { const r = document.querySelector('#map .plan-cam--alarm')?.getBoundingClientRect(),
              m = document.getElementById('map').getBoundingClientRect(); return !!r && r.left > m.left && r.right < m.right && r.top > m.top && r.bottom < m.bottom; })()"""))
    pg.screenshot(path=f"{out}/topdown_drawer.png")
    n = pg.locator(".feed-item").count(); t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {n}", timeout=10000, polling=10)
    print(f"alarm into the feed with the details + their map open: {round((time.time() - t) * 1000)} ms")
    pg.click("#drawer-close"); pg.wait_for_timeout(300)
    print("details closed, their map removed:", pg.locator(".drawer-where").count() == 0 or pg.locator(".where-mount .leaflet-container").count() == 0)

    # Exit: the street map comes back, cameras go away
    pg.click("#globe-close"); pg.wait_for_timeout(1200)
    print("after exit: plan-sat", pg.evaluate("!!document.querySelector('#map.plan-sat')"), "| cameras drawn", pg.locator(".plan-cam").count(),
          "| bar hidden", pg.evaluate("document.getElementById('globe-bar').hidden"),
          "| street tiles", pg.eval_on_selector_all(".leaflet-tile-pane img", "els => els.filter(e => !e.src.includes('/tiles/sat/')).length"))
    # Clicking the site's pin on the street map: straight into top-down again
    pg.locator(".pin-icon .pin-head").first.click(); pg.wait_for_function("!!window.__plan", timeout=5000)
    pg.wait_for_timeout(1200)
    print("pin clicked -> top-down:", pg.evaluate("!!document.querySelector('#map.plan-sat')"), "| cameras drawn:", pg.locator(".plan-cam").count())
    # ✕ on the site panel (back to all sites) leaves the top-down view
    pg.click("#close-detail"); pg.wait_for_timeout(800)
    print("site closed -> overview:", pg.evaluate("!window.__plan && !document.querySelector('#map.plan-sat')"))
    # Clicking an alarm in the feed: details open, and behind them the map is top-down at the alarm's camera
    n = pg.locator(".feed-item").count(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {n}", timeout=10000)
    pg.locator(".feed-item").first.click()
    pg.wait_for_function("!!window.__plan && !!document.querySelector('#map .plan-cam--alarm')", timeout=10000)
    print("alarm clicked from the overview -> main map top-down with the camera red:", pg.locator("#map .plan-cam--alarm").count() == 1)
    pg.click("#drawer-close")
    print("page errors:", errors or "none")
    b.close()

"""3D site view: opens, draws pins, picks a site, closes, and never gets in the way of alarms.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.globe /tmp
(polling at 60 s proves alarms arrive by push; needs internet for the imagery)

Headless Chromium here only has software WebGL (SwiftShader), the worst case. So this checks:
 1. alarm latency with the 3D view closed (baseline)
 2. software graphics: "3D view" refuses with a message, 2D map stays
 3. forced on anyway (localStorage twg-3d-force=1): it opens, pins work, and either the frames
    stay fast or the watchdog closes the view; alarm latency is measured while it's open
 4. the 3D library fails to load: message, 2D map back, alarms still arrive
"""
import sys
import time
from playwright.sync_api import sync_playwright
from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
LONGTASKS = """window.__lt = []; try { new PerformanceObserver((l) => l.getEntries().forEach((e) => window.__lt.push(Math.round(e.duration))))
    .observe({ type: 'longtask', buffered: true }); } catch (e) {}"""


def dismiss_popup(pg):
    if pg.locator("#critical-dialog[open]").count():
        pg.keyboard.press("Escape"); pg.wait_for_timeout(300)


def latency(pg, label, kinds=("line", "dock", "panic")):
    res = []
    for kind in kinds:
        sel = {"line": ".feed-item.p2", "dock": ".feed-item.p3", "panic": "#critical-dialog[open]"}[kind]
        n = pg.locator(sel).count() if kind != "panic" else 0
        osc = pg.evaluate("window.__osc")
        t = time.time(); inject(kind)
        if kind == "panic": pg.wait_for_selector(sel, timeout=10000)
        else: pg.wait_for_function(f"document.querySelectorAll('{sel}').length > {n}", timeout=10000)
        ms = round((time.time() - t) * 1000)
        pg.wait_for_timeout(400)
        res.append((kind, ms, "sound" if pg.evaluate("window.__osc") > osc else "NO SOUND"))
        dismiss_popup(pg)
    print(f"  NX event -> operator screen (ms), {label}:", res)
    return res


def click_3d(pg):
    dismiss_popup(pg)
    if not pg.locator("#site-detail:not([hidden])").count(): pg.click(".site-item")
    pg.click("[data-globe]")


with sync_playwright() as p:
    b = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    ctx.add_init_script(COUNT_TONES); ctx.add_init_script(LONGTASKS)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg); pg.wait_for_timeout(3000)

    print("1. baseline")
    latency(pg, "3D view closed")

    print("2. software graphics, not forced")
    click_3d(pg)
    pg.wait_for_selector(".toast.error", timeout=10000)
    print("  message:", pg.inner_text(".toast.error")[:110])
    print("  Cesium not even downloaded:", pg.evaluate("!window.Cesium"), "| 2D map shown:", pg.evaluate("document.getElementById('globe').hidden"))

    print("3. forced on")
    pg.evaluate("localStorage.setItem('twg-3d-force', '1')")
    pg.evaluate("window.__lt.length = 0")
    t = time.time(); click_3d(pg)
    pg.wait_for_function("document.querySelector('#globe canvas') && document.getElementById('globe-msg').hidden", timeout=30000)
    print(f"  open (library loaded, first frame) in {round(time.time() - t, 1)} s; pins:", pg.evaluate("window.__globe.entities.values.length"))
    opened_at = time.time()
    lat = []
    for i in range(6):                    # alarms during the fly-in and imagery loading
        if pg.evaluate("!window.__globe"): break
        lat += latency(pg, f"3D open, t+{round(time.time() - opened_at)} s", kinds=("line",))
        pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{out}/globe_site.png")
    open_now = pg.evaluate("!!window.__globe")
    print("  still open:", open_now, "| frame stats:", pg.evaluate("window.__globeStats || null"),
          "| browser long tasks (ms):", pg.evaluate("window.__lt.slice(0, 20)"))
    if not open_now:
        print("  watchdog toast:", pg.inner_text(".toast.warn")[:140] if pg.locator(".toast.warn").count() else "(none)")
    else:
        dismiss_popup(pg)
        if pg.locator("#close-detail").count(): pg.click("#close-detail")
        box = pg.locator("#globe canvas").bounding_box()
        pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2); pg.wait_for_timeout(1500)
        print("  pin click selected the site:", pg.locator("#site-detail:not([hidden]) h2").count() == 1)
        pg.click("[data-imagery=street]"); pg.wait_for_timeout(3000)
        pg.screenshot(path=f"{out}/globe_street.png")
        if pg.evaluate("!!window.__globe"):
            pg.click("#globe-close")
    pg.wait_for_timeout(500)
    print("  closed: WebGL canvas removed, 2D map shown:", pg.evaluate("!document.querySelector('#globe canvas') && document.getElementById('globe').hidden"))
    latency(pg, "after the 3D view closed", kinds=("line",))

    print("4. 3D library fails to load")
    pg2 = ctx.new_page()
    pg2.route("**/vendor/cesium/Cesium.js", lambda r: r.fulfill(status=404, body="nope"))
    pg2.goto(f"{PORTAL}/"); pg2.mouse.click(600, 400); pg2.wait_for_timeout(2500)
    click_3d(pg2)
    pg2.wait_for_selector(".toast.error", timeout=10000)
    print("  message:", pg2.inner_text(".toast.error")[:90])
    print("  2D map shown again:", pg2.evaluate("document.getElementById('globe').hidden && !!document.querySelector('#map .leaflet-tile')"))
    latency(pg2, "after the failed load", kinds=("line",))

    print("page errors:", errors or "none")
    b.close()

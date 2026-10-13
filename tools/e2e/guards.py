"""Guard tracking (Peplink GPS) in a real browser, against the fake InControl2 in tools/fake_nx.py.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.guards [screenshot_dir]

Sites page → Guards & vehicles (the company's own InControl2 account): credentials, Test connection, Find routers, Track (a moving patrol car and a trailer that
last reported yesterday). Map page: live marker that moves with a trail, a stale grey one, the Guards list
and follow; the 3D view draws them. InControl2 failing shows on the marker; alarms stay fast throughout,
including while InControl2 hangs.
"""
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def ic2(mode):
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_ic2/mode/{mode}", method="POST"))


def alarm_ms(pg):
    t = time.time()
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = round((time.time() - t) * 1000)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    return ms


MARKER = "() => { const m = document.querySelector('.guard-icon .guard'); if (!m) return null; const r = m.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y)]; }"

with sync_playwright() as p:
    ic2("reset")
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)

    # ---- Sites page (company admins)
    pg.goto(f"{PORTAL}/sites")
    pg.wait_for_selector("#gt-form")
    pg.fill("#gt-id", "dev-client"); pg.fill("#gt-secret", "dev-secret"); pg.fill("#gt-poll", "15")
    pg.check("#gt-enabled"); pg.click("#gt-form button[type=submit]")
    pg.wait_for_function("document.getElementById('gt-secret').placeholder.startsWith('Saved')", timeout=5000)
    check("must-never" not in pg.content(), "the secret isn't sent back to the page")
    pg.click("#gt-test")
    pg.wait_for_function("document.getElementById('gt-test-result').textContent.includes('Connected')", timeout=10000)
    check("TWG Dev (fake)" in pg.inner_text("#gt-test-result"), "Test connection lists the organization")
    pg.focus("#gt-org"); pg.wait_for_function("document.getElementById('gt-org').options.length > 2", timeout=10000)
    pg.select_option("#gt-org", "blocked")
    pg.wait_for_selector("#gt-devices .alert-error", timeout=10000)
    check("API access blocked" in pg.inner_text("#gt-devices"), "an organization with API access blocked says so")
    pg.select_option("#gt-org", "devorg")
    pg.wait_for_function("document.getElementById('gt-group').options.length > 2", timeout=10000)
    pg.select_option("#gt-group", "2")
    pg.wait_for_selector("#gt-devices table", timeout=10000)
    check("Office AP" in pg.inner_text("#gt-devices") and pg.locator("#gt-devices [data-track]").count() == 0,
          "a router without GPS is listed but can't be tracked")
    pg.select_option("#gt-group", "38")
    pg.wait_for_selector("#gt-devices [data-track]", timeout=10000)
    rows = pg.inner_text("#gt-devices")
    check("Patrol Car 1" in rows and "Parked Trailer" in rows, "Organization → Group lists that group's GPS routers")
    pg.fill("#gt-filter", "trailer")
    check(pg.locator("#gt-devices tbody tr").count() == 1, "search narrows the list")
    pg.fill("#gt-filter", "")
    pg.screenshot(path=f"{out}/guards-sites-find.png", full_page=True)
    t0 = time.time()
    pg.click("#gt-devices [data-track='0']"); pg.wait_for_timeout(800)
    pg.click("#gt-devices [data-track]")
    pg.wait_for_function("document.querySelectorAll('#gt-trackers tr[data-id]').length === 2", timeout=5000)
    pg.wait_for_function("[...document.querySelectorAll('#gt-trackers tr[data-id]')].every((r) => !r.textContent.includes('none yet'))", timeout=30000)
    check(True, f"both trackers have a position {time.time() - t0:.1f} s after Track (poller woken, not waiting for its round)")
    pg.screenshot(path=f"{out}/guards-sites-tracked.png", full_page=True)

    # ---- Map page
    pg.goto(f"{PORTAL}/")
    pg.wait_for_selector(".guard-icon .guard--live", timeout=15000)
    check("Patrol Car 1" in pg.inner_text(".guard--live .guard-label") and "mph" in pg.inner_text(".guard--live .guard-label"),
          "live marker with name and speed")
    check(pg.locator(".guard-icon .guard--stale").count() == 1 and "last seen" in pg.inner_text(".guard--stale .guard-label"),
          "the trailer that reported yesterday is grey: 'last seen …'")
    check(pg.locator("#guard-list li").count() == 2, "Guards list in the side panel")
    pg.click("#guard-list li:has-text('Patrol Car 1')"); pg.wait_for_timeout(1200)
    check("following" in pg.get_attribute("#guard-list li:has-text('Patrol Car 1')", "class"), "clicking a guard zooms to it and follows")
    before = pg.evaluate("() => { const g = [...document.querySelectorAll('.guard-icon')].find((x) => x.textContent.includes('Patrol')); return g && g.style.transform; }")
    t0 = time.time()
    pg.wait_for_function("(b) => { const g = [...document.querySelectorAll('.guard-icon')].find((x) => x.textContent.includes('Patrol')); return g && g.style.transform !== b; }",
                         arg=before, timeout=60000)
    check(True, f"the patrol car moved on the map {time.time() - t0:.0f} s later (fake IC2 reports every 20 s, polled every 15 s)")
    check(pg.locator(".leaflet-guards-pane path").count() >= 1, "its trail is drawn")
    t0 = time.time()
    pg.click("#guard-list li:has-text('Patrol Car 1') [data-refresh]")
    pg.wait_for_selector(".toast:has-text('position from')", timeout=10000)
    check(True, f"↻ in the Guards list gets the position now ({time.time() - t0:.1f} s)")
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_ic2/rename/3?name=Patrol%20Car%207", method="POST"))
    pg.wait_for_timeout(5200)                               # a refresh asks IC2 at most every 5 s per guard
    pg.click("#guard-list li:has-text('Patrol Car 1') [data-refresh]")
    pg.wait_for_selector("#guard-list li:has-text('Patrol Car 7')", timeout=10000)
    check("Patrol Car 7" in pg.inner_text(".guard--live .guard-label"), "a router renamed in InControl2 is renamed on the map")
    pg.screenshot(path=f"{out}/guards-map.png")
    print(f"  alarm with guards on the map: {alarm_ms(pg)} ms")

    # ---- 3D view (forced on: headless has software WebGL only)
    pg.evaluate("localStorage.setItem('twg-3d-force', '2')")
    pg.reload(); pg.wait_for_timeout(1500)
    if pg.locator("#critical-dialog[open]").count():
        pg.keyboard.press("Escape")
    pg.click(".site-item"); pg.wait_for_timeout(600); pg.click("[data-globe]")
    pg.wait_for_function("document.querySelector('#globe canvas') && document.getElementById('globe-msg').hidden", timeout=40000)
    pg.wait_for_timeout(2500)
    labels = pg.evaluate("() => window.__globe.entities.values.map((e) => e.label && e.label.text && e.label.text.getValue()).filter(Boolean)")
    check(any("PATROL CAR 7" in l for l in labels) and any("PARKED TRAILER" in l for l in labels), "the 3D view shows both")
    pg.screenshot(path=f"{out}/guards-3d.png")
    pg.click("#globe-close"); pg.wait_for_timeout(500)

    # ---- InControl2 failing / hanging
    ic2("fail")
    pg.wait_for_selector(".guard-icon .guard--err", timeout=40000)
    check(True, "InControl2 failing: the markers get a red ring (position kept)")
    ic2("hang")
    lat = [alarm_ms(pg) for _ in range(3)]
    print(f"  alarms while InControl2 hangs: {lat} ms")
    check(max(lat) < 1000, "alarms aren't slowed by InControl2 hanging")
    ic2("ok")
    pg.wait_for_function("!document.querySelector('.guard-icon .guard--err')", timeout=60000)
    check(True, "back to normal when InControl2 recovers")
    check(not errors, f"no page errors {errors[:3]}")
    b.close()

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

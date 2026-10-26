"""Geotab (ZenduONE / GoFleet / MyGeotab) vehicles: Sites page setup, Test connection, vehicles on the map with speed,
Geotab failing (rate limit, down, wrong password) said on the Sites page, and alarm latency while it polls / fails.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.geotab /tmp
(polling at 60 s: every alarm measured here came by push)
"""
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "."
ok = True


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + what)


def geo(mode):
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_geotab/mode/{mode}", method="POST"))


def arrive(pg):
    n = pg.locator(".feed-item.p2").count()
    t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
    return round((time.time() - t) * 1000)


with sync_playwright() as p:
    geo("ok")
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)

    # ---- Sites page
    pg.goto(f"{PORTAL}/sites")
    pg.wait_for_selector("#gx-form")
    pg.wait_for_function("document.getElementById('gx-status').textContent.trim() !== ''")   # loaded: paint() won't clear what's typed
    pg.fill("#gx-db", "dev"); pg.fill("#gx-user", "dev-user"); pg.fill("#gx-pass", "dev-pass"); pg.fill("#gx-poll", "30")
    pg.check("#gx-enabled")
    t0 = time.time()
    pg.click("#gx-form button[type=submit]")
    pg.wait_for_function("document.getElementById('gx-status').textContent.includes('Working: 3 vehicles')", timeout=10000)
    check(True, f"Save: 3 vehicles read from Geotab and listed {time.time() - t0:.2f} s after the click")
    check("dev-pass" not in pg.content() and pg.get_attribute("#gx-pass", "placeholder").startswith("Saved"),
          "the password isn't sent back to the page")
    names = pg.evaluate("[...document.querySelectorAll('#gt-trackers tr[data-id] input[type=text]')].map(i => i.value)")
    check(sorted(names) == ["Patrol 1", "Patrol 2", "Spare"] and pg.locator("#gt-trackers .chip:text-is('Geotab')").count() == 3,
          f"tracked list: {names}, each with a Geotab chip; the retired device left out")
    pg.click("#gx-test")
    pg.wait_for_function("document.getElementById('gx-test-result').textContent.includes('Connected')", timeout=10000)
    check("3 vehicles, 2 with a position" in pg.inner_text("#gx-test-result"), f"Test connection: {pg.inner_text('#gx-test-result')[:90]!r}")
    pg.locator("#gx-form details summary").click()
    pg.screenshot(path=f"{out}/geotab-sites.png", full_page=True)

    # ---- Map
    pg.goto(f"{PORTAL}/")
    pg.wait_for_function("[...document.querySelectorAll('.guard-label')].some(e => e.textContent.includes('Patrol 1'))", timeout=15000)
    label = pg.evaluate("[...document.querySelectorAll('.guard-label')].find(e => e.textContent.includes('Patrol 1')).textContent")
    check("mph" in label, f"Patrol 1 on the map with its speed: {label.strip()!r}")
    pg.mouse.click(700, 500)
    lat = [arrive(pg) for _ in range(3)]
    check(max(lat) < 1000, f"alarms on screen in {lat} ms with Geotab polling")
    pg.screenshot(path=f"{out}/geotab-map.png")

    # ---- Geotab failing: alarms unaffected, the Sites page says what's wrong
    for mode, words in (("overlimit", "rate limit"), ("fail", "HTTP 500")):
        geo(mode)
        pg.evaluate("""async () => { const t = document.querySelector('meta[name=csrf-token]').content;
            await fetch('/api/tracking/geotab', {method:'PUT', headers:{'Content-Type':'application/json','X-CSRF-Token':t},
              body: JSON.stringify({enabled: true, database: 'dev', username: 'dev-user', server: '', poll_s: 30})}); }""")
        lat = [arrive(pg) for _ in range(2)]
        err = pg.evaluate("fetch('/api/tracking').then(r => r.json()).then(v => v.geotab.last_error)")
        check(words in err and max(lat) < 1000, f"Geotab {mode}: {err!r}; alarms {lat} ms")
    geo("unauthorized")
    pg.goto(f"{PORTAL}/sites")
    pg.wait_for_selector("#gx-form")
    pg.wait_for_function("document.getElementById('gx-status').textContent.trim() !== ''")   # loaded: paint() won't clear what's typed
    pg.fill("#gx-pass", "wrong"); pg.click("#gx-form button[type=submit]")
    pg.wait_for_function("document.getElementById('gx-status').textContent.includes('refused')", timeout=10000)
    check("refused the database, user name or password" in pg.inner_text("#gx-status"), "wrong password said on the Sites page")
    pg.screenshot(path=f"{out}/geotab-refused.png")
    geo("ok")
    pg.fill("#gx-pass", "dev-pass"); pg.click("#gx-form button[type=submit]")
    pg.wait_for_function("document.getElementById('gx-status').textContent.includes('Working')", timeout=10000)
    check(True, "right password saved again: working at once")
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if ok else "SOME CHECKS FAILED")

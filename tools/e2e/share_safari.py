"""Location sharing the way Safari needs it (run with E2E_BROWSER=webkit too, see tools/e2e/dispatch.py):
one location request per tap (two at once let Safari refuse the second), and reopening the page never asks for the
location without a tap unless the browser already allows it (Safari silently refuses those: "blocked, no prompt").

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.share_safari [screenshot_dir]
"""
import os
import sys

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
# Count every location request the page makes.
COUNT = """(() => { window.__geo = 0; const g = navigator.geolocation;
  for (const k of ["getCurrentPosition", "watchPosition"]) { const f = g[k].bind(g); g[k] = (...a) => { window.__geo++; return f(...a); }; } })()"""


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


API = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
    return [r.status, await r.json()]; }"""

with sync_playwright() as p:
    b = getattr(p, os.environ.get("E2E_BROWSER", "chromium")).launch()
    ctx = b.new_context(viewport={"width": 1300, "height": 900})
    pg = ctx.new_page()
    login_and_add_site(pg)
    pg.wait_for_timeout(1500)
    pg.evaluate(API, ["/api/platform/settings/email", "PUT", {"enabled": False, "portal_url": PORTAL}])   # links point at dev
    s, u = pg.evaluate(API, ["/api/users", "POST", {"email": "g@twgsecurity.com", "display_name": "G", "password": "Guard-password-123!"}])
    s, r = pg.evaluate(API, ["/api/tracking/phones", "POST", {"user_id": u["id"]}])
    link = r["share"]["link"]
    check(s == 200 and link.startswith(PORTAL + "/g/"), f"a phone guard and its location link (on this test portal: {link[:30]}…)")
    if not link.startswith(PORTAL):
        sys.exit("the link doesn't point at the test portal: stopping before a phone opens it")

    geo = {"latitude": 40.2670, "longitude": -76.8838, "accuracy": 8}
    # 1. Allowed: one request per tap, and it shares.
    allowed = b.new_context(viewport={"width": 390, "height": 844}, permissions=["geolocation"], geolocation=geo)
    allowed.add_init_script(COUNT)
    ph = allowed.new_page()
    errors = []
    ph.on("pageerror", lambda e: errors.append(str(e)))
    ph.goto(link)
    check(ph.evaluate("window.__geo") == 0, "opening the page asks for nothing")
    ph.click("#share-start")
    ph.wait_for_function("document.getElementById('share-state').textContent.includes('Sharing your location')", timeout=10000)
    check(ph.evaluate("window.__geo") == 1, f"one tap = one location request ({ph.evaluate('window.__geo')})")
    # 2. Reopened while sharing, location allowed: carries on by itself.
    ph.reload()
    ph.wait_for_function("document.getElementById('share-state').textContent.includes('Sharing your location')", timeout=10000)
    check(True, "reopened with location allowed: sharing resumes by itself")

    # 3. Reopened while sharing, location NOT allowed: no request without a tap, a clear prompt instead.
    denied = b.new_context(viewport={"width": 390, "height": 844}, geolocation=geo)
    denied.add_init_script(COUNT)
    dp = denied.new_page()
    dp.on("pageerror", lambda e: errors.append(str(e)))
    dp.goto(link)
    dp.evaluate("localStorage.setItem('twg-share-on', '1')")
    dp.reload()
    dp.wait_for_timeout(1500)
    check(dp.evaluate("window.__geo") == 0 and "Tap Start sharing" in dp.inner_text("#share-state"),
          f"reopened without permission: no request until a tap, says to tap ({dp.inner_text('#share-state')!r})")
    dp.screenshot(path=f"{out}/share-safari-reopen.png")

    # "Request location" (troubleshooting): what the phone answered, on screen and in the portal's audit log.
    dp.click("#loc-test")
    dp.wait_for_selector(".loc-test-result [data-sent]:has-text('Sent to TWG')", timeout=15000)
    bad = dp.inner_text(".loc-test-result")
    check("PERMISSION_DENIED" in bad and ("refused at once" in bad or "was refused" in bad), f"Request location, location blocked: says so ({bad.splitlines()[0]!r})")
    dp.screenshot(path=f"{out}/share-safari-request-blocked.png")
    ph.click("#loc-test")
    ph.wait_for_selector(".loc-test-result [data-sent]:has-text('Sent to TWG')", timeout=30000)
    good = ph.inner_text(".loc-test-result")
    check(good.startswith("Location works"), f"Request location, allowed: works ({good.splitlines()[0]!r})")
    rows = [r for r in pg.evaluate(API, ["/api/audit?limit=200", "GET", None])[1] if r["action"] == "location.test"]
    check(len(rows) == 2 and {r["detail"]["outcome"] for r in rows} == {"ok", "error"},
          f"both reports reached the audit log ({[(r['detail']['outcome'], r['detail'].get('code'), r['detail']['ms']) for r in rows]})")
    check(not errors, f"no page errors {errors}")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

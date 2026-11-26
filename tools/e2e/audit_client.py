"""Audit log says where each sign-in and action came from: website, iPhone app, Android app (app/audit.py client_of).
Signs in as the iPhone and Android apps (their X-TWG-Client header), acts from each, then checks the audit page's
labels and "Done from" filter, the user's signed-in list, and alarm latency with the marker on every request.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.audit_client /tmp
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "."
ok = True
EMAIL, PASSWORD = "admin@twgsecurity.com", "Smoke-test-password-123!"


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + what)


def app_call(path, client, token=None, body=None, method=None):
    headers = {"Content-Type": "application/json", "X-TWG-Client": client,
               "User-Agent": f"TWG-Alarms-{'iOS' if client.startswith('ios') else 'Android'}/{client.split('/')[1]}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{PORTAL}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")


def arrive(pg):
    n = pg.locator(".feed-item.p2").count()
    t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
    return round((time.time() - t) * 1000)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.goto(f"{PORTAL}/")
    pg.mouse.click(700, 500)
    lat = [arrive(pg) for _ in range(3)]
    check(max(lat) < 1000, f"alarms on screen in {lat} ms")
    alarm_id = pg.evaluate("fetch('/api/alarms?state=all').then(r => r.json()).then(a => (a.items || a)[0].id)")

    # The apps: sign in, add a note, sign out (Android stays signed in).
    ios = app_call("/api/mobile/login", "ios/1.6 (55)", body={"email": EMAIL, "password": PASSWORD, "device_name": "iPhone · iOS 26.1 · TWG Alarms 1.6"})["token"]
    app_call(f"/api/alarms/{alarm_id}/notes", "ios/1.6 (55)", ios, {"text": "from the iPhone"})
    app_call("/api/mobile/logout", "ios/1.6 (55)", ios, {})
    droid = app_call("/api/mobile/login", "android/0.1 (1)", body={"email": EMAIL, "password": PASSWORD, "device_name": "Google Pixel 8 · Android 15 · TWG Alarms"})["token"]
    app_call(f"/api/alarms/{alarm_id}/notes", "android/0.1 (1)", droid, {"text": "from the Android"})
    pg.evaluate(f"""async () => {{ const t = document.querySelector('meta[name=csrf-token]').content;
        await fetch('/api/alarms/{alarm_id}/notes', {{method:'POST', headers:{{'Content-Type':'application/json','X-CSRF-Token':t}},
          body: JSON.stringify({{text: 'from the desk'}})}}); }}""")

    # Audit page: a label per row, and the filter.
    pg.goto(f"{PORTAL}/audit")
    pg.wait_for_selector("#audit-rows .client-chip")
    chips = pg.evaluate("[...document.querySelectorAll('#audit-rows tr')].map(tr => [tr.children[1].textContent, tr.querySelector('.client-chip')?.textContent || ''])")
    seen = {(a, c) for a, c in chips}
    check({("Note added", "iPhone 1.6"), ("Note added", "Android 0.1"), ("Note added", "Web"), ("Signed in", "iPhone 1.6"),
           ("Signed out", "iPhone 1.6"), ("Signed in", "Android 0.1"), ("Signed in", "Web")} <= seen,
          f"labels: {sorted(seen)[:12]}")
    title = pg.get_attribute("#audit-rows .client-chip:text-is('iPhone 1.6')", "title")
    check(title == "iPhone app 1.6 (55)", f"hover says the full version: {title!r}")
    pg.select_option("#f-client", "android")
    pg.wait_for_function("[...document.querySelectorAll('#audit-rows .client-chip')].every(c => c.textContent.startsWith('Android'))")
    n_android = pg.locator("#audit-rows tr").count()
    check(n_android == 2, f"Done from: Android app -> {n_android} rows (sign-in, note)")
    pg.select_option("#f-client", "apps")
    pg.wait_for_function("document.querySelectorAll('#audit-rows tr').length === 5")
    check(True, "Done from: Phone apps (all) -> 5 rows")
    pg.screenshot(path=f"{out}/audit-client.png", full_page=True)

    # The user's signed-in list shows the Android app and its version.
    me = pg.evaluate("fetch('/api/account').then(r => r.json())")
    clients = sorted((s["client"], s["client_version"]) for s in me["sessions"])
    check(("android", "0.1 (1)") in clients and ("web", "") in clients and ("ios", "1.6 (55)") not in clients,
          f"signed-in list: {clients}")
    pg.goto(f"{PORTAL}/account")
    pg.wait_for_function("document.getElementById('sess-rows').textContent.includes('Android app 0.1 (1)')", timeout=10000)
    check(True, "Account page: 'Android app 0.1 (1)' in the signed-in list")
    pg.screenshot(path=f"{out}/audit-client-account.png", full_page=True)
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if ok else "SOME CHECKS FAILED")

"""The phone apps' company switcher on a real server: TWG staff send X-TWG-Company and the app's live stream follows
that company plus their own (app/scope.py). Two companies with their own fake NX servers; times each alarm from the
NX event to the app's stream and to the website's screen.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.app_company
"""
import json
import queue
import subprocess
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import LOGIN, PORTAL, inject, login_and_add_site

ACME_NX = "http://127.0.0.1:8198"
fails = []
IOS = {"X-TWG-Client": "ios/1.7 (60)", "Content-Type": "application/json"}


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def inject_acme(kind):
    urllib.request.urlopen(urllib.request.Request(f"{ACME_NX}/_inject/{kind}", method="POST"))


def call(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json().catch(() => null)}; }""", [method, path, body])


def app(path, token=None, company=None, body=None):
    h = {**IOS, **({"Authorization": f"Bearer {token}"} if token else {}), **({"X-TWG-Company": company} if company else {})}
    req = urllib.request.Request(f"{PORTAL}{path}", data=json.dumps(body).encode() if body is not None else None, headers=h,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def stream(token, company):
    """The app's /api/events in a thread: (arrival time, event, data) on a queue."""
    q = queue.Queue()

    def run():
        req = urllib.request.Request(f"{PORTAL}/api/events", headers={**IOS, "Authorization": f"Bearer {token}",
                                                                      "X-TWG-Company": company})
        with urllib.request.urlopen(req) as r:
            event = None
            for line in r:
                line = line.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    q.put((time.time(), event, json.loads(line[6:])))
    threading.Thread(target=run, daemon=True).start()
    return q


def wait_alarm(q, tenant_id, after, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            t, ev, d = q.get(timeout=max(0.05, end - time.time()))
        except queue.Empty:
            break
        if ev == "alarm.new" and d.get("tenant_id") == tenant_id and t >= after:
            return round((t - after) * 1000), d
    return None, None


def drained(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


nx2 = subprocess.Popen([".venv/bin/uvicorn", "tools.fake_nx:app", "--port", "8198"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{ACME_NX}/rest/v4/site/info", timeout=1); break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    with sync_playwright() as p:
        b = p.chromium.launch()
        web = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
        login_and_add_site(web)
        acme_id = call(web, "POST", "/api/tenants", {"name": "Acme Security", "display_name": "Acme", "admin_email": "admin@acme.test",
                                                    "admin_password": "Acme-password-123!"})["body"]["id"]
        call(web, "POST", "/api/scope", {"scope": str(acme_id)})
        call(web, "POST", "/api/sites", {"name": "Acme Warehouse", "host": ACME_NX, "nx_user": "portal", "nx_pass": "x",
                                         "address": "Allentown, PA", "lat": 40.6084, "lng": -75.4902})
        call(web, "POST", "/api/scope", {"scope": "own"})
        web.goto(f"{PORTAL}/"); web.mouse.click(700, 500)
        web.wait_for_timeout(2500)                                       # both sites' NX push connections

        tok = app("/api/mobile/login", body={"email": LOGIN[0], "password": LOGIN[1], "device_name": "iPhone · e2e"})[1]["token"]
        st, tenants = app("/api/tenants", tok)
        check(st == 200 and {t["id"] for t in tenants} >= {acme_id}, f"the app's picker lists the companies ({st})")
        own_id = next(t["id"] for t in tenants if t["own"])
        sites = lambda c: sorted(s["name"] for s in app("/api/sites", tok, c)[1])  # noqa: E731
        check(sites(str(acme_id)) == ["Acme Warehouse"], f"viewing Acme: {sites(str(acme_id))}")
        check(sites("own") == ["Fake Test Site"], f"own company: {sites('own')}")

        viewing, own_only = stream(tok, str(acme_id)), stream(tok, "own")
        time.sleep(1.5)
        drained(viewing); drained(own_only)
        lat_acme, lat_own, lat_web = [], [], []
        for _ in range(3):
            t = time.time(); inject_acme("line")
            ms, d = wait_alarm(viewing, acme_id, t)
            lat_acme.append(ms)
            n = web.locator(".feed-item.p2").count()
            t = time.time(); inject("line")
            ms, d = wait_alarm(viewing, own_id, t)
            lat_own.append(ms)
            web.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
            lat_web.append(round((time.time() - t) * 1000))
        check(all(x is not None and x < 1000 for x in lat_acme), f"Acme's alarms reach the app viewing Acme in {lat_acme} ms")
        check(all(x is not None and x < 1000 for x in lat_own), f"its own company's alarms still reach it in {lat_own} ms")
        check(max(lat_web) < 1000, f"website alarms on screen in {lat_web} ms")
        time.sleep(0.5)
        leaked = [d for _, ev, d in drained(own_only) if d.get("tenant_id") == acme_id]
        check(not leaked, f"an app on its own company gets none of Acme's events ({len(leaked)})")

        aid = app("/api/alarms?state=open", tok, str(acme_id))[1][0]["id"]
        st, _ = app(f"/api/alarms/{aid}/notes", tok, str(acme_id), {"text": "TWG looked from the app"})
        check(st == 200, f"TWG support adds a note in Acme from the app ({st})")
        web.goto(f"{PORTAL}/audit")
        call(web, "POST", "/api/scope", {"scope": str(acme_id)})
        log = call(web, "GET", "/api/audit")["body"]
        viewed = [r for r in log if r["action"] == "support.viewed" and r["client"] == "ios"]
        check(len(viewed) == 1, f"Acme's log: support.viewed from the iPhone app once ({len(viewed)})")
        b.close()
finally:
    nx2.terminate()

print("ALL PASS" if not fails else f"{len(fails)} FAILED")

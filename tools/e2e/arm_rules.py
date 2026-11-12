"""NX rules tagged #arm follow arming, end to end (app/services/arm_rules.py): tag a rule in NX, disarm from the Sites
page and time how long until NX has it off, arm and time it back on, then alarms; then NX refusing the switch (the
portal's account without rights): the site says so, a system alarm comes in, and it fixes itself once NX allows it.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.arm_rules [screenshot_dir]
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "."
fails = []
TAG = "Strobe + speaker on line crossing #arm #alarm"


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def nx(path, method="GET"):
    with urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}{path}", method=method)) as r:
        return json.loads(r.read() or b"null")


def rule_on(comment=TAG):
    return next(r for r in nx("/rest/v4/events/rules") if r["comment"] == comment)["enabled"] is not False


def until(cond, timeout=30.0):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return round((time.time() - t) * 1000)
        time.sleep(0.02)
    return None


def call(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json().catch(() => null)}; }""", [method, path, body])


def arrive(pg, sel=".feed-item.p2", kind="line", timeout=10000):
    n = pg.locator(sel).count()
    t = time.time(); inject(kind)
    pg.wait_for_function(f"document.querySelectorAll('{sel}').length > {n}", timeout=timeout)
    return round((time.time() - t) * 1000)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    sid = pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].id)")
    nx("/_rules/line?comment=" + urllib.parse.quote(TAG), "POST")       # tagged in Nx Witness
    call(pg, "POST", f"/api/sites/{sid}/check")                          # the portal reads the rules
    check(rule_on() and rule_on("Panic button"), "armed: the #arm rule is on in NX")

    # 1. Disarm from the Sites page: the dialog says what it switches off, NX has it off within a second.
    pg.goto(f"{PORTAL}/sites")
    pg.click(f"button[data-arm=disarm][data-id='{sid}']")
    pg.wait_for_selector("#arm-dialog[open]")
    summary = pg.inner_text("#arm-summary")
    check("1 NX rule tagged #arm" in summary, f"the disarm dialog says what it switches off: {summary!r}")
    pg.screenshot(path=f"{out}/arm-rules-1-dialog.png")
    t = time.time()
    pg.click("#arm-submit")
    ms = until(lambda: not rule_on(), 10)
    check(ms is not None and ms < 1500, f"disarmed: the #arm rule is off in NX {ms} ms after the click")
    check(rule_on("Panic button") and rule_on("Hold-up button #24h"), "untagged and #24h rules untouched")
    pg.wait_for_function("document.body.innerText.includes('switched off in NX')", timeout=10000)
    pg.screenshot(path=f"{out}/arm-rules-2-sites.png", full_page=True)

    # 2. The map's rule list shows the tag and what it does.
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    rules = call(pg, "GET", f"/api/sites/{sid}/rules")["body"]["rules"]
    line = next(r for r in rules if r["comment"] == TAG)
    check(line["arm"] and line["enabled"] is False and line["level"] == "off", f"the rules list: #arm, off in NX ({line['level']})")

    # 3. Arm: back on, alarms come in as before.
    pg.goto(f"{PORTAL}/sites")
    t = time.time()
    pg.click(f"button[data-arm=arm][data-id='{sid}']")
    ms = until(rule_on, 10)
    check(ms is not None and ms < 1500, f"armed: the #arm rule is back on in NX {ms} ms after the click")
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    for _ in range(40):
        if pg.evaluate(f"fetch('/api/sites').then(r => r.json()).then(s => s[0].push)"):
            break
        time.sleep(0.25)
    lat = [arrive(pg) for _ in range(3)]
    check(max(lat) < 1000, f"alarms after arming on screen in {lat} ms")

    # 4. NX refuses (the portal's NX account lost its rights): the site says why, a system alarm comes in.
    nx("/_rules_mode/refuse", "POST")
    n = pg.locator(".feed-item").count()
    r = call(pg, "POST", f"/api/sites/{sid}/disarm", {"minutes": 30})
    t = time.time()
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {n}", timeout=30000)
    took = round(time.time() - t, 1)
    feed = pg.inner_text("#feed") if pg.locator("#feed").count() else pg.inner_text("body")
    check("#arm rules not off" in feed or "didn't switch off" in feed,
          f"NX refusing: a system alarm comes in {took} s after disarming, though the site is disarmed")
    s = call(pg, "GET", f"/api/sites/{sid}")["body"]
    check(s["arm_rules"]["state"] == "failed" and "Power Users" in s["arm_rules"]["detail"], f"the site says why: {s['arm_rules']['detail'][:90]!r}")
    pg.goto(f"{PORTAL}/sites")
    pg.wait_for_function("document.body.innerText.includes(\"Couldn't switch\")", timeout=10000)
    pg.screenshot(path=f"{out}/arm-rules-3-failed.png", full_page=True)
    nx("/_rules_mode/allow", "POST")
    ms = until(lambda: not rule_on(), 40)
    check(ms is not None, f"rights given back: switched off by the next retry {round(ms / 1000, 1) if ms else '-'} s later")
    pg.wait_for_function("document.body.innerText.includes('switched off in NX')", timeout=15000)
    call(pg, "POST", f"/api/sites/{sid}/arm", {})
    check(until(rule_on, 10) is not None, "armed again: back on")
    log = [r for r in call(pg, "GET", "/api/audit")["body"] if r["action"] == "site.arm_rules"]
    check(len(log) >= 4, f"audit log: {len(log)} '#arm NX rules switched' rows")
    pg.goto(f"{PORTAL}/audit")
    pg.wait_for_function("document.body.innerText.includes('#arm NX rules switched')", timeout=10000)
    pg.screenshot(path=f"{out}/arm-rules-4-audit.png")
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if not fails else f"{len(fails)} FAILED")

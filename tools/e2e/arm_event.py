"""Arming tells NX, end to end (app/services/arm_event.py): Edit site → Arming → "Tell NX when this site arms or
disarms", plus a soft-trigger webhook ticked "Run when the site arms". Disarm and arm from the Sites page and time each
step in NX (event, trigger, #arm rule) and its order; the portal doesn't alarm on its own message; alarms after arming;
the #arm option off leaves the rules on.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.arm_event [screenshot_dir]
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
RULE = "Siren on Site armed #arm"


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def nx(path, method="GET"):
    with urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}{path}", method=method)) as r:
        return json.loads(r.read() or b"null")


def until(cond, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return round((time.time() - t) * 1000)
        time.sleep(0.02)
    return None


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    login_and_add_site(pg)
    sid = pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].id)")
    nx("/_rules/line?comment=" + urllib.parse.quote(RULE), "POST")
    pg.wait_for_timeout(2500)

    # 1. Edit site → Arming: tell NX.
    pg.goto(f"{PORTAL}/sites/{sid}/edit")
    pg.wait_for_selector("#ae-on")
    check(pg.is_checked("#ae-rules") and not pg.is_checked("#ae-on"), "defaults: #arm rules on, event off")
    pg.check("#ae-on")
    pg.screenshot(path=f"{out}/ae-1-form.png", full_page=True)
    pg.click("#sched-save")
    pg.wait_for_selector("#sched-state:has-text('Saved')")

    # 2. A soft-trigger webhook that runs when the site arms.
    pg.goto(f"{PORTAL}/sites#webhooks")
    pg.wait_for_selector("#wh-site option", state="attached")
    pg.select_option("#wh-kind", "trigger")
    pg.wait_for_selector("#wh-trigger option:has-text('Strobe speaker armed')", state="attached")
    pg.select_option("#wh-trigger", label="Strobe speaker armed")
    pg.fill("#wh-name", "Strobe chirp on arm")
    pg.check("#wh-on-arm")
    pg.click("#wh-submit")
    pg.wait_for_selector("#wh-rows tr:has-text('Strobe chirp on arm') .chip:has-text('On arm')")
    pg.evaluate("fetch('/api/sites/%d/check', {method:'POST', headers:{'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}})" % sid)
    pg.wait_for_timeout(1000)

    # 3. Disarm from the Sites page: NX hears "Site disarmed", then the #arm rule goes off.
    pg.goto(f"{PORTAL}/sites")
    n0 = len(nx("/_nx_order"))
    pg.click(f"button[data-arm=disarm][data-id='{sid}']")
    pg.wait_for_selector("#arm-dialog[open]")
    t = time.time(); pg.click("#arm-submit")
    ms_ev = until(lambda: ["generic", "Site disarmed"] in nx("/_nx_order")[n0:])
    ms_rule = until(lambda: ["rule", RULE, False] in nx("/_nx_order")[n0:])
    order = nx("/_nx_order")[n0:]
    check(ms_ev is not None and ms_ev < 1500, f"disarm → NX got “Site disarmed” {ms_ev} ms after the click")
    check(order[:2] == [["generic", "Site disarmed"], ["rule", RULE, False]], f"…before the #arm rule went off: {order}")
    desc = [x for x in nx("/_nx_sent") if x.get("caption") == "Site disarmed"][-1]["description"]
    check(desc.startswith("Disarmed by Dev Admin") and "re-arms at" in desc, f"description: {desc!r}")

    # 4. Arm: the #arm rule on, then "Site armed" and the soft trigger.
    n0 = len(nx("/_nx_order")); s0 = len(nx("/_nx_sent"))
    t = time.time(); pg.click(f"button[data-arm=arm][data-id='{sid}']")
    ms_ev = until(lambda: ["generic", "Site armed"] in nx("/_nx_order")[n0:])
    ms_tr = until(lambda: any(x["kind"] == "trigger" for x in nx("/_nx_sent")[s0:]))
    order = nx("/_nx_order")[n0:]
    check(ms_ev is not None and ms_ev < 1500, f"arm → NX got “Site armed” {ms_ev} ms after the click")
    check(ms_tr is not None and ms_tr < 1500, f"…and the strobe soft trigger {ms_tr} ms after")
    check(order[:2] == [["rule", RULE, True], ["generic", "Site armed"]], f"…after the #arm rule came back on: {order}")

    # 5. The portal doesn't alarm on its own message (NX logged it back); real alarms still come in fast.
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    pg.wait_for_timeout(2000)
    check(pg.locator("#critical-dialog[open]").count() == 0 and pg.locator(".feed-item.p1").count() == 0,
          "the strobe soft trigger fired on arm doesn't come back as a critical alarm")
    feed = pg.inner_text("body")
    check("Site armed" not in feed and "Site disarmed" not in feed, "no alarm for the portal's own arm/disarm message")
    lat = []
    for _ in range(3):
        n = pg.locator(".feed-item.p2").count()
        t = time.time(); inject("line")
        pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
        lat.append(round((time.time() - t) * 1000))
    check(max(lat) < 1000, f"alarms after arming on screen in {lat} ms")
    pg.screenshot(path=f"{out}/ae-2-feed.png")

    # 6. #arm option off: disarming leaves the rule on.
    pg.goto(f"{PORTAL}/sites/{sid}/edit")
    pg.wait_for_selector("#ae-rules")
    pg.uncheck("#ae-rules")
    pg.click("#sched-save")
    pg.wait_for_selector("#sched-state:has-text('Saved')")
    n0 = len(nx("/_nx_order"))
    pg.evaluate("fetch('/api/sites/%d/disarm', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content}, body: '{}'})" % sid)
    time.sleep(2)
    order = nx("/_nx_order")[n0:]
    check(["generic", "Site disarmed"] in order and not [o for o in order if o[0] == "rule"],
          f"#arm option off: event sent, rule left on ({order})")
    pg.goto(f"{PORTAL}/audit")
    pg.wait_for_function("document.body.innerText.includes('Arm/disarm sent to NX')", timeout=10000)
    pg.screenshot(path=f"{out}/ae-3-audit.png")
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if not fails else f"{len(fails)} FAILED")

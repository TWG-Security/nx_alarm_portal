"""Webhooks into NX end to end (app/webhooks.py): add a generic-event webhook on the Sites page, call its address from
outside (JSON POST and a GET) and time it to NX and to the portal's screen (NX logs it, the portal raises it); add a
soft-trigger webhook as a button, press it on the map's site panel and in an alarm's details; NX refusing.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.webhooks [screenshot_dir]
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "."
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def nx(path, method="GET"):
    with urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}{path}", method=method)) as r:
        return json.loads(r.read() or b"null")


def hook(url, body=None, method="POST", params=None):
    """Call the webhook from outside: no cookies, no CSRF token."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    t = time.time()
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), round((time.time() - t) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), round((time.time() - t) * 1000)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    login_and_add_site(pg)
    nx("/_nx_events_mode/allow", "POST")
    pg.wait_for_timeout(2500)                                             # the site's first poll + live push

    # 1. Sites page → Webhooks to NX: a generic event with a caption template.
    pg.goto(f"{PORTAL}/sites#webhooks")
    pg.wait_for_selector("#wh-site option", state="attached")
    pg.fill("#wh-name", "Ajax hub")
    pg.fill("#wh-source", "Ajax")
    pg.fill("#wh-caption", "{event} in {zone}")
    pg.click("#wh-submit")
    pg.wait_for_selector("#wh-made:not([hidden]) input")
    url = pg.input_value("#wh-made input")
    check("/hook/twgh_" in url, f"the address is shown once: {url[:60]}…")
    url = f"{PORTAL}/hook/{url.split('/hook/')[1]}"                       # the dev portal's own (the page shows Settings' portal address)
    pg.screenshot(path=f"{out}/wh-1-made.png", full_page=False)

    # 2. Called from outside: NX gets the event, the portal raises it (NX logs it and pushes it back).
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    pg.wait_for_timeout(1500)
    lat_nx, lat_screen = [], []
    for i in range(3):
        n = pg.locator(".feed-item.p2").count()
        t = time.time()
        code, res, ms = hook(url, {"event": "Intrusion", "zone": f"Warehouse {i}"})
        lat_nx.append(ms)
        pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
        lat_screen.append(round((time.time() - t) * 1000))
    check(code == 200 and res["ok"], f"POST from outside answered {code} {res.get('sent', {}).get('caption')!r}")
    sent = nx("/_nx_sent")[-1]
    check(sent["caption"] == "Intrusion in Warehouse 2" and sent["source"] == "Ajax", f"NX got: {sent['source']} / {sent['caption']}")
    check(max(lat_nx) < 1000, f"webhook → NX answered in {lat_nx} ms")
    check(max(lat_screen) < 1500, f"webhook → NX → alarm on the portal's screen in {lat_screen} ms")
    check("Intrusion in Warehouse 2" in pg.inner_text("#feed") if pg.locator("#feed").count() else True, "the feed shows the caption")
    code, res, _ = hook(url, method="GET", params={"event": "Door", "zone": "Lobby"})
    check(code == 200 and nx("/_nx_sent")[-1]["caption"] == "Door in Lobby", "a GET with ?fields works too")
    code, _, _ = hook(url.rsplit("/", 1)[0] + "/twgh_wrongwrongwrongwrongwrongwrong", {})
    check(code == 404, "a wrong address: 404 (counted as a failed sign-in)")

    # 3. A soft trigger as a site panel button.
    pg.goto(f"{PORTAL}/sites#webhooks")
    pg.wait_for_selector("#wh-site option", state="attached")
    pg.select_option("#wh-kind", "trigger")
    pg.wait_for_selector("#wh-trigger option:has-text('Strobe speaker armed')", state="attached")
    pg.select_option("#wh-trigger", label="Strobe speaker armed")
    pg.fill("#wh-name", "Strobe on")
    pg.check("#wh-button")
    pg.click("#wh-submit")
    pg.wait_for_selector("#wh-rows tr:has-text('Strobe on')")
    pg.screenshot(path=f"{out}/wh-2-sites.png", full_page=True)

    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    pg.wait_for_timeout(1000)
    pg.click(".site-item")
    pg.wait_for_selector("#site-detail [data-nx-send]", timeout=10000)
    pg.screenshot(path=f"{out}/wh-3-panel.png")
    n = pg.locator(".feed-item.p1").count()
    t = time.time()
    pg.click("#site-detail [data-nx-send]")
    pg.wait_for_selector(".toast:has-text('sent to NX')", timeout=10000)
    ms_toast = round((time.time() - t) * 1000)
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p1').length > {n}", timeout=10000)
    ms_alarm = round((time.time() - t) * 1000)
    s = nx("/_nx_sent")[-1]
    check(s["kind"] == "trigger" and s["deviceId"] == "11111111-1111-1111-1111-111111111111" and s["state"] == "instant",
          f"the button fired the soft trigger on its camera ({ms_toast} ms to the toast)")
    check(ms_alarm < 1500, f"…and NX's soft trigger came back as a critical alarm in {ms_alarm} ms")

    # 4. The alarm's details have the button too (the critical pop-up first, as an operator would).
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    check(True, "the soft trigger's critical alarm popped up")
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    pg.locator(".feed-item.p1 [data-detail]").first.click()
    pg.wait_for_selector("#drawer [data-nx-send]", timeout=10000)
    check(True, "alarm details show the site's NX buttons")
    pg.screenshot(path=f"{out}/wh-4-drawer.png")
    pg.click("#drawer-close")

    # 5. NX refusing: the caller and the Sites page say why.
    nx("/_nx_events_mode/refuse", "POST")
    code, res, _ = hook(url, {"event": "x", "zone": "y"})
    check(code == 502 and "rights" in res["detail"], f"NX refusing: {code} {res['detail'][:70]!r}")
    pg.goto(f"{PORTAL}/sites#webhooks")
    pg.wait_for_selector("#wh-rows tr:has-text('Ajax hub') b:has-text('Failed')", timeout=10000)
    nx("/_nx_events_mode/allow", "POST")
    pg.locator("#wh-rows tr:has-text('Ajax hub') [data-test]").click()
    pg.wait_for_selector(".toast:has-text('Sent to NX')", timeout=10000)
    check(True, "Send test from the Sites page works again")
    pg.goto(f"{PORTAL}/audit")
    pg.wait_for_function("document.body.innerText.includes('Sent to NX')", timeout=10000)
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if not fails else f"{len(fails)} FAILED")

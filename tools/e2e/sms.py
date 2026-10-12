"""Alarm texts end to end, against the fake Twilio in tools/fake_nx.py.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.sms [screenshot_dir]

Platform page → Text messages (save, test text), Users page (mobile number, alarm level, Test), an alarm
→ text with a link → the link on a phone-sized browser with no sign-in (details, clip, live), and the
failure paths: Twilio rejecting texts and Twilio hanging, with the alarm's time to the screen measured
each time (texts must never slow alarms down).
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
SID = "AC" + "1" * 32


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def texts() -> list[dict]:
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/_twilio"))


def twilio_mode(mode: str) -> None:
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_twilio/mode/{mode}", method="POST"))


def wait_texts(n: int, timeout=10.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        t = texts()
        if len(t) >= n:
            return t
        time.sleep(0.05)
    return texts()


def twilio_reply(frm: str, body: str) -> int:
    """A text someone sends to our number, as Twilio would post it (signed with the dev Auth Token)."""
    import base64, hashlib, hmac, urllib.parse
    params = {"From": frm, "To": "+15705550000", "Body": body, "MessageSid": "SMreply", "AccountSid": SID}
    url = f"{PORTAL}/twilio/sms"
    sig = base64.b64encode(hmac.new(b"dev-token", (url + "".join(k + v for k, v in sorted(params.items()))).encode(),
                                    hashlib.sha1).digest()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers={"X-Twilio-Signature": sig})
    return urllib.request.urlopen(req).status


def alarm_on_screen(pg, kind: str) -> float:
    """Inject an alarm and time it to the feed (ms)."""
    before = pg.locator(".feed-item").count()
    t0 = time.time()
    inject(kind)
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {before}", timeout=10000, polling=10)
    return (time.time() - t0) * 1000


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    api = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
        return [r.status, await r.json()]; }"""
    # Links must open from this test browser, so point the portal address at dev.
    st, _ = pg.evaluate(api, ["/api/platform/settings/email", "PUT", {"enabled": False, "portal_url": PORTAL}])
    check(st == 200, "portal address set to the dev server")

    # --- 1. Platform page → Text messages
    pg.goto(f"{PORTAL}/platform#sms")
    pg.click('#tabs [data-tab="sms"]')
    check("Text messages" in pg.inner_text("#tabs"), "Platform page has a Text messages tab")
    pg.check("#tx-enabled"); pg.fill("#tx-sid", SID); pg.fill("#tx-token", "dev-token"); pg.fill("#tx-from", "(570) 555-0000")
    pg.click("#sms-form button[type=submit]")
    pg.wait_for_selector(".toast:has-text('Text message settings saved')", timeout=5000)
    check(pg.input_value("#tx-from") == "+15705550000", "sender number normalised to +15705550000")
    check(pg.get_attribute("#tx-token", "placeholder").startswith("Saved"), "auth token saved, not shown back")
    pg.fill("#tx-test-to", "570-555-0100"); pg.click("#tx-test")
    pg.wait_for_selector(".toast:has-text('Test text sent')", timeout=8000)
    t = wait_texts(1)
    check(t and t[-1]["to"] == "+15705550100" and "test text" in t[-1]["body"], "test text reached Twilio")
    pg.wait_for_selector("#sms-log tr:has-text('Test')", timeout=5000)
    check(pg.input_value("#tx-max") == "10" and pg.input_value("#s-max") == "0",
          "text limit shows 10 and the Sessions tab's 'always sign out after' stays 0")
    pg.screenshot(path=f"{out}/sms1-platform.png", full_page=True)

    # --- 2. Users page: compact table, Edit dialog (mobile number, level, opt-in, Test), Add user dialog
    no_sideways = """() => document.documentElement.scrollWidth <= innerWidth
        && [...document.querySelectorAll('.table-wrap')].every((w) => w.scrollWidth <= w.clientWidth + 1)"""
    pg.goto(f"{PORTAL}/users")
    pg.wait_for_selector("[data-edit]")
    check(pg.evaluate(no_sideways), "Users table fits without sideways scrolling at 1500 px")
    pg.click("[data-edit]")
    pg.wait_for_selector("#edit-dialog[open]")
    pg.fill("#ed-phone", "(570) 555-0101")
    pg.click("#edit-form button[type=submit]")
    pg.wait_for_selector("#edit-dialog[open]", state="detached", timeout=5000)
    pg.wait_for_selector(".users-table .chip:has-text('Waiting for YES')", timeout=5000)
    check("Critical + Alarm" in pg.inner_text(".users-table"), "Edit: adding a number saves it with Critical + Alarm")
    ask = wait_texts(2)[-1]
    check(ask["to"] == "+15705550101" and "Reply YES to sign up" in ask["body"] and "STOP to opt out at any time" in ask["body"]
          and "/sms-terms" in ask["body"], "adding a number texts the opt-in request (YES, STOP, terms)")
    pg.click("[data-edit]"); pg.wait_for_selector("#edit-dialog[open]")
    check(pg.locator("#edit-dialog [data-test-text]").count() == 0 and pg.locator("#edit-dialog [data-opt-in]").count() == 1,
          "Edit dialog: Resend opt-in, no Test until they reply YES")
    pg.screenshot(path=f"{out}/sms2a-edit-waiting.png")
    pg.click("#ed-cancel")
    check(twilio_reply("+15705550101", "Yes") == 200, "Twilio-signed YES reply accepted")
    confirm = wait_texts(3)[-1]
    check("you're signed up" in confirm["body"], "YES gets the signed-up confirmation")
    pg.reload(); pg.wait_for_selector(".users-table .chip:has-text('Opted in')", timeout=5000)
    check(True, "Users page shows Opted in")
    pg.click("[data-edit]"); pg.wait_for_selector("#edit-dialog[open]")
    pg.click("#edit-dialog [data-test-text]")
    pg.wait_for_selector(".toast:has-text('Test text sent')", timeout=8000)
    pg.wait_for_selector("#edit-dialog .small-print:has-text('test text sent')", timeout=5000)
    check(wait_texts(4)[-1]["to"] == "+15705550101", "Edit dialog: Send a test text texts that person")
    pg.check('input[name="ed-scope"][value="some"]')
    pg.check("#ed-sites input[type=checkbox]")
    pg.screenshot(path=f"{out}/sms2b-edit-optedin.png")
    pg.click("#edit-form button[type=submit]")
    pg.wait_for_selector("#edit-dialog[open]", state="detached", timeout=5000)
    pg.wait_for_selector(".users-table .chip:has-text('Fake Test Site')", timeout=5000)
    check(True, "Edit: Only these sites -> the table shows the site")
    # Add user dialog: invite with a mobile number -> setup link shown, opt-in text sent
    pg.click("#add-user"); pg.wait_for_selector("#add-dialog[open]")
    pg.fill("#u-email", "nightshift@twgsecurity.com"); pg.fill("#u-name", "Night Shift"); pg.fill("#u-phone", "570 555 0102")
    pg.screenshot(path=f"{out}/sms2c-add.png")
    pg.click("#u-submit")
    pg.wait_for_selector("#u-link .alert", timeout=8000)
    check(pg.locator("#add-dialog[open]").count() == 1 and "Setup link" in pg.inner_text("#u-link"), "Add user: dialog shows the setup link")
    pg.click("#u-cancel")
    check(wait_texts(5)[-1]["to"] == "+15705550102", "Add user with a number texts them the opt-in request")
    pg.wait_for_selector(".users-table td:has-text('Night Shift')", timeout=5000)
    check(pg.evaluate(no_sideways), "still no sideways scrolling with two users")
    pg.screenshot(path=f"{out}/sms2-users.png")
    pg.set_viewport_size({"width": 1280, "height": 800})
    check(pg.evaluate(no_sideways), "no sideways scrolling at 1280 px")
    pg.set_viewport_size({"width": 1500, "height": 950})

    # --- 3. An alarm → a text with a link; alarm timing unaffected
    pg.goto(f"{PORTAL}/")
    pg.wait_for_timeout(1500)
    n0 = len(texts())
    ms = alarm_on_screen(pg, "line")
    t = wait_texts(n0 + 1)
    check(len(t) == n0 + 1, "the alarm sent one text")
    text = t[-1]
    print(f"      alarm on screen {ms:.0f} ms; text: {text['body']!r}")
    check(ms < 1000, f"alarm on screen in {ms:.0f} ms with texts on")
    lines = text["body"].split("\n")
    check(lines[0].startswith("TWG ALARM: ") and lines[1] == "Fake Test Site" and lines[2] == "Harrisburg, PA",
          "text: alarm, then site name, then address, one per line")
    check(any(l.startswith("Directions: https://www.google.com/maps/dir/?api=1&destination=40.26650,-76.88380") for l in lines),
          "text has a tap-to-navigate Directions link")
    check(lines[-1] == "Reply STOP to opt out.", "text ends with the opt-out line")
    link = next(l for l in lines if l.startswith("View alarm: ")).split(": ", 1)[1]
    check(text["media"].endswith("/alarm.jpg"), "text is an MMS with the alarm picture")
    pic = urllib.request.urlopen(text["media"])
    check(pic.status == 200 and pic.headers["content-type"] == "image/jpeg", f"picture loads for Twilio ({len(pic.read())} bytes)")

    # --- 4. The link on a phone, signed out
    phone = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    m = phone.new_page()
    m_errors = []
    m.on("pageerror", lambda e: m_errors.append(str(e)))
    t0 = time.time()
    r = m.goto(link)
    check(r.status == 200 and r.headers.get("cache-control") == "no-store" and r.headers.get("referrer-policy") == "no-referrer",
          "link page opens signed out, no-store, no-referrer")
    check("Line Detector" in m.inner_text("h1") and "Fake Test Site" in m.inner_text(".link-facts"), "shows the alarm and site")
    m.wait_for_selector(".player video:not([hidden])", timeout=30000)
    print(f"      clip playing {time.time() - t0:.1f} s after opening the link")
    check(m.evaluate("document.documentElement.scrollWidth <= innerWidth"), "no sideways scrolling at 390 px")
    m.screenshot(path=f"{out}/sms3-link-phone.png", full_page=True)
    m.click('[data-c="live"]')
    m.wait_for_function("(() => { const v = document.querySelector('.player-live video'); return v && v.readyState >= 2; })()", timeout=15000)
    check(True, "Live button plays the camera live on the link page")
    m.screenshot(path=f"{out}/sms4-link-live.png")
    check(m.locator("a:has-text('Sign in to acknowledge')").count() == 1, "offers Sign in to acknowledge")
    other = m.goto(f"{PORTAL}/api/alarms/1/clip")
    check(other.status == 401, "the link gives no access to the rest of the portal")
    bad = m.goto(link[:-3] + "zzz")
    check(bad.status == 404 and "doesn't work" in m.inner_text("h1"), "a mangled link shows 'This link doesn't work'")
    m.screenshot(path=f"{out}/sms5-bad-link.png")
    check(not m_errors, f"no script errors on the link page {m_errors}")
    r = m.goto(f"{PORTAL}/sms-terms")
    check(r.status == 200 and "Reply STOP" in m.inner_text("article") and "not sold" in m.inner_text("article"),
          "public text-message terms page, signed out")
    m.screenshot(path=f"{out}/sms7-terms.png", full_page=True)
    phone.close()

    # --- 5. Twilio rejects texts: alarms still instant, the failure shows loudly
    twilio_mode("fail")
    ms = alarm_on_screen(pg, "panic")
    check(ms < 1000, f"Twilio rejecting: alarm on screen in {ms:.0f} ms")
    pg.wait_for_selector("#critical-dialog[open]", timeout=5000)
    pg.click('#critical-dialog [data-verdict="dismissed"]')
    pg.wait_for_timeout(1500)
    pg.goto(f"{PORTAL}/platform#sms"); pg.click('#tabs [data-tab="sms"]')
    pg.wait_for_selector("#sms-status .alert-error", timeout=5000)
    check("30034" in pg.inner_text("#sms-status"), "Platform page shows Twilio's error")
    pg.screenshot(path=f"{out}/sms6-failed.png", full_page=True)
    pg.goto(f"{PORTAL}/users")
    pg.wait_for_selector(".text-failed", timeout=5000)
    check("FAILED" in pg.inner_text(".text-failed"), "Users page marks the person's last text FAILED")

    # --- 6. Twilio hangs: alarms still instant
    twilio_mode("hang")
    pg.goto(f"{PORTAL}/")
    pg.wait_for_timeout(1500)
    times = [alarm_on_screen(pg, "line") for _ in range(4)]
    print(f"      Twilio hanging: alarms on screen {[round(x) for x in times]} ms")
    check(max(times) < 1000, f"Twilio hanging: every alarm on screen in under 1 s (max {max(times):.0f} ms)")
    twilio_mode("ok")
    check(not errors, f"no script errors on the portal pages {errors}")
    b.close()

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

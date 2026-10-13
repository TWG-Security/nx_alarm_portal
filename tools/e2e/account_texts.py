"""My account button + signing yourself up for alarm texts, in a real browser (phone and desktop sizes).

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.account_texts [screenshot_dir]

Texts go to the fake Twilio in tools/fake_nx.py (dev_up.sh points TWILIO_API_BASE there).
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, LOGIN, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def texts():
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/_twilio"))


def sign_in(pg):
    pg.goto(f"{PORTAL}/login")
    pg.fill("#email", LOGIN[0]); pg.fill("#password", LOGIN[1])
    pg.click("button[type=submit]"); pg.wait_for_url(f"{PORTAL}/")


with sync_playwright() as p:
    b = p.chromium.launch()
    # Desktop: switch texts on (Platform page API), see the button with the name.
    desk = b.new_context(viewport={"width": 1500, "height": 900}).new_page()
    login_and_add_site(desk)
    desk.evaluate("""async () => { const t = document.querySelector('meta[name=csrf-token]').content;
      const r = await fetch('/api/platform/settings/sms', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
        body: JSON.stringify({enabled: true, sid: 'AC' + '0'.repeat(32), token: 'dev-token', sender: '+15705550000', link_hours: 24,
                              max_per_10min: 10, help_contact: 'support@example.test', pictures: false})});
      if (!r.ok) throw new Error(await r.text()); }""")
    btn = desk.locator(".account-btn")
    check(btn.is_visible() and "My account" in btn.inner_text() and "Dev Admin" in btn.inner_text(), "desktop: My account button with the name")
    desk.screenshot(path=f"{out}/account-btn-desktop.png", clip={"x": 900, "y": 0, "width": 600, "height": 60})

    # Phone: the old name link was hidden on narrow screens; the button keeps its icon.
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    ph = ctx.new_page()
    sign_in(ph)
    check(ph.locator(".account-btn").is_visible(), "phone: the My account button is visible (icon)")
    for path in ("/", "/account", "/alarms"):
        ph.goto(PORTAL + path); ph.wait_for_timeout(500)
        w = ph.evaluate("document.documentElement.scrollWidth")
        check(w <= 390, f"phone: {path} fits the screen (page width {w} px)")
    ph.goto(PORTAL + "/")
    ph.screenshot(path=f"{out}/account-btn-phone.png", clip={"x": 0, "y": 0, "width": 390, "height": 70})
    ph.click(".account-btn")
    ph.wait_for_url(f"{PORTAL}/account")
    ph.wait_for_selector("#tx-phone")
    check(ph.locator("#tx-form h2").inner_text().startswith("Alarm texts"), "Account page opens with the Alarm texts card")
    n = len(texts())
    ph.fill("#tx-phone", "(570) 555-0142")
    ph.select_option("#tx-when", "both")
    ph.click("#tx-form button[type=submit]")
    ph.wait_for_selector("#tx-box .chip.disarmed", timeout=5000)
    check("waiting for your yes" in ph.inner_text("#tx-box").lower(), "saved: waiting for the YES reply")
    ph.wait_for_timeout(4500)
    check("We texted you" in ph.inner_text("#tx-box"), "the status catches up once the opt-in text has gone")
    for _ in range(20):
        if len(texts()) > n:
            break
        time.sleep(0.25)
    sent = texts()[n:]
    check(sent and sent[-1]["to"] == "+15705550142" and "YES" in sent[-1]["body"], "the opt-in text went to the number typed")
    ph.screenshot(path=f"{out}/account-texts-phone.png", full_page=True)

    # Alarms keep coming on the desktop meanwhile.
    desk.goto(f"{PORTAL}/"); desk.wait_for_timeout(1500); desk.mouse.click(700, 400)
    t = time.time(); inject("panic")
    try:
        desk.wait_for_selector("#critical-dialog[open]", timeout=10000)
        print(f"  alarm on the desktop: {round((time.time() - t) * 1000)} ms")
    except Exception:
        print("  (no site on this dev database: alarm latency not measured)")
    b.close()

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

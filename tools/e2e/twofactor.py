"""Two-step sign-in, passkeys and sessions in a real browser.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.twofactor [screenshot_dir]

- the admin turns on the authenticator app on the Account page, then adds and tests a passkey
  (Chrome's virtual authenticator), signs out and back in with the passkey alone
- with 2FA required, a new operator is walked through enrolment at sign-in
- a "wall screen" signed in elsewhere is signed out remotely: it must show SIGNED OUT and sound,
  not quietly sit there or drop to a login page
"""
import sys
import time

import pyotp
from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, LOGIN, PORTAL, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def call(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json().catch(() => null)}; }""", [method, path, body])


def code(secret, ahead=0):
    return pyotp.TOTP(secret).at(time.time() + ahead)


def password_in(pg, email, password):
    pg.goto(f"{PORTAL}/login")
    pg.fill("#email", email); pg.fill("#password", password)
    pg.click("button[type=submit]")
    pg.wait_for_load_state()


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1400, "height": 950})
    pg = ctx.new_page()
    cdp = ctx.new_cdp_session(pg)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "automaticPresenceSimulation": True}})
    login_and_add_site(pg)

    # ---- authenticator app
    pg.goto(f"{PORTAL}/account")
    pg.click("#totp-on")
    pg.wait_for_selector("#totp-dialog[open]")
    secret = pg.inner_text("#totp-secret").strip()
    check(pg.get_attribute("#totp-qr", "src").startswith("data:image/png"), "QR code shown for the authenticator app")
    pg.screenshot(path=f"{out}/account-totp-setup.png")
    pg.fill("#totp-code", code(secret)); pg.click("#totp-form button[type=submit]")
    pg.wait_for_selector("#codes-dialog[open]")
    codes = pg.locator("#codes-list li").all_inner_texts()
    check(len(codes) == 10, "10 recovery codes shown once")
    pg.click("#codes-done")
    pg.wait_for_selector("#mfa-box .chip.online")
    check("10 recovery codes left" in pg.inner_text("#mfa-box"), "Account page: two-step sign-in on, 10 codes left")

    # ---- passkey
    pg.fill("#pk-name", "E2E virtual key")
    pg.click("#pk-add button[type=submit]")
    pg.wait_for_selector("#pk-box .chip.online", timeout=10000)
    check("E2E virtual key" in pg.inner_text("#pk-box"), "passkey added and tested ('Works')")
    pg.screenshot(path=f"{out}/account.png", full_page=True)

    # ---- sign out, back in with the passkey alone
    pg.click("form[action='/logout'] button")
    pg.wait_for_url(f"{PORTAL}/login")
    check(pg.locator("#passkey-login").is_visible(), "sign-in page offers 'Sign in with a passkey' on localhost")
    t = time.time()
    pg.click("#passkey-login")
    pg.wait_for_url(f"{PORTAL}/", timeout=10000)
    check(True, f"signed in with the passkey alone in {round((time.time() - t) * 1000)} ms (no password, no code)")

    # ---- password + code, and the 2FA page
    pg.click("form[action='/logout'] button")
    password_in(pg, *LOGIN)
    check(pg.url.endswith("/login/2fa"), "password sign-in now asks for the second step")
    pg.screenshot(path=f"{out}/login-2fa.png")
    pg.fill("#code", code(secret, 30)); pg.click("button[type=submit]")
    pg.wait_for_url(f"{PORTAL}/")
    check(True, "code from the app accepted")

    # ---- required 2FA: a new operator enrols at sign-in
    r = call(pg, "POST", "/api/users", {"email": "night@twgsecurity.com", "display_name": "Night Op",
                                        "password": "Night!shift-2026", "role": "operator"})
    check(r["status"] == 200, "operator created")
    r = call(pg, "PUT", "/api/platform/settings/two-factor", {"require_twg": True, "customers": "company", "passkeys_enabled": True})
    check(r["status"] == 200, "Platform: 2FA required for TWG")

    wall_ctx = b.new_context(viewport={"width": 1400, "height": 900})
    wall_ctx.add_init_script(COUNT_TONES)
    wall = wall_ctx.new_page()
    password_in(wall, "night@twgsecurity.com", "Night!shift-2026")
    check(wall.url.endswith("/login/enroll"), "the operator is sent to set up two-step sign-in")
    wall.screenshot(path=f"{out}/login-enroll.png")
    s2 = wall.inner_text("#totp-secret").strip()
    wall.fill("#code", code(s2)); wall.click("button[type=submit]")
    wall.wait_for_selector("#recovery-codes li")
    check(wall.locator("#recovery-codes li").count() == 10, "recovery codes shown after enrolment")
    wall.click("text=I've saved them, continue")
    wall.wait_for_url(f"{PORTAL}/")
    wall.mouse.click(700, 500)                                   # sound needs a user gesture
    wall.wait_for_selector("#live-status.ok", timeout=15000)
    check(True, "operator's wall screen is live")

    # ---- the admin signs the wall screen out remotely
    users = call(pg, "GET", "/api/users")["body"]
    night = next(u for u in users if u["email"] == "night@twgsecurity.com")
    osc = wall.evaluate("window.__osc")
    t = time.time()
    r = call(pg, "DELETE", f"/api/users/{night['id']}/sessions")
    check(r["status"] == 200 and r["body"]["ended"] == 1, "admin: 'Sign them out everywhere'")
    wall.wait_for_selector("#signedout-banner:not([hidden])", timeout=15000)
    secs = time.time() - t
    check(secs < 7, f"wall screen shows SIGNED OUT {secs:.1f} s after (live stream told it)")
    check(wall.inner_text("#live-status") == "Signed out", "top bar says 'Signed out', not 'Live'")
    check(wall.url == f"{PORTAL}/", "it stays on the page (no quiet jump to a login form)")
    wall.wait_for_function(f"window.__osc > {osc}", timeout=5000)
    check(True, "and it sounds the connection-lost tone")
    wall.screenshot(path=f"{out}/signed-out-banner.png")
    wall.click("#signedout-banner a")
    wall.wait_for_url("**/login?**")
    check("You were signed out" in wall.content(), "'Sign in again' opens sign-in with an explanation")

    call(pg, "PUT", "/api/platform/settings/two-factor", {"require_twg": False, "customers": "company", "passkeys_enabled": True})
    b.close()

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

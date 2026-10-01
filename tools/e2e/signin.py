"""Sign-in protection in a real browser: an attacker gets banned, the operator's alarm screen keeps
working throughout, and TWG unblocks the address from the Platform page.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.signin [screenshot_dir]

The dev portal sees every browser as 127.0.0.1 (a trusted proxy), so the attacker's browser sends a
CF-Connecting-IP header, exactly as the Cloudflare Tunnel does in production.
"""
import sys
import time

from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
ATTACKER = "203.0.113.50"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def try_login(pg, email, password):
    pg.goto(f"{PORTAL}/login")
    if pg.locator("#email").count() == 0:
        return
    pg.fill("#email", email); pg.fill("#password", password)
    pg.click("button[type=submit]")
    pg.wait_for_load_state()


with sync_playwright() as p:
    b = p.chromium.launch()
    ops = b.new_context(viewport={"width": 1500, "height": 950})
    ops.add_init_script(COUNT_TONES)
    op = ops.new_page()
    login_and_add_site(op)                                  # the operator's monitoring screen (LAN, 127.0.0.1)
    op.wait_for_selector("#live-status.ok", timeout=15000)

    bad = b.new_context(extra_http_headers={"CF-Connecting-IP": ATTACKER})
    atk = bad.new_page()
    for i in range(4):
        try_login(atk, "admin@twgsecurity.com", f"guess-{i}")
        check("Invalid email or password" in atk.content(), f"attempt {i + 1}: generic 'invalid' answer")
    try_login(atk, "admin@twgsecurity.com", "guess-5")
    check(atk.locator("h1", has_text="Temporarily blocked").count() == 1 and ATTACKER in atk.content(),
          "5th failed attempt: blocked page with the attacker's address")
    atk.screenshot(path=f"{out}/signin-blocked.png")
    atk.goto(f"{PORTAL}/login")
    check(atk.locator("#email").count() == 0, "a reload of the sign-in page stays blocked")
    try_login(atk, "admin@twgsecurity.com", "smoke-test-password-123")
    check(atk.locator("#email").count() == 0 and "/login" in atk.url, "the right password from the banned address is refused too")

    # The operator's screen is unaffected: a critical alarm still pops up straight away.
    check(op.locator("#live-status.ok").count() == 1, "operator's live updates still connected")
    t = time.time(); inject("panic")
    op.wait_for_selector("#critical-dialog[open]", timeout=10000)
    check(True, f"critical alarm popped up on the operator's screen in {round((time.time() - t) * 1000)} ms after the ban")
    op.click('#critical-dialog [data-verdict="false"]')
    op.wait_for_selector("#critical-dialog[open]", state="detached", timeout=5000) if op.locator("#critical-dialog[open]").count() else None

    # TWG unblocks it on the Platform page.
    plat = ops.new_page()
    plat.goto(f"{PORTAL}/platform")
    plat.wait_for_selector(f"#ban-rows td:has-text('{ATTACKER}')", timeout=5000)
    check("5 failed login attempts" in plat.inner_text("#ban-rows"), "Platform page lists the ban and why")
    check("can't be banned" in plat.inner_text("#my-ip") and "127.0.0.1" in plat.inner_text("#my-ip"),
          "Platform page: your own LAN address can't be banned")
    rows = plat.inner_text("#ev-rows")
    check(rows.count("Failed") >= 5 and ATTACKER in rows, "recent attempts list the failures")
    plat.screenshot(path=f"{out}/platform-signin.png", full_page=True)
    plat.on("dialog", lambda d: d.accept())
    plat.click(f"[data-ip='{ATTACKER}'][data-unban]")
    plat.wait_for_selector("#ban-rows td.empty", timeout=5000)
    check(True, "unblocked from the Platform page")
    atk.goto(f"{PORTAL}/login")
    check(atk.locator("#email").count() == 1, "the address can reach the sign-in page again")
    try_login(atk, "admin@twgsecurity.com", "still-wrong")
    check("Invalid email or password" in atk.content(), "older failures were forgiven (one more doesn't re-ban)")

    plat.click("#tabs [data-tab=cloudflare]")
    plat.screenshot(path=f"{out}/platform-cloudflare.png", full_page=True)
    plat.evaluate("document.documentElement.dataset.theme = 'light'")
    plat.click("#tabs [data-tab=signin]")
    plat.screenshot(path=f"{out}/platform-signin-light.png", full_page=True)
    b.close()

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

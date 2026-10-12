"""Email, invites and forgot-password in a real browser, with a local SMTP sink catching the mail.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.email [screenshot_dir]

- Platform → Email: point at the sink, send a test
- Users: invite someone; they open the emailed link, set a password (the rules are checked) and are in
- Forgot password: the emailed link sets a new password once; the old one stops working
"""
import email as email_lib
import re
import sys
import time

from aiosmtpd.controller import Controller
from playwright.sync_api import sync_playwright

from tools.e2e.common import LOGIN, PORTAL

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
inbox = []


class Sink:
    async def handle_DATA(self, server, session, envelope):
        inbox.append(email_lib.message_from_bytes(envelope.content))
        return "250 OK"


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def wait_mail(n, timeout=15):
    end = time.time() + timeout
    while len(inbox) < n and time.time() < end:
        time.sleep(0.1)
    return len(inbox) >= n


def html_of(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode()


def link(msg, path):
    return re.search(rf"{re.escape(PORTAL)}{path}\?token=[A-Za-z0-9_\-]+", html_of(msg)).group(0)


sink = Controller(Sink(), hostname="127.0.0.1", port=8125)
sink.start()
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        pg.goto(f"{PORTAL}/login")
        pg.fill("#email", LOGIN[0]); pg.fill("#password", LOGIN[1]); pg.click("button[type=submit]")
        pg.wait_for_url(f"{PORTAL}/")

        # ---- Platform → Email
        pg.goto(f"{PORTAL}/platform#email")
        pg.check("#m-enabled")
        pg.fill("#m-host", "127.0.0.1"); pg.select_option("#m-tls", "none"); pg.fill("#m-port", "8125")
        pg.fill("#m-from", "TWG Alarm Portal <alerts@twgsecurity.com>"); pg.fill("#m-url", PORTAL)
        pg.click("#mail-form button[type=submit]")
        pg.wait_for_selector("#mail-status .alert-warn", state="detached", timeout=5000)
        pg.fill("#m-test-to", "boss@twgsecurity.com"); pg.click("#m-test")
        check(wait_mail(1) and inbox[0]["Subject"] == "Alarm portal test email", "test email arrives at the sink")
        pg.wait_for_selector("#mail-log .chip.online", timeout=5000)
        check(True, "Platform page lists it as Sent")

        # ---- invite on the Users page
        pg.goto(f"{PORTAL}/users")
        pg.fill("#u-email", "newbie@twgsecurity.com"); pg.fill("#u-name", "New Operator")
        pg.click("#user-form button[type=submit]")
        pg.wait_for_selector("#u-link .alert-ok")
        check("invite email is on its way" in pg.inner_text("#u-link"), "Users page shows the setup link and that the email is going")
        check(wait_mail(2) and inbox[1]["To"] == "newbie@twgsecurity.com", "invite email arrives")
        html = html_of(inbox[1])
        check("border:5px solid #1A1A1A" in html and "background:#E08A30" in html, "invite email carries TWG branding")
        shot = b.new_page(viewport={"width": 700, "height": 900})
        shot.set_content(html); shot.screenshot(path=f"{out}/email-invite.png", full_page=True); shot.close()
        pg.wait_for_selector("tr:has-text('newbie@twgsecurity.com') .chip.disarmed")
        pg.wait_for_selector("tr:has-text('newbie@twgsecurity.com') :text('email sent')", timeout=8000)
        check(True, "Users list: Invited, email sent (refreshes by itself)")

        new = b.new_context().new_page()
        new.goto(link(inbox[1], "/setup"))
        check("newbie@twgsecurity.com" in new.inner_text(".login-card"), "setup page greets them by email and company")
        new.fill("#password", "password1234"); new.fill("#password2", "password1234"); new.click("button[type=submit]")
        check("needs an uppercase letter" in new.inner_text(".alert-error"), "a weak password is refused and says why")
        new.screenshot(path=f"{out}/setup-weak.png")
        new.fill("#password", "Brand-New-2026!"); new.fill("#password2", "Brand-New-2026!"); new.click("button[type=submit]")
        new.wait_for_url(f"{PORTAL}/")
        check(True, "account set up and signed in")
        new.goto(link(inbox[1], "/setup"))
        check("already used" in new.inner_text(".login-card"), "the invite link works only once")

        # ---- forgot password
        fp = b.new_context().new_page()
        fp.goto(f"{PORTAL}/login"); fp.click("text=Forgot password?")
        fp.fill("#email", "newbie@twgsecurity.com"); fp.click("button[type=submit]")
        check("we've emailed it a link" in fp.inner_text("#forgot-sent"), "forgot password: neutral confirmation")
        check(wait_mail(3) and "Reset" in inbox[2]["Subject"], "reset email arrives")
        fp.goto(link(inbox[2], "/reset"))
        fp.fill("#password", "Second-Try-2026!"); fp.fill("#password2", "Second-Try-2026!"); fp.click("button[type=submit]")
        fp.wait_for_url("**/login?reason=reset")
        check("password was changed" in fp.inner_text(".login-card"), "after the reset: sign in with the new password")
        fp.fill("#email", "newbie@twgsecurity.com"); fp.fill("#password", "Brand-New-2026!"); fp.click("button[type=submit]")
        check("Invalid email or password" in fp.inner_text(".login-card"), "the old password no longer works")
        fp.fill("#email", "newbie@twgsecurity.com"); fp.fill("#password", "Second-Try-2026!"); fp.click("button[type=submit]")
        fp.wait_for_url(f"{PORTAL}/")
        check(True, "the new one does")
        new.goto(f"{PORTAL}/")
        check("/login" in new.url, "the browser signed in before the reset was signed out")

        pg.goto(f"{PORTAL}/platform#email")
        pg.wait_for_selector("#mail-log tr")
        pg.screenshot(path=f"{out}/platform-email.png", full_page=True)
        b.close()
finally:
    sink.stop()

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

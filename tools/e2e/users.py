"""Deleting a user, in a real browser.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.users [screenshot_dir]

An operator is signed in on a second screen. The admin deletes them from the Users page (Edit → Delete user):
the row goes, the operator's screen is signed out, the audit log shows "User deleted", the same email can be
added again, and an alarm right after still reaches the admin's map (timed).
"""
import sys
import time

from playwright.sync_api import sync_playwright

from tools.e2e.common import LOGIN, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
OP = ("night.guard@twgsecurity.com", "Night-guard-password-1!")


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def api(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json().catch(() => null)}; }""", [method, path, body])


with sync_playwright() as p:
    b = p.chromium.launch()
    admin = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
    login_and_add_site(admin)
    r = api(admin, "POST", "/api/users", {"email": OP[0], "display_name": "Night Guard", "password": OP[1]})
    check(r["status"] == 200, f"operator created ({r['status']})")

    op = b.new_context(viewport={"width": 1200, "height": 800}).new_page()
    op.goto(f"{PORTAL}/login")
    op.fill("#email", OP[0]); op.fill("#password", OP[1])
    op.click("button[type=submit]"); op.wait_for_url(f"{PORTAL}/")
    check(True, "operator signed in on a second screen")

    admin.goto(f"{PORTAL}/users")
    admin.wait_for_selector(f"text={OP[0]}")
    row = admin.locator("#user-rows tr", has_text=OP[0])
    row.locator("[data-edit]").click()
    btn = admin.locator("#edit-dialog [data-delete]")
    check(btn.is_visible(), "Edit dialog has a Delete user button")
    admin.screenshot(path=f"{out}/users-delete-button.png")

    prompts = []
    admin.once("dialog", lambda d: (prompts.append(d.message), d.dismiss()))
    btn.click()
    admin.wait_for_timeout(300)
    check(prompts and "can't be undone" in prompts[0], "asks to confirm first")
    check(admin.locator("#user-rows tr", has_text=OP[0]).count() == 1, "Cancel keeps the user")

    admin.once("dialog", lambda d: d.accept())
    btn.click()
    admin.wait_for_selector(f"#user-rows tr:has-text('{OP[0]}')", state="detached", timeout=5000)
    check(not admin.locator("#edit-dialog").evaluate("d => d.open"), "dialog closes")
    check(admin.locator("#user-rows tr", has_text=OP[0]).count() == 0, "row is gone from Users")
    admin.screenshot(path=f"{out}/users-deleted.png")

    # Their screen: the live stream is told they're signed out (or the next call gets 401).
    try:
        op.wait_for_url("**/login**", timeout=8000)
        check(True, "deleted operator's screen went to the sign-in page")
    except Exception:
        st = api(op, "GET", "/api/alarms?state=all")["status"]
        check(st == 401, f"deleted operator's screen is signed out (API {st})")

    op.goto(f"{PORTAL}/login")
    op.fill("#email", OP[0]); op.fill("#password", OP[1]); op.click("button[type=submit]")
    op.wait_for_timeout(800)
    check("/login" in op.url, "their password no longer signs in")

    admin.goto(f"{PORTAL}/audit")
    admin.wait_for_selector("text=User deleted", timeout=5000)
    check(admin.locator("text=User deleted").count() >= 1, "audit log shows User deleted")

    r = api(admin, "POST", "/api/users", {"email": OP[0], "password": OP[1]})
    check(r["status"] == 200, f"same email can be added again ({r['status']})")

    # Alarms keep flowing right after a delete (session revoke + stream sign-out shouldn't touch them).
    admin.goto(f"{PORTAL}/")
    admin.wait_for_timeout(1500)
    t0 = time.monotonic()
    inject("panic")
    admin.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = int((time.monotonic() - t0) * 1000)
    check(ms < 1000, f"critical alarm popped up on the admin's screen {ms} ms after injection")
    b.close()

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

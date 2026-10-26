"""Client profile: editor, Client card in the site panel, package on the feed, Response box + logged calls.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.profile
Screenshots go to E2E_SHOTS (default: the current directory).
"""
import os

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, inject, login_and_add_site

SHOTS = os.environ.get("E2E_SHOTS", ".")
ok = True


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + what)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1920, "height": 1080}).new_page()
    login_and_add_site(pg); pg.wait_for_timeout(2000)
    site_id = pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].id)")
    pg.locator(f".site-item[data-id='{site_id}']").click()
    pg.click("[data-profile-edit]")
    pg.wait_for_selector(".profile-dialog[open]")
    pg.check("input[name=pf-package][value=ground_response]")
    pg.fill("#pf-name", "Jordan Smith"); pg.fill("#pf-phone", "416-555-0100"); pg.fill("#pf-police", "416-808-2222")
    pg.fill("#pf-pass", "bluebird"); pg.fill("#pf-notes", "Dog in the back yard. Gate code 4411.")
    pg.click("[data-pf-add]")
    pg.fill(".pf-contact [data-c=name]", "Ann Smith"); pg.fill(".pf-contact [data-c=role]", "spouse"); pg.fill(".pf-contact [data-c=phone]", "+1 416 555 0101")
    pg.fill("#pf-ajax", "https://app.ajax.systems/")
    pg.screenshot(path=f"{SHOTS}/profile-1-editor.png")
    pg.click(".pf-form button[type=submit]")
    pg.wait_for_selector(".profile-dialog:not([open])", state="attached")
    pg.wait_for_selector("#site-detail .client-card .pkg--ground_response", timeout=5000)
    card = pg.inner_text("#site-detail .client-card")
    check("Jordan Smith" in card and "416-555-0100" in card and "Ann Smith" in card and "bluebird" not in card,
          "Client card: name, phone, contact; passcode hidden until Show")
    pg.click("#site-detail [data-reveal]")
    check("bluebird" in pg.inner_text("#site-detail .client-card"), "Show reveals the passcode")
    side = pg.locator(".map-side").bounding_box()["width"]
    check(side >= 370, f"site column widens for the profile ({side:.0f} px)")
    pg.screenshot(path=f"{SHOTS}/profile-2-site-panel.png")
    pg.fill("#site-search", "jordan")
    check(pg.locator(".site-item").count() == 1, "site search finds the client's name")
    pg.fill("#site-search", "nobody-here")
    check(pg.locator(".site-item").count() == 0 and "No site matches" in pg.inner_text("#site-list"), "and says when nothing matches")
    pg.fill("#site-search", "")

    n = pg.locator(".feed-item.p2").count(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
    check(pg.locator(".feed-item .pkg--ground_response").count() >= 1, "feed card shows the package")
    pg.locator(".feed-item.p2 [data-detail]").first.click()
    pg.wait_for_selector(".response-box .btn-call--police", timeout=10000)
    resp = pg.inner_text(".response-box")
    check("416-808-2222" in resp and "Jordan Smith" in resp and "Ann Smith" in resp and "Open in Ajax" in resp,
          "Response box: police (local number), client, contact, Ajax")
    check(pg.get_attribute(".response-box .btn-call--police", "href") == "tel:4168082222", "Call police is a tel: link to the local number")
    pg.screenshot(path=f"{SHOTS}/profile-3-response.png")
    pg.evaluate("document.querySelector('.response-box .btn-call--police').addEventListener('click', e => e.preventDefault(), {once: true})")
    pg.click(".response-box .btn-call--police")
    pg.wait_for_selector(".drawer-timeline :text('Called the police')", timeout=8000)
    check(True, "the call is on the alarm's timeline")
    b.close()

print("ALL PASS" if ok else "SOME CHECKS FAILED")

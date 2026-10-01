"""Multi-company end to end: two companies with their own (fake) NX servers, in real browsers.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.tenancy [screenshot_dir]

Starts a second fake NX on :8198 for the customer company. Checks isolation, branding, the All
companies view, support mode, who hears which alarm, the audit trail, and disabling sign-in.
"""
import io
import subprocess
import sys
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
ACME_NX = "http://127.0.0.1:8198"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def inject_acme(kind):
    urllib.request.urlopen(urllib.request.Request(f"{ACME_NX}/_inject/{kind}", method="POST"))


def call(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json().catch(() => null)}; }""", [method, path, body])


def sign_in(ctx, email, password):
    pg = ctx.new_page()
    pg.goto(f"{PORTAL}/login")
    pg.fill("#email", email); pg.fill("#password", password)
    pg.click("button[type=submit]")
    return pg


nx2 = subprocess.Popen([".venv/bin/uvicorn", "tools.fake_nx:app", "--port", "8198"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{ACME_NX}/rest/v4/site/info", timeout=1); break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    with sync_playwright() as p:
        b = p.chromium.launch()
        twg_ctx = b.new_context(viewport={"width": 1500, "height": 950}); twg_ctx.add_init_script(COUNT_TONES)
        twg = twg_ctx.new_page()
        errors = []
        twg.on("pageerror", lambda e: errors.append(f"twg: {e}"))
        login_and_add_site(twg)                                                 # TWG's own site (fake NX :8199)
        r = call(twg, "POST", "/api/tenants", {"name": "Acme Security", "display_name": "Acme",
                                               "admin_email": "admin@acme.test", "admin_password": "acme-password-123"})
        acme_id = r["body"]["id"]
        call(twg, "POST", "/api/scope", {"scope": str(acme_id)})
        r = call(twg, "POST", "/api/sites", {"name": "Acme Warehouse", "host": ACME_NX, "nx_user": "portal", "nx_pass": "x",
                                             "address": "Allentown, PA", "lat": 40.6084, "lng": -75.4902})
        check(r["status"] == 200 and r["body"]["tenant_id"] == acme_id, "TWG support created a site inside Acme")
        buf = io.BytesIO(); Image.new("RGB", (480, 120), (20, 70, 160)).save(buf, "PNG")
        twg.goto(f"{PORTAL}/companies")
        twg.wait_for_selector(f'[data-edit="{acme_id}"]')
        twg.click(f'[data-edit="{acme_id}"]')
        twg.set_input_files("#e-file", files=[{"name": "acme.png", "mimeType": "image/png", "buffer": buf.getvalue()}])
        twg.wait_for_selector("#e-logo:not([hidden])", timeout=5000)
        twg.click("#e-cancel")
        call(twg, "POST", "/api/scope", {"scope": "own"})
        twg.goto(f"{PORTAL}/"); twg.mouse.click(700, 500)
        twg.wait_for_timeout(2500)                                               # both sites' push connections

        # --- 1. Acme's own portal: only Acme, Acme branding, its alarms sound
        acme_ctx = b.new_context(viewport={"width": 1400, "height": 900}); acme_ctx.add_init_script(COUNT_TONES)
        acme = sign_in(acme_ctx, "admin@acme.test", "acme-password-123")
        acme.on("pageerror", lambda e: errors.append(f"acme: {e}"))
        acme.wait_for_selector(".site-item"); acme.mouse.click(700, 500)
        check([x.inner_text() for x in acme.locator(".site-item .name").all()] == ["Acme Warehouse"], "Acme sees only its own site")
        check(acme.locator('.brand img[src*="/branding/"]').count() == 1 and "Powered by TWG Security" in acme.inner_text(".brand"),
              "Acme's top bar shows its logo and 'Powered by TWG Security'")
        check(acme.locator('a[href="/companies"]').count() == 0 and acme.locator("#scope-switch").count() == 0,
              "Acme has no Companies page or company switcher")
        acme.screenshot(path=f"{out}/t1-acme.png")
        osc_twg, osc_acme = twg.evaluate("window.__osc"), acme.evaluate("window.__osc")
        t = time.time(); inject_acme("line")
        acme.wait_for_selector(".feed-item.p2", timeout=10000)
        check(True, f"Acme's alarm on Acme's screen in {round((time.time() - t) * 1000)} ms")
        acme.wait_for_timeout(1200)
        check(acme.evaluate("window.__osc") > osc_acme, "…and it sounded for Acme")
        check(twg.locator(".feed-item").count() == 0 and twg.evaluate("window.__osc") == osc_twg,
              "TWG (own view) neither sees nor hears Acme's alarm")

        # --- 2. TWG, All companies: sees both, hears only its own
        twg.select_option("#scope-switch", "all")
        twg.wait_for_function("document.querySelector('.support-banner')?.innerText.includes('All companies')", timeout=10000)
        twg.mouse.click(700, 500)
        twg.wait_for_function("document.querySelectorAll('.site-item').length === 2", timeout=5000)
        check(twg.locator(".site-item .tenant-chip").count() == 2, "All companies: both sites, each labelled with its company")
        check(twg.locator(".feed-item .tenant-chip").count() >= 1, "Acme's open alarm in TWG's feed, labelled Acme")
        twg.wait_for_timeout(1500)
        osc = twg.evaluate("window.__osc")
        inject_acme("panic")                                                     # an Acme CRITICAL
        twg.wait_for_function("document.querySelectorAll('.feed-item.p1').length >= 1", timeout=10000)
        twg.wait_for_timeout(1500)
        check(twg.locator("#critical-dialog[open]").count() == 0 and twg.evaluate("window.__osc") == osc,
              "Acme's critical: shown in TWG's feed, no pop-up, no siren on TWG's screen")
        twg.screenshot(path=f"{out}/t2-all.png")

        # --- 3. TWG supporting Acme: TWG's own critical still pops up and sounds
        twg.select_option("#scope-switch", str(acme_id))
        twg.wait_for_function("document.querySelector('.support-banner')?.innerText.includes('Support mode')", timeout=10000)
        twg.wait_for_selector(".site-item", timeout=5000)
        twg.mouse.click(700, 500)
        check("Support mode: Acme" in twg.inner_text(".support-banner"), "support banner: " + twg.inner_text(".support-banner")[:60])
        check([x.inner_text() for x in twg.locator(".site-item .name").all()] == ["Acme Warehouse"], "support mode shows Acme only")
        osc = twg.evaluate("window.__osc")
        t = time.time(); inject("panic")                                         # TWG's own critical
        twg.wait_for_selector("#critical-dialog[open]", timeout=10000)
        check(True, f"TWG's own critical popped up in {round((time.time() - t) * 1000)} ms while viewing Acme")
        twg.wait_for_timeout(1200)
        check(twg.evaluate("window.__osc") > osc, "…and the siren played")
        twg.click('#critical-dialog [data-verdict="false"]')
        # Acknowledge Acme's open line-crossing alarm as support.
        twg.wait_for_selector(".feed-item.p2", timeout=5000)
        aid = int(twg.get_attribute(".feed-item.p2", "data-id"))
        r = call(twg, "POST", f"/api/alarms/{aid}/ack", {"verdict": "real", "note": "Checked by TWG support"})
        check(r["status"] == 200, "TWG support acknowledged Acme's alarm")

        # --- 4. Acme sees TWG's support in its own audit log
        acme.goto(f"{PORTAL}/audit"); acme.wait_for_selector("tbody tr")
        audit_text = acme.inner_text("tbody")
        check("TWG support" in audit_text and "TWG support opened this company" in audit_text,
              "Acme's audit log shows TWG's visit and actions, marked TWG support")

        # --- 5. Companies page, then turn Acme's sign-in off
        twg.goto(f"{PORTAL}/companies"); twg.wait_for_selector("#company-rows [data-open]")
        rows = twg.inner_text("#company-rows")
        check("Acme" in rows and "TWG Security" in rows, "Companies page lists both companies with their numbers")
        twg.screenshot(path=f"{out}/t3-companies.png")
        r = call(twg, "PUT", f"/api/tenants/{acme_id}", {"is_active": False})
        acme.goto(f"{PORTAL}/")
        check("/login" in acme.url, "Acme's sign-in turned off: their session is cut off")
        acme.fill("#email", "admin@acme.test"); acme.fill("#password", "acme-password-123"); acme.click("button[type=submit]")
        acme.wait_for_selector(".alert-error")
        check("disabled" in acme.inner_text(".alert-error"), "…and signing in again explains why")
        call(twg, "PUT", f"/api/tenants/{acme_id}", {"is_active": True})
        check(not errors, f"no JavaScript errors {errors[:3]}")
        b.close()
finally:
    nx2.terminate()

print(f"\n{len(fails)} failure(s)" if fails else "\nall multi-company checks passed")
raise SystemExit(1 if fails else 0)

"""Verdicts, live-beside-recorded drawer, groups and bulk edit, in a real browser.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.verdicts [screenshot_dir]
"""
import sys
import time

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, inject, login_and_add_site

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


def sign_in(ctx, email, password):
    pg = ctx.new_page()
    pg.goto(f"{PORTAL}/login")
    pg.fill("#email", email); pg.fill("#password", password)
    pg.click("button[type=submit]"); pg.wait_for_url(f"{PORTAL}/")
    return pg


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)

    # --- 1. drawer: recorded + live side by side, live video actually playing
    inject("line")
    pg.wait_for_selector(".feed-item.p2", timeout=10000)
    t = time.time()
    pg.click(".feed-item.p2 [data-detail]")
    pg.wait_for_selector(".drawer-media .live-mount video", timeout=5000)
    pg.wait_for_function("document.querySelector('.live-mount video').readyState >= 2", timeout=15000)
    first = time.time() - t
    t0 = pg.evaluate("document.querySelector('.live-mount video').currentTime")
    pg.wait_for_timeout(2000)
    t1 = pg.evaluate("document.querySelector('.live-mount video').currentTime")
    check(t1 - t0 > 1.5, f"live video playing beside the clip: first frame {first:.1f} s after opening, advanced {t1 - t0:.1f} s in 2 s")
    check(pg.locator(".drawer-media .player-mount video, .drawer-media .player-mount .player-msg").count() > 0
          and pg.locator('.drawer-media [data-c="live"]').count() == 0, "recorded clip on the left, no duplicate Live toggle")
    pg.wait_for_timeout(4000)                                            # let the clip load for the screenshot
    pg.screenshot(path=f"{out}/v1-drawer.png")

    # --- 2. alarm latency while live video streams
    n = pg.locator(".feed-item.p3").count()
    t = time.time(); inject("dock")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p3').length > {n}", timeout=10000)
    check(True, f"alarm while live video streams: on screen in {round((time.time() - t) * 1000)} ms")

    # --- 3. acknowledge from the drawer as a real event; stream stops on close
    pg.click('.drawer [data-ack="real"]')
    pg.wait_for_selector(".drawer .chip.verdict-real", timeout=5000)
    check(True, "drawer: Real event acknowledges and shows the verdict")
    pg.click("#drawer-close")
    pg.wait_for_timeout(300)
    check(pg.locator(".live-mount video").count() == 0, "closing the drawer stops the live stream")

    # --- 4. quick ack dialog from the feed: False alarm
    pg.click(".feed-item.p3 [data-ack]")
    pg.wait_for_selector("#ack-dialog[open]")
    check(pg.locator("#ack-dialog #ack-real").is_visible() and pg.locator("#ack-dialog #ack-false").is_visible(),
          "quick ack dialog offers Real event / False alarm")
    pg.fill("#ack-note", "Camera blip")
    pg.click("#ack-false")
    pg.wait_for_function("!document.querySelector('.feed-item.p3')", timeout=5000)

    # --- 5. critical pop-up: two verdict buttons, live via its toggle
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    check(pg.locator('#critical-dialog [data-verdict="real"]').count() == 1 and
          pg.locator('#critical-dialog [data-verdict="false"]').count() == 1, "critical pop-up offers both verdicts")
    pg.click('#critical-dialog [data-c="live"]')
    pg.wait_for_function("document.querySelector('#critical-dialog .player-live video')?.readyState >= 2", timeout=15000)
    check(True, "critical pop-up Live button plays live video")
    pg.click('#critical-dialog [data-verdict="false"]')
    pg.wait_for_selector("#critical-dialog[open]", state="detached", timeout=5000)

    # --- 6. groups: an operator gets bulk edit only through a group
    r = call(pg, "POST", "/api/users", {"email": "op@twgsecurity.com", "display_name": "Night Operator",
                                        "password": "operator-pass-1234", "role": "operator"})
    op_id = r["body"]["id"]
    for kind in ("line", "line", "line"):
        inject(kind)
    pg.wait_for_timeout(1500)
    ctx2 = b.new_context(viewport={"width": 1400, "height": 900})
    op = sign_in(ctx2, "op@twgsecurity.com", "operator-pass-1234")
    op.goto(f"{PORTAL}/alarms?state=all")
    op.wait_for_selector(".alarm", timeout=5000)
    check(op.locator("#bulk-bar:not([hidden])").count() == 0 and op.locator(".alarm .sel").count() == 0,
          "operator without the permission: no bulk edit")
    pg.goto(f"{PORTAL}/users")
    pg.click("#g-new")
    pg.fill("#g-name", "Supervisors")
    pg.check('#g-perms input[value="alarms.bulk_edit"]')
    pg.check(f'#g-members input[value="{op_id}"]')
    pg.click("#group-form button[type=submit]")
    pg.wait_for_selector(".group-row", timeout=5000)
    check("Night Operator" in pg.inner_text(".group-row"), "group created on the Users page with its member")
    pg.screenshot(path=f"{out}/v2-groups.png", full_page=True)
    op.reload()
    op.wait_for_selector("#bulk-bar:not([hidden])", timeout=5000)
    op.check("#bulk-all")
    count = op.locator(".alarm .sel:checked").count()
    op.click('[data-bulk="false"]')
    op.wait_for_selector("#bulk-dialog[open]")
    summary = op.inner_text("#bulk-summary")
    op.screenshot(path=f"{out}/v3-bulk.png")
    op.fill("#bulk-note", "Storm review")
    op.click("#bulk-apply")
    op.wait_for_selector(".toast", timeout=10000)
    toast = op.inner_text(".toast")
    check("Acknowledges" in summary and "Changes the verdict" in summary, f"bulk dialog explains: {summary.splitlines()[0]}")
    check(toast.startswith("Done"), f"operator in the group bulk-marked {count} alarms: {toast}")
    op.goto(f"{PORTAL}/alarms?state=all&verdict=false")
    op.wait_for_timeout(800)
    check(op.locator(".alarm").count() == count and op.locator(".chip.verdict-false").count() == count,
          f"all {count} now show False alarm (verdict filter)")
    audit = call(pg, "GET", "/api/audit?action=alarm.")["body"]
    check(any(r["action"] == "alarm.verdict" and r["detail"].get("bulk") for r in audit), "verdict overrides in the audit log")
    check(not errors, f"no JavaScript errors {errors[:2]}")
    b.close()

print(f"\n{len(fails)} failure(s)" if fails else "\nall verdict / live / bulk checks passed")
raise SystemExit(1 if fails else 0)

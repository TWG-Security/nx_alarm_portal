"""Dismiss: the third choice beside Real event / False alarm, in a real browser.
Closes an alarm without a verdict-style call and without an NX bookmark.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.dismiss [screenshot_dir]

Checks the critical pop-up, the drawer, the feed's quick-ack dialog and a 600-alarm bulk dismiss
(more than one 500-id API call), and times a new critical alarm reaching a second screen while the
bulk dismiss runs.
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
BULK_N = 600


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def bookmarks() -> int:
    return len(json.load(urllib.request.urlopen(f"{FAKE_NX}/_state")).get("bookmarks", []))


def open_count(pg) -> int:
    return pg.evaluate("""async () => (await (await fetch('/api/alarms?limit=500')).json()).length""")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    bm0 = bookmarks()

    # --- 1. critical pop-up: Dismiss closes it
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    check(pg.locator('#critical-dialog [data-verdict="dismissed"]').is_visible(), "critical pop-up shows Dismiss")
    pg.screenshot(path=f"{out}/d1-critical.png")
    pg.click('#critical-dialog [data-verdict="dismissed"]')
    pg.wait_for_selector("#critical-dialog[open]", state="detached", timeout=5000)
    check("Alarm dismissed" in pg.inner_text(".toast"), "critical pop-up: Dismiss closes it, toast 'Alarm dismissed'")

    # --- 2. drawer: Dismiss shows the Dismissed chip
    inject("line")
    pg.wait_for_selector(".feed-item.p2", timeout=10000)
    pg.click(".feed-item.p2 [data-detail]")
    pg.wait_for_selector('.drawer [data-ack="dismissed"]', timeout=5000)
    pg.screenshot(path=f"{out}/d2-drawer.png")
    pg.click('.drawer [data-ack="dismissed"]')
    pg.wait_for_selector(".drawer .chip.verdict-dismissed", timeout=5000)
    nx = pg.inner_text(".drawer .ack-box")
    check("skipped" in nx, "drawer: Dismiss shows the Dismissed chip and NX write-back 'skipped'")
    pg.click("#drawer-close")

    # --- 3. quick ack dialog from a feed card
    inject("dock")
    pg.wait_for_selector(".feed-item.p3", timeout=10000)
    pg.click(".feed-item.p3 [data-ack]")
    pg.wait_for_selector("#ack-dialog[open]")
    check(pg.locator("#ack-dismissed").is_visible(), "quick ack dialog shows Dismiss")
    pg.click("#ack-dismissed")
    pg.wait_for_function("!document.querySelector('.feed-item.p3')", timeout=5000)
    check(True, "quick ack dialog: Dismiss removes the card from the feed")
    check(bookmarks() == bm0, f"no NX bookmarks from the three dismissals ({bookmarks() - bm0} added)")

    # --- 4. bulk dismiss of 600, with a second screen timing a new alarm meanwhile
    for _ in range(BULK_N):
        inject("line")
    deadline = time.time() + 60
    while open_count(pg) < min(BULK_N, 500) and time.time() < deadline:
        time.sleep(0.5)
    time.sleep(3)
    check(open_count(pg) >= 500, f"{BULK_N} alarms injected (API shows {open_count(pg)}+ open)")

    wall = ctx.new_page()                                # the wall screen: map page with the live feed
    wall.on("pageerror", lambda e: errors.append(str(e)))
    wall.goto(f"{PORTAL}/")
    wall.wait_for_selector(".feed-item", timeout=10000)

    pg.goto(f"{PORTAL}/alarms?state=open")
    pg.wait_for_selector(".alarm", timeout=10000)
    while pg.locator("#load-more").is_visible():
        pg.click("#load-more")
        pg.wait_for_timeout(400)
    shown = pg.locator(".alarm").count()
    pg.check("#bulk-all")
    pg.click('[data-bulk="dismissed"]')
    pg.wait_for_selector("#bulk-dialog[open]")
    title, summary = pg.inner_text("#bulk-title"), pg.inner_text("#bulk-summary")
    check(title == f"Dismiss {shown} alarms" and "No NX bookmarks" in summary, f"bulk dialog: '{title}'; {summary.splitlines()[0]}")
    pg.screenshot(path=f"{out}/d3-bulk.png")
    t_bulk = time.time()
    pg.click("#bulk-apply")
    time.sleep(0.3)                                      # bulk is running server-side now
    t = time.time(); inject("panic")
    wall.wait_for_selector("#critical-dialog[open]", timeout=15000)
    lat = round((time.time() - t) * 1000)
    check(lat < 1000, f"new critical alarm on the wall screen {lat} ms after it fired, during the bulk dismiss")
    pg.wait_for_selector(".toast", timeout=120000)
    pg.wait_for_function("document.querySelector('.toast')?.innerText.startsWith('Done')", timeout=120000)
    took = time.time() - t_bulk
    toast = pg.inner_text(".toast")
    check(f"{shown} acknowledged" in toast, f"bulk dismiss of {shown} took {took:.1f} s: {toast}")
    left = open_count(pg)
    check(left == 1, f"only the new critical alarm is still open ({left} open)")
    check(bookmarks() == bm0, f"no NX bookmarks from the bulk dismiss ({bookmarks() - bm0} added)")
    pg.goto(f"{PORTAL}/alarms?state=all&verdict=dismissed")
    pg.wait_for_selector(".chip.verdict-dismissed", timeout=10000)
    check(True, "verdict filter 'Dismissed' lists them")
    check(not errors, f"no JavaScript errors {errors[:2]}")
    b.close()

print(f"\n{len(fails)} failure(s)" if fails else "\nall dismiss checks passed")
raise SystemExit(1 if fails else 0)

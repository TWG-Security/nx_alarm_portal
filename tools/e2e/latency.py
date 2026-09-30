"""Alarm delivery end to end: push latency, and the degraded mode when the live stream breaks.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.latency
(polling at 60 s proves delivery is by push; expect tens of milliseconds)
"""
import time
from playwright.sync_api import sync_playwright
from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    login_and_add_site(pg); pg.wait_for_timeout(3000)
    # --- 1. push latency, with polling at 60 s
    lat = []
    for kind, sel in (("line", ".feed-item.p2"), ("dock", ".feed-item.p3"), ("panic", "#critical-dialog[open]")):
        n = pg.locator(sel).count() if not sel.startswith("#") else 0
        t = time.time(); inject(kind)
        if sel.startswith("#"): pg.wait_for_selector(sel, timeout=10000)
        else: pg.wait_for_function(f"document.querySelectorAll('{sel}').length > {n}", timeout=10000)
        lat.append((kind, round((time.time() - t) * 1000)))
    print("NX event -> on operator screen (ms), polling at 60 s:", lat)
    pg.keyboard.press("Escape")
    # --- 2. stream lost: banner + tone within ~10 s, alarms still arrive via 2 s fallback
    pg.route("**/api/events", lambda r: r.fulfill(status=502, body="Bad Gateway"))
    pg.goto(f"{PORTAL}/alarms"); pg.mouse.click(600, 400)
    osc0 = pg.evaluate("window.__osc")
    t = time.time(); pg.wait_for_selector("#conn-banner:not([hidden])", timeout=20000)
    print(f"stream blocked: loud banner after {round(time.time() - t, 1)}s:", pg.inner_text("#conn-banner")[:40])
    pg.wait_for_timeout(1200)
    print("connection-lost tone played:", pg.evaluate("window.__osc") > osc0)
    n = pg.locator(".alarm.p2").count(); t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.alarm.p2').length > {n}", timeout=15000)
    print(f"alarm while stream is down arrived in {round(time.time() - t, 1)}s (fallback polling)")
    pg.unroute("**/api/events")
    pg.wait_for_selector("#conn-banner[hidden]", state="attached", timeout=30000)
    print("stream back; banner cleared; indicator:", pg.inner_text("#live-status"))
    b.close()

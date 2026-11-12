"""Mute a site / camera (silent, still listed), Large video and Site cameras in the alarm details.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.mute
(polling at 60 s: every arrival measured here came by push)
Screenshots go to E2E_SHOTS (default: the current directory).
"""
import os
import time

from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site

SHOTS = os.environ.get("E2E_SHOTS", ".")
ok = True


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + what)


def arrive(pg, sel, kind):
    n = pg.locator(sel).count()
    t = time.time(); inject(kind)
    pg.wait_for_function(f"document.querySelectorAll('{sel}').length > {n}", timeout=10000)
    return round((time.time() - t) * 1000)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1920, "height": 1080})
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    login_and_add_site(pg); pg.wait_for_timeout(3000)
    site_id = pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0].id)")

    # 1. an alarm, its details: Site cameras strip, Large video
    ms = arrive(pg, ".feed-item.p2", "line")
    print(f"alarm on screen in {ms} ms")
    pg.locator(".feed-item.p2 [data-detail]").first.click()
    pg.wait_for_selector(".drawer-cams:not([hidden]) .cam-tile", timeout=10000)
    tiles = pg.locator(".cam-tile").count()
    check(tiles >= 2, f"site cameras strip shows {tiles} cameras, alarm camera first: "
          f"{pg.locator('.cam-tile').first.get_attribute('class')}")
    pg.wait_for_function("[...document.querySelectorAll('.cam-tile img')].every(i => i.complete && i.naturalWidth > 0)", timeout=15000)
    check(True, "every site camera still loaded")
    compact = pg.locator(".drawer-media .live-stage").bounding_box()
    pg.screenshot(path=f"{SHOTS}/mute-1-compact.png")
    pg.click("#drawer-layout")
    pg.wait_for_timeout(400)
    big = pg.locator(".drawer-media .live-stage").bounding_box()
    drawer = pg.locator("#drawer").bounding_box()
    side = pg.locator(".map-side").bounding_box()
    feed = pg.locator("#feed-list").bounding_box()
    check(big["width"] > compact["width"] * 1.6, f"Large video: live view {compact['width']:.0f} -> {big['width']:.0f} px wide")
    check(abs(drawer["x"] - side["x"]) < 2 and feed["x"] >= drawer["x"] + drawer["width"] - 2,
          f"Large video covers the site column (x {drawer['x']:.0f}, width {drawer['width']:.0f}); live alarms stay visible (x {feed['x']:.0f})")
    pg.screenshot(path=f"{SHOTS}/mute-2-large.png")
    other = pg.locator(".cam-tile:not(.is-alarm)").first
    name = other.get_attribute("data-name")
    other.click()
    pg.wait_for_selector(f".live-label:has-text('{name}')", timeout=5000)
    src = pg.evaluate("document.querySelector('.live-mount video')?.src || document.querySelector('.live-mount .live-still')?.src")
    check(f"/media/sites/{site_id}/cameras/" in (src or ""), f"clicking '{name}' streams it in the Live pane ({(src or '')[:70]})")
    pg.wait_for_function("document.querySelector('.live-mount video')?.readyState >= 2 || !document.querySelector('.live-mount .live-still').hidden", timeout=15000)
    pg.screenshot(path=f"{SHOTS}/mute-3-other-camera.png")
    pg.click("[data-cam-back]")
    pg.wait_for_selector(".live-label:has-text('now')", timeout=5000)
    check(True, "↩ Alarm camera goes back")

    # 2. mute the whole site from the details: alarms still arrive (by push), silently
    pg.wait_for_selector(".mute-slot [data-mute='5'][data-dev='']", timeout=5000)
    labels = pg.eval_on_selector_all(".mute-slot [data-dev='']", "bs => bs.map(b => b.textContent)")
    check(labels == ["5 min", "15 min", "30 min", "1 h", "4 h", "8 h", "24 h"], f"mute lengths up to 24 h: {labels}")
    pg.click(".mute-slot [data-mute='5'][data-dev='']")
    pg.wait_for_selector(".mute-slot .mute-on", timeout=5000)
    check("Site muted" in pg.inner_text(".mute-slot"), "details show 'Site muted' with Unmute")
    pg.click("#drawer-close")
    tl = pg.evaluate(f"fetch('/api/sites/{site_id}').then(r => r.json()).then(s => s.mute)")
    check("site" in tl, f"server has the site mute until {tl.get('site', {}).get('until_ms')}")
    pg.wait_for_timeout(1500)
    osc0 = pg.evaluate("window.__osc")
    lat = [arrive(pg, ".feed-item--muted", "line") for _ in range(3)]
    pg.wait_for_timeout(2500)                                    # the arrival tone and the repeat loop would play by now
    check(pg.evaluate("window.__osc") == osc0, f"muted alarms listed in {lat} ms, no sound (tones {osc0} -> {pg.evaluate('window.__osc')})")
    check(pg.locator(".feed-item--muted .muted-chip").count() >= 3, "feed cards say Muted")
    check(pg.locator(".site-item .muted-chip").count() == 1, "site list shows the mute")
    pg.screenshot(path=f"{SHOTS}/mute-4-feed.png")
    t = time.time(); inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    crit = round((time.time() - t) * 1000)
    pg.wait_for_timeout(300)
    check(pg.evaluate("window.__osc") > osc0, f"critical still pops up ({crit} ms) and sounds while the site is muted")
    pg.keyboard.press("Escape")
    pg.evaluate("""async () => { const t = document.querySelector('meta[name=csrf-token]').content;
        for (const id of [...(await (await fetch('/api/alarms?state=open')).json())].filter(a => a.priority === 1).map(a => a.id))
          await fetch(`/api/alarms/${id}/ack`, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':t}, body: JSON.stringify({verdict:'dismissed', note:''})}); }""")
    pg.wait_for_timeout(800)

    # 3. unmute from the site panel; the next alarm sounds again
    pg.locator(f".site-item[data-id='{site_id}']").click()
    pg.wait_for_selector("#site-detail [data-unmute]", timeout=5000)
    pg.click("#site-detail [data-unmute]")
    pg.wait_for_selector("#site-detail .mute-on", state="detached", timeout=5000)
    pg.wait_for_timeout(1500)
    osc1 = pg.evaluate("window.__osc")
    ms = arrive(pg, ".feed-item.p2:not(.feed-item--muted)", "line")
    pg.wait_for_timeout(500)
    check(pg.evaluate("window.__osc") > osc1, f"after Unmute an alarm sounds again ({ms} ms)")

    # 4. Acknowledge dialog: Then mute this camera
    pg.locator(".feed-item.p2:not(.feed-item--muted) [data-ack]").first.click()
    pg.wait_for_selector("#ack-mute-row:not([hidden])", timeout=5000)
    pg.select_option("#ack-mute", "camera"); pg.select_option("#ack-mute-min", "1440")
    pg.screenshot(path=f"{SHOTS}/mute-5-ack-dialog.png")
    pg.click("#ack-false")
    pg.wait_for_function(f"fetch('/api/sites/{site_id}').then(r => r.json()).then(s => Object.keys(s.mute.cameras || {{}}).length === 1)", timeout=5000)
    m = pg.evaluate(f"fetch('/api/sites/{site_id}').then(r => r.json()).then(s => s.mute)")
    cam = next(iter(m["cameras"].values()))
    check(abs(cam["until_ms"] - cam["at_ms"] - 86_400_000) < 1000 and "site" not in m, f"acknowledge + mute camera '{cam['name']}' for 24 h")
    osc2 = pg.evaluate("window.__osc")
    ms = arrive(pg, ".feed-item--muted", "line")
    pg.wait_for_timeout(1500)
    check(pg.evaluate("window.__osc") == osc2, f"that camera's next alarm is listed ({ms} ms) and silent")
    pg.locator(".feed-item--muted").first.click()
    pg.wait_for_selector(".mute-slot .mute-on", timeout=5000)
    txt = pg.inner_text(".mute-slot .mute-on")
    check("Camera muted" in txt and "until tomorrow" in txt, f"details say until when: {txt!r}")
    pg.screenshot(path=f"{SHOTS}/mute-6-24h.png")
    pg.click("#drawer-close")
    n = pg.locator(".feed-item.p3").count()
    inject("dock")                                                # another camera, a system warning: never muted
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p3').length > {n}", timeout=10000)
    check(pg.locator(".feed-item.p3.feed-item--muted").count() == 0, "a warning from another camera isn't muted")
    b.close()

print("ALL PASS" if ok else "SOME CHECKS FAILED")

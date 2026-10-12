"""Alarm video end to end: growing clip, timeline controls, bounding boxes, critical pop-up.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.player [screenshot-dir]
"""
import sys
from playwright.sync_api import sync_playwright
from tools.e2e.common import BOX_PIXELS as _BOX, inject, login_and_add_site
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
errors = []
BOX_PIXELS = "(() => (" + _BOX + ")('%s'))"
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append("console: " + m.text))
    login_and_add_site(pg)
    inject("line")
    pg.wait_for_selector(".feed-item.p2", timeout=15000)
    pg.click(".feed-item.p2 [data-detail]")
    pg.wait_for_selector("#drawer .player-msg", timeout=5000)
    print("while recording:", pg.inner_text("#drawer .player-msg"))
    pg.screenshot(path=f"{OUT}/player-pending.png")
    pg.wait_for_selector("#drawer video:not([hidden])", timeout=40000)
    pg.wait_for_timeout(1500)
    v = pg.evaluate("() => { const v = document.querySelector('#drawer video'); return {dur: v.duration, t: v.currentTime, paused: v.paused, loop: v.loop, w: v.videoWidth, rs: v.readyState}; }")
    print("video:", v)
    print("clock:", pg.inner_text("#drawer .player-clock"), "| legend:", pg.inner_text("#drawer .tl-legend"))
    pg.click('#drawer [data-c="alarm"]'); pg.click('#drawer [data-c="play"]'); pg.wait_for_timeout(300)
    print("paused at alarm; clock:", pg.inner_text("#drawer .player-clock"), "| box pixels:", pg.evaluate(BOX_PIXELS % "#drawer"))
    pg.screenshot(path=f"{OUT}/player-drawer.png")
    start_before = pg.inner_text("#drawer .tl-start")
    pg.click('#drawer [data-c="earlier"]')
    pg.wait_for_function("() => document.querySelector('#drawer .tl-start').textContent !== '%s' && !document.querySelector('#drawer video').hidden" % start_before, timeout=30000)
    print("widened window: start", start_before, "->", pg.inner_text("#drawer .tl-start"))
    pg.select_option("#drawer .player-speed", "2"); print("rate:", pg.evaluate("document.querySelector('#drawer video').playbackRate"))
    pg.click('#drawer [data-c="play"]'); t0 = pg.evaluate("document.querySelector('#drawer video').currentTime")
    pg.click('#drawer [data-c="fwd"]'); t1 = pg.evaluate("document.querySelector('#drawer video').currentTime")
    print("frame step:", round(t1 - t0, 2), "s")
    pg.keyboard.press("Escape")

    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=15000)
    pg.wait_for_selector("#critical-dialog video:not([hidden])", timeout=40000)
    pg.wait_for_timeout(1200)
    print("critical pop-up video playing:", not pg.evaluate("document.querySelector('#critical-dialog video').paused"),
          "| box pixels:", pg.evaluate(BOX_PIXELS % "#critical-dialog"))
    pg.screenshot(path=f"{OUT}/player-critical.png")
    b.close()
print("errors:", errors)

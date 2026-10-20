"""Camera health end to end: the worker learns references from the fake NX, a scene change and a blurry camera show up
with boxes on the Camera health page, the nav badge and the map's site panel; Accept view and Mute; alarms stay fast
while the worker runs; the page fits a phone.

    CAMHEALTH=1 CAMHEALTH_INTERVAL_S=6 POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.camhealth [screenshot_dir]
"""
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site
from tools.health_scene import CHANGE_BOX, H, W

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
GATE, DOCK = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"   # PTZ (Recording) / Online


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def scene(dev, mode):
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_health/{dev}/{mode}", method="POST"))


def card(pg, name):
    return pg.locator(".hc", has=pg.locator(".hc-name", has_text=name))


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    scene("all", "normal")
    login_and_add_site(pg)
    pg.goto(f"{PORTAL}/health")
    pg.select_option("#hf-show", "all")
    t0 = time.time()
    pg.wait_for_function("""() => [...document.querySelectorAll('.hc--ok .hc-chip--ok')].filter(e => e.textContent === 'OK').length >= 2""",
                         timeout=90000)
    check(True, f"both cameras checked and their references learned in {time.time() - t0:.0f} s")

    # A pallet appears in front of the dock camera (an Online camera: frames come off the live MJPEG stream).
    scene(DOCK, "change")
    pg.select_option("#hf-show", "attention")
    t0 = time.time()
    card(pg, "Loading Dock").locator(".hc-chip--warning", has_text="Scene changed").wait_for(timeout=90000)
    check(True, f"Scene changed shown {time.time() - t0:.0f} s after the change (3 checks in a row)")
    box = card(pg, "Loading Dock").locator(".hc-box").first.bounding_box()
    img = card(pg, "Loading Dock").locator("img").bounding_box()
    rel = [(box["x"] - img["x"]) / img["width"], (box["y"] - img["y"]) / img["height"], box["width"] / img["width"], box["height"] / img["height"]]
    truth = [CHANGE_BOX[0] / W, CHANGE_BOX[1] / H, CHANGE_BOX[2] / W, CHANGE_BOX[3] / H]
    from app.camhealth.state import iou
    cx, cy = rel[0] + rel[2] / 2, rel[1] + rel[3] / 2
    inside = truth[0] < cx < truth[0] + truth[2] and truth[1] < cy < truth[1] + truth[3]
    check(inside and iou(rel, truth) > 0.4, f"the box sits on the pallet ({[round(v, 2) for v in rel]} vs {[round(v, 2) for v in truth]}, IoU {iou(rel, truth):.2f})")

    # The PTZ camera goes out of focus: a problem (PTZ: no moved / scene checks, but blur counts at its home view).
    scene(GATE, "blur")
    card(pg, "Front Gate PTZ").locator(".hc-chip--problem", has_text="Out of focus").wait_for(timeout=90000)
    check(True, "a blurry camera is a problem")
    pg.wait_for_function("document.getElementById('nav-health-count').textContent === '2'", timeout=15000)
    check(True, "the Cameras badge counts 2")
    check(pg.locator(".hc").first.get_attribute("class").find("hc--problem") >= 0, "problems are listed first")
    pg.screenshot(path=f"{out}/camhealth-page.png", full_page=True)

    # Details: Reference | Now with the box; Accept current view.
    card(pg, "Loading Dock").click()
    pg.wait_for_selector("#hc-dialog[open] #hcd-now .hc-box")
    check(pg.locator(".hcd-ref--now img").count() == 1, "the dialog shows the day reference in use")
    check("Night reference" in pg.inner_text("#hcd-refs"), "and a night reference pane")
    pg.locator("#hc-dialog").screenshot(path=f"{out}/camhealth-dialog.png")
    pg.once("dialog", lambda d: d.accept())
    pg.click("[data-act=accept]")
    pg.wait_for_function("!document.querySelector('.hc .hc-name') || ![...document.querySelectorAll('.hc .hc-name')].some(e => e.textContent.includes('Loading Dock'))", timeout=15000)
    check(True, "Accept current view: the scene change is gone from Needs attention")
    pg.click("#hcd-close")

    # Retake the day reference of the blurry one: the next clean day check becomes it.
    card(pg, "Front Gate PTZ").click()
    pg.wait_for_selector("#hc-dialog[open] [data-ref-act=retake][data-ref=day]")
    pg.click("[data-ref-act=retake][data-ref=day]")
    pg.wait_for_selector("#hc-dialog[open] [data-ref-act=cancel][data-ref=day]", timeout=10000)
    check(True, "Retake now: pending until the next clean day check")
    pg.click("[data-ref-act=cancel][data-ref=day]")
    pg.wait_for_selector("#hc-dialog[open] [data-ref-act=retake][data-ref=day]", timeout=10000)
    pg.click("#hcd-close")

    # TWG's processing panel.
    pg.wait_for_function("document.querySelectorAll('#hp-tiles .hp-tile').length >= 6", timeout=40000)
    check("Off (CPU only)" in pg.inner_text("#hp-tiles"), "TWG processing panel: worker numbers (dev: no GPU)")
    check(pg.locator("#hp-recent tr").count() >= 1, "recent checks listed")

    # Mute the blurry one for 24 h.
    card(pg, "Front Gate PTZ").click()
    pg.wait_for_selector("#hc-dialog[open] [data-act=mute]")
    pg.click("[data-act=mute][data-hours='24']")
    pg.wait_for_function("document.getElementById('nav-health-count').hidden", timeout=15000)
    check(True, "muting it clears the badge")
    pg.click("#hcd-close")
    pg.select_option("#hf-show", "muted")
    check(card(pg, "Front Gate PTZ").count() == 1, "it's listed under Muted")

    # The map's site panel says it in one line.
    card(pg, "Front Gate PTZ").click()
    pg.wait_for_selector("#hc-dialog[open] [data-act=mute][data-hours='0']")
    pg.click("[data-act=mute][data-hours='0']")
    pg.goto(f"{PORTAL}/")
    pg.click(".site-item")
    pg.wait_for_selector(".sp-health", timeout=15000)
    check("1 camera problem" in pg.inner_text(".sp-health"), f"site panel: {pg.inner_text('.sp-health')!r}")

    # Alarms while the worker checks every few seconds.
    lat = []
    for _ in range(5):
        n = pg.locator(".feed-item.p2").count()
        t = time.time(); inject("line")
        pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=10000)
        lat.append(round((time.time() - t) * 1000))
        time.sleep(1.5)
    check(max(lat) < 1000, f"alarms on screen in {lat} ms with the worker running")

    # Phone.
    ph = b.new_page(viewport={"width": 390, "height": 844}, storage_state=pg.context.storage_state())
    ph.goto(f"{PORTAL}/health")
    ph.select_option("#hf-show", "all")
    ph.wait_for_selector(".hc")
    sw = ph.evaluate("document.documentElement.scrollWidth")
    check(sw <= 390, f"phone: no sideways scroll ({sw} px)")
    ph.screenshot(path=f"{out}/camhealth-phone.png", full_page=True)
    check(not errors, f"no page errors {errors}")
    scene("all", "off")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

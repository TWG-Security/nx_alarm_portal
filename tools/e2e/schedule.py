"""Arming schedule editor on the site form, in a real browser.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.schedule [screenshot_dir]

Business-hours preset, the week preview (armed / disarmed spans), 12-hour time pick list, quick day buttons,
problems flagged before saving, the card's own Save (kept after a reload), Undo.
"""
import sys

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


WIDTHS = """() => [...document.querySelectorAll('.sched-day:not(.sched-ticks)')].map((d) => [...d.querySelectorAll('.sched-bar span')]
  .map((s) => [s.className, Math.round(parseFloat(s.style.width) * 10) / 10]))"""

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    site_id = pg.evaluate("async () => (await (await fetch('/api/sites')).json())[0].id")
    pg.goto(f"{PORTAL}/sites/{site_id}/edit")
    pg.wait_for_selector("#sched-rows")
    check(pg.is_disabled("#sched-save") and "Saved" in pg.inner_text("#sched-state"), "nothing to save at first")

    pg.click("#sched-preset")
    check(pg.locator(".sched-row").count() == 2, "business-hours preset adds two times")
    labels = pg.evaluate("() => [...document.querySelectorAll('.sched-row .sched-time')].map((s) => s.selectedOptions[0].text)")
    check(labels == ["7:00 AM", "6:00 PM"], f"times read as 12-hour clock times {labels}")
    w = pg.evaluate(WIDTHS)
    # Monday: armed 0:00-7:00 (29.2 %), disarmed 7:00-18:00 (45.8 %), armed 18:00-24:00 (25 %). Saturday armed all day.
    check(w[0] == [["armed", 29.2], ["disarmed", 45.8], ["armed", 25.0]] and w[5] == [["armed", 100.0]], f"week preview right {w[0]} / {w[5]}")
    check(not pg.is_disabled("#sched-save") and "Unsaved" in pg.inner_text("#sched-state"), "Save schedule lights up with 'Unsaved changes'")
    pg.screenshot(path=f"{out}/schedule-preset.png", full_page=True)

    # A conflicting row is flagged and blocks saving; a row with no days too.
    pg.click("#sched-add")
    row = pg.locator(".sched-row").nth(2)
    row.locator("[data-act=disarm]").click()
    row.locator(".sched-time").select_option("18:00")
    pg.wait_for_timeout(200)
    check("set to both arm and disarm" in pg.inner_text("#sched-problems") and pg.is_disabled("#sched-save"), "arm + disarm at the same time is flagged, Save blocked")
    row.locator("[data-days=weekend]").click()
    pg.wait_for_timeout(200)
    check(pg.inner_text("#sched-problems") == "" and not pg.is_disabled("#sched-save"), "Weekend quick button fixes it")
    w = pg.evaluate(WIDTHS)
    check(w[5][0][0] == "armed" and w[5][-1][0] == "disarmed", f"Saturday now disarms at 6 PM {w[5]}")

    pg.click("#sched-save")
    pg.wait_for_function("document.getElementById('sched-state').textContent.includes('Saved')", timeout=5000)
    check(pg.is_disabled("#sched-save"), "saved")
    saved = pg.evaluate(f"async () => (await (await fetch('/api/sites')).json()).find((s) => s.id === {site_id}).arm_schedule")
    check(len(saved) == 3, f"server has the 3 times {saved}")
    pg.reload(); pg.wait_for_selector(".sched-row")
    check(pg.locator(".sched-row").count() == 3 and pg.is_disabled("#sched-save"), "kept after a reload")

    pg.locator(".sched-row").nth(0).locator(".sched-del").click()
    check("Unsaved" in pg.inner_text("#sched-state"), "removing a time is an unsaved change")
    pg.click("#sched-undo")
    check(pg.locator(".sched-row").count() == 3 and pg.is_disabled("#sched-save"), "Undo puts it back")
    pg.screenshot(path=f"{out}/schedule-saved.png", full_page=True)
    check(not errors, f"no page errors {errors[:3]}")
    b.close()

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

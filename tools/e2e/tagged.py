"""Tagged NX rules only, the site's Check now and its NX rules list, and the bigger top-bar clock.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.tagged [screenshot_dir]

With "Only NX rules tagged #critical, #alarm or #warning" on, the fake panic rule tagged #critical raises an alarm
and the untagged line-crossing rule doesn't; switched off, it does again. Timed.
"""
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def tag(kind, comment):
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_rules/{kind}?comment={urllib.parse.quote(comment)}", method="POST"))


API = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
    return [r.status, await r.json()]; }"""

def patches():
    import json
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/_rule_patches"))


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    tag("panic", "Panic button #critical")
    tag("line", "Line crossing")
    login_and_add_site(pg)
    pg.wait_for_timeout(3000)

    size = pg.eval_on_selector("#sys-clock", "e => parseFloat(getComputedStyle(e).fontSize)")
    check(size >= 18, f"the top-bar clock is bigger ({size}px)")

    # Settings → Alarm levels: tick "tagged rules only".
    pg.goto(pg.url.split("/", 3)[0] + "//" + pg.url.split("/")[2] + "/settings")
    pg.wait_for_selector("#tagged-only")
    pg.check("#tagged-only")
    pg.click("#save-levels")
    pg.wait_for_selector(".toast:has-text('saved')", timeout=5000)
    check(pg.evaluate(API, ["/api/settings/alarm-levels", "GET", None])[1]["tagged_only"] is True, "Settings: tagged rules only saved")

    # Map → site → Check now, then View NX rules.
    pg.goto(pg.url.rsplit("/", 1)[0] + "/")
    pg.wait_for_timeout(1500)
    pg.click(".site-item")
    pg.click("[data-check]")
    pg.wait_for_selector(".toast:has-text('checked')", timeout=15000)
    check("4 NX rules" in pg.inner_text(".toast:has-text('checked')"), f"Check now: {pg.inner_text('.toast:has-text(' + chr(39) + 'checked' + chr(39) + ')')!r}")
    pg.keyboard.press("Escape")
    pg.click("[data-rules]")
    pg.wait_for_selector(".rules-dialog .rules-table", timeout=8000)
    rows = pg.eval_on_selector_all(".rules-table tbody tr", "trs => trs.map(t => [t.cells[1].innerText.split('\\n')[0], t.cells[0].innerText.split('\\n')[0]])")
    print("      rules:", rows)
    d = {k: v.title() for k, v in rows}
    check(d.get("Panic button #critical") == "Critical" and d.get("Line crossing") == "Ignored", "NX rules list: the tagged rule is Critical, the untagged one Ignored")
    check("tagged rules only" in pg.inner_text(".rules-dialog"), "the list says tagged rules only is on")
    pg.screenshot(path=f"{out}/tagged-rules.png")
    pg.click("[data-rules-close]")

    # Untagged event: ignored. Tagged: raised.
    before = pg.locator(".feed-item").count()
    inject("line")
    time.sleep(4)
    check(pg.locator(".feed-item").count() == before, "an event from the untagged rule is ignored")
    t0 = time.time()
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = (time.time() - t0) * 1000
    check(ms < 1000, f"an event from the #critical rule raises a critical alarm ({ms:.0f} ms)")
    pg.keyboard.press("Escape")

    # An admin tags the line-crossing rule #alarm from the portal: written to NX, read back, raised from then on.
    pg.click("[data-rules]")
    pg.wait_for_selector(".rules-dialog .rules-table", timeout=8000)
    pg.click("[data-rules-filter=tagged]")
    check(pg.locator(".rules-table tbody tr").count() == 1, "filter Tagged only: just the tagged rule")
    pg.click("[data-rules-filter=all]")
    pg.click("tr:has-text('Line crossing') [data-rule-edit]")
    pg.wait_for_selector(".rx [data-x=event-type]", timeout=15000)
    pg.select_option(".rx [data-d=level]", "alarm")
    pg.fill(".rx input[data-k=intervalS]", "0")
    pg.screenshot(path=f"{out}/tagged-edit.png")
    pg.click(".rx [data-x=save]")
    pg.wait_for_selector(".toast:has-text('Saved in NX')", timeout=10000)
    sent = [x for x in patches() if "comment" in x]
    check(sent and sent[-1]["comment"] == "Line crossing #alarm" and sent[-1]["action"]["intervalS"] == 0,
          f"Save in NX: a partial update with the new tag and interval {sent[-1:]}")
    rows = {k: v.title() for k, v in pg.eval_on_selector_all(".rules-table tbody tr[data-rule-row]", "trs => trs.map(t => [t.cells[1].innerText.split('\\n')[0], t.cells[0].innerText.split('\\n')[0]])")}
    check(rows.get("Line crossing #alarm") == "Alarm", f"the list shows it as Alarm at once ({rows})")
    pg.click("[data-rules-close]")
    before = pg.locator(".feed-item").count()
    t0 = time.time()
    inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {before}", timeout=10000)
    check(True, f"now tagged, its events raise alarms ({(time.time() - t0) * 1000:.0f} ms)")
    tag("line", "Line crossing")                     # untag it again in NX for the last step

    # The admin switch in the rules list turns tagged rules only off: untagged rules raise alarms again.
    pg.click("[data-rules]")
    pg.wait_for_selector("[data-rules-tagged-only]", timeout=8000)
    pg.uncheck("[data-rules-tagged-only]")
    pg.wait_for_selector(".toast:has-text('Tagged rules only: off')", timeout=5000)
    pg.click("[data-rules-close]")
    pg.click("[data-check]"); pg.wait_for_timeout(1500)
    before = pg.locator(".feed-item").count()
    t0 = time.time()
    inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {before}", timeout=10000)
    check(True, f"setting off: the untagged rule raises alarms again ({(time.time() - t0) * 1000:.0f} ms)")
    check(not errors, f"no page errors {errors}")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

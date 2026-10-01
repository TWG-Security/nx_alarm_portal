"""Incident export end to end: acknowledge with a note, export the evidence package from the alarm
drawer, check its contents, and measure that an alarm arriving mid-export is not delayed.

    tools/dev_up.sh && .venv/bin/python -m tools.e2e.export [out_dir]
"""
import hashlib
import io
import json
import sys
import time
import zipfile

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    inject("line")
    pg.wait_for_selector(".feed-item.p2", timeout=10000)
    aid = int(pg.get_attribute(".feed-item.p2", "data-id"))
    pg.evaluate("""async (id) => { const t = document.querySelector('meta[name=csrf-token]').content;
        await fetch(`/api/alarms/${id}/ack`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
          body: JSON.stringify({note: 'Verified on camera: delivery driver at the gate. No action.'})}); }""", aid)
    pg.wait_for_timeout(9000)                                  # let the post-alarm footage finish recording
    pg.goto(f"{PORTAL}/alarms?state=all&open={aid}")
    pg.wait_for_selector("#drawer-export", timeout=10000)
    pg.click("#drawer-export")
    pg.check('input[name="export-format"][value="zip"]')
    pg.fill("#export-note", "Requested by site manager for the delivery log.")
    n = pg.locator(".alarm.p3").count()
    t0 = time.time()
    with pg.expect_download(timeout=90000) as dl:
        pg.click("#export-submit")
        inject("dock")                                          # an alarm arrives while the export is built
        pg.wait_for_function(f"document.querySelectorAll('.alarm.p3').length > {n}", timeout=10000)
        alarm_ms = round((time.time() - t0) * 1000)
    export_s = time.time() - t0
    check(alarm_ms < 1500, f"alarm during the export shown in {alarm_ms} ms")
    data = open(dl.value.path(), "rb").read()
    check(True, f"evidence package {dl.value.suggested_filename} ({len(data) / 1e6:.1f} MB) in {export_s:.1f} s")
    pg.wait_for_selector("#export-status .alert-ok", timeout=5000)
    z = zipfile.ZipFile(io.BytesIO(data))
    files = {n.split("/", 1)[1]: z.read(n) for n in z.namelist()}
    man = json.loads(files["manifest.json"])
    check(all(hashlib.sha256(files[k]).hexdigest() == v for k, v in man["files"].items()), "all checksums match the manifest")
    check("clip.mp4" in files and sum(k.startswith("frames/annotated/") for k in files) >= 3,
          f"clip + {sum(k.startswith('frames/') for k in files)} frame files")
    open(f"{out}/export.zip", "wb").write(data)
    open(f"{out}/report.pdf", "wb").write(files["incident-report.pdf"])
    pg.goto(f"{PORTAL}/audit?alarm_id={aid}")
    pg.wait_for_function("!document.getElementById('audit-rows').textContent.includes('Loading')", timeout=5000)          # not just the "Loading…" row
    check("Alarm exported" in pg.inner_text("tbody"), "export is in the audit log")
    check(not errors, f"no JavaScript errors {errors[:2]}")
    b.close()
print(f"\n{len(fails)} failure(s)" if fails else "\nall export checks passed")
raise SystemExit(1 if fails else 0)

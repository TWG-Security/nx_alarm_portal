"""The full NX rule editor: drop-downs from the site's own NX, zones found per camera, create / edit / delete, the
failure paths (NX refusing writes, NX not describing its rules), phone width, and alarm latency while it's open.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.rule_editor [screenshot_dir]
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
GATE, DOCK = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
AXIS = "nx.axis.{c95466c7-a66e-d03d-7e39-13f1a05704cb}"


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def post(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}{path}", method="POST")))


def nx_rules():
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/rest/v4/events/rules"))


def critical_after(pg, fire, what):
    t0 = time.time()
    fire()
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    ms = (time.time() - t0) * 1000
    check(ms < 1000, f"{what}: critical pop-up in {ms:.0f} ms")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    for m in ("replace", "allow", "manifest"):
        post(f"/_rules_mode/{m}")
    inject("axis")                                  # an AXIS Object Analytics "Front Lot" event in NX's log
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    pg.click(".site-item")
    pg.click("[data-rules]")
    pg.wait_for_selector(".rules-dialog .rules-table", timeout=8000)

    # ---- New rule: drop-downs from NX, zones
    t0 = time.time()
    pg.click("[data-rule-new]")
    pg.wait_for_selector(".rx [data-x=event-type]", timeout=20000)
    check(True, f"editor ready with the site's catalogue in {(time.time() - t0) * 1000:.0f} ms")
    events = pg.eval_on_selector_all(".rx [data-x=event-type] option", "os => os.map(o => o.value)")
    check({"analytics", "analyticsObject", "softTrigger", "motion", "cameraInput", "generic"} <= set(events), f"event drop-down from NX: {len(events)} types")
    acts = pg.eval_on_selector_all(".rx [data-x=action-type] option", "os => os.map(o => o.value)")
    check("writeToLog" in acts and "desktopNotification" in acts and "http" not in acts, f"action drop-down: portal-safe actions only ({len(acts)})")
    pg.select_option(".rx [data-x=event-type]", "analytics")
    pg.check(f".rx input[data-k=devices][value='{GATE}']")
    zones = pg.eval_on_selector_all(".rx .rx-zone", "bs => bs.map(b => b.dataset.zone)")
    check(zones == ["Front Lot"], f"the gate camera's zone was found from its events: {zones}")
    pg.click(".rx .rx-zone[data-zone='Front Lot']")
    val = pg.eval_on_selector(".rx select[data-k=eventTypeId]", "s => s.value")
    check(val == AXIS, f"picking the zone fills in its event type ({val})")
    opts = pg.eval_on_selector_all(".rx select[data-k=eventTypeId] option", "os => os.map(o => o.textContent)")
    check(any("Front Lot" in o and "seen" in o for o in opts), f"the type drop-down names the zone and how often it was seen {opts[:3]}")
    pg.check(f".rx input[data-k=devices][value='{DOCK}']")
    zones = pg.eval_on_selector_all(".rx .rx-zone", "bs => bs.map(b => b.dataset.zone)")
    check({"Dock Apron", "Tripwire 1"} <= set(zones), f"the dock camera's drawn zones from its plugin settings: {zones}")
    pg.click(f".rx [data-x=zones-show][data-cam='{DOCK}']")
    pg.wait_for_selector(".rx .rx-pic img", timeout=8000)
    pg.wait_for_function("document.querySelector('.rx .rx-pic img')?.complete", timeout=8000)
    check(pg.locator(".rx .rx-pic svg polygon").count() == 1 and pg.locator(".rx .rx-pic svg polyline").count() == 1,
          "Show on camera: the area and the tripwire are drawn over the camera's picture")
    pg.screenshot(path=f"{out}/rule-editor-zones.png", full_page=True)
    pg.uncheck(f".rx input[data-k=devices][value='{DOCK}']")
    pg.select_option(".rx [data-d=level]", "critical")
    pg.fill(".rx [data-d=comment]", "Front lot zone")
    pg.select_option(".rx [data-x=action-type]", "writeToLog")
    pg.fill(".rx input[data-k=intervalS]", "0")
    summary = pg.inner_text(".rx .rx-summary")
    check("Front Lot" in summary and "Critical" in summary and "Front Gate PTZ" in summary, f"summary reads: {summary!r}")
    critical_after(pg, lambda: inject("panic"), "an alarm while the editor is open")
    t0 = time.time()
    pg.click(".rx [data-x=save]")
    pg.wait_for_selector(".toast:has-text('Rule created in NX')", timeout=15000)
    check(True, f"Create in NX: {(time.time() - t0) * 1000:.0f} ms")
    made = next((r for r in nx_rules() if r.get("comment") == "Front lot zone #critical"), None)
    check(made and made["event"]["eventTypeId"] == AXIS and made["event"]["devices"]["ids"] == [GATE]
          and made["action"] == {"type": "writeToLog", "intervalS": 0}, f"NX has the new rule as built: {made}")
    rows = {r[0]: r[1] for r in pg.eval_on_selector_all(".rules-table tbody tr[data-rule-row]", "trs => trs.map(t => [t.cells[1].innerText.split('\\n')[0], t.cells[0].innerText.split('\\n')[0]])")}
    check(rows.get("Front lot zone #critical", "").lower() == "critical", "the list shows it as Critical at once")
    rid = made["id"].strip("{}")
    pg.click("[data-rules-close]")
    critical_after(pg, lambda: urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_inject/axis?rule_id={rid}", method="POST")),
                   "a Front Lot event from the new #critical rule")

    # ---- Edit it: values come back into the drop-downs; filter + action changed
    pg.click("[data-rules]")
    pg.wait_for_selector(".rules-dialog .rules-table", timeout=8000)
    pg.click(f"[data-rule-edit='{rid}']")
    pg.wait_for_selector(".rx [data-x=event-type]", timeout=15000)
    check(pg.eval_on_selector(".rx select[data-k=eventTypeId]", "s => s.value") == AXIS
          and pg.is_checked(f".rx input[data-k=devices][value='{GATE}']"), "Edit: the rule's camera and zone type are selected")
    pg.select_option(".rx select[data-k=caption][data-sub=checkType]", "doesNotContainKeywords")
    pg.fill(".rx input[data-k=caption][data-sub=value]", "test")
    pg.select_option(".rx [data-x=action-type]", "desktopNotification")
    pg.check(".rx input[data-k=acknowledge]")
    pg.fill(".rx input[data-k=intervalS]", "0")
    pg.click(".rx [data-x=save]")
    pg.wait_for_selector(".toast:has-text('Saved in NX')", timeout=15000)
    r = next(r for r in nx_rules() if r["id"].strip("{}") == rid)
    check(r["event"]["caption"] == {"checkType": "doesNotContainKeywords", "value": "test"} and r["action"]["type"] == "desktopNotification"
          and r["action"]["acknowledge"] is True, f"NX has the edit: caption {r['event']['caption']}, action {r['action']}")

    # ---- Failure paths: NX refuses writes; NX can't describe its rules
    post("/_rules_mode/refuse")
    pg.click(f"[data-rule-edit='{rid}']")
    pg.wait_for_selector(".rx [data-x=event-type]", timeout=15000)
    pg.uncheck(".rx [data-d=enabled]")
    pg.click(".rx [data-x=save]")
    pg.wait_for_selector(".rx .alert-error", timeout=15000)
    msg = pg.inner_text(".rx .alert-error")
    check("Power Users" in msg and pg.is_visible(".rx [data-x=save]"), f"NX refusing: the editor says so and keeps the draft ({msg!r})")
    check(next(r for r in nx_rules() if r["id"].strip("{}") == rid)["enabled"] is True, "nothing changed in NX")
    post("/_rules_mode/allow")
    pg.click(".rx [data-x=back]")
    post("/_rules_mode/nomanifest")
    pg.click("[data-rules-check]")                   # Check now: the server's catalogue cache stays, so wait it out via Read again
    pg.wait_for_selector(".rules-dialog .rules-table", timeout=15000)
    pg.click(f"[data-rule-edit='{rid}']")
    pg.wait_for_selector(".rx [data-x=refresh]", timeout=15000)
    pg.wait_for_timeout(2500)                        # "Read again" twice within 2 s reuses the first read
    pg.click(".rx [data-x=refresh]")
    pg.wait_for_selector(".rx [data-x=quick]", timeout=15000)
    check("not available on this NX" in pg.inner_text(".rx .alert-error"), "NX without a rule catalogue: the editor explains and offers Quick edit")
    pg.click(".rx [data-x=quick]")
    check(pg.locator("[data-re=level]").count() == 1, "Quick edit (tags, on/off, interval) still works")
    post("/_rules_mode/manifest")
    pg.click("[data-re-cancel]")

    # ---- Delete it
    pg.click(f"[data-rule-edit='{rid}']")
    pg.wait_for_selector(".rx", timeout=15000)
    if pg.locator(".rx [data-x=retry]").count():
        pg.click(".rx [data-x=retry]")
    pg.wait_for_selector(".rx [data-x=delete]", timeout=15000)
    pg.click(".rx [data-x=delete]")
    pg.wait_for_selector(".toast:has-text('Rule deleted in NX')", timeout=15000)
    check(not any(r["id"].strip("{}") == rid for r in nx_rules()), "Delete: gone from NX")
    check(pg.locator(f"[data-rule-edit='{rid}']").count() == 0, "and from the list")
    pg.click("[data-rules-close]")
    critical_after(pg, lambda: inject("panic"), "an alarm after all the rule writes")

    # ---- Phone width
    ph = b.new_page(viewport={"width": 390, "height": 844})
    ph.on("pageerror", lambda e: errors.append(str(e)))
    ph.on("dialog", lambda d: d.accept())
    ph.context.add_cookies(pg.context.cookies())
    ph.goto(pg.url)
    ph.wait_for_timeout(2000)
    while ph.locator("#critical-dialog[open]").count():      # the open critical alarms pop up on a fresh screen
        ph.keyboard.press("Escape"); ph.wait_for_timeout(300)
    ph.click(".site-item")
    ph.click("[data-rules]")
    ph.wait_for_selector("[data-rule-new]", timeout=8000)
    ph.click("[data-rule-new]")
    ph.wait_for_selector(".rx [data-x=event-type]", timeout=15000)
    ph.select_option(".rx [data-x=event-type]", "analytics")
    over = ph.evaluate("() => { const d = document.querySelector('.rules-dialog'); return d.scrollWidth - d.clientWidth; }")
    check(over <= 1, f"phone (390 px): the editor fits the screen (overflow {over} px)")
    ph.screenshot(path=f"{out}/rule-editor-phone.png", full_page=True)
    check(not errors, f"no page errors {errors}")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

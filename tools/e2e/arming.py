"""Arming end to end: disarm/arm from the UI, suppression, #24h and system alarms while disarmed,
the disarm timer, a scheduled change, and the schedule editor. Takes about 3 minutes (waits for real clock minutes).

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.arming
"""
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

from tools.e2e.common import COUNT_TONES, PORTAL, inject, login_and_add_site

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def call(pg, method: str, path: str, body=None) -> dict:
    return pg.evaluate("""async ([m, p, b]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t},
                                  body: b ? JSON.stringify(b) : undefined});
        return {status: r.status, body: await r.json()}; }""", [method, path, body])


def count(pg, sel: str) -> int:
    return pg.locator(sel).count()


def wait_more(pg, sel: str, n: int, timeout=10000) -> float:
    t = time.time()
    pg.wait_for_function(f"document.querySelectorAll('{sel}').length > {n}", timeout=timeout)
    return round((time.time() - t) * 1000)


def site_body(site: dict, **kw) -> dict:
    keys = ("name", "host", "nx_user", "address", "lat", "lng", "notes", "timezone")
    return {**{k: site[k] for k in keys}, "connect": False, **kw}


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(timezone_id="UTC")
    ctx.add_init_script(COUNT_TONES)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(3000)
    site = call(pg, "GET", "/api/sites")["body"][0]
    check(site["arming"]["armed"] and site["arming"]["source"] == "default", "new site starts armed")

    # --- 1. disarm from the map's site panel
    pg.click(".site-item")
    pg.click("#site-detail [data-arm=disarm]")
    pg.wait_for_selector("#arm-dialog[open]")
    opts = pg.eval_on_selector_all("#arm-duration option", "os => os.map(o => o.textContent)")
    check("Only when someone re-arms it" in opts and pg.input_value("#arm-duration") == "240",
          f"disarm dialog defaults to 4 h without a schedule ({len(opts)} choices)")
    pg.select_option("#arm-duration", "30")
    pg.fill("#arm-note", "E2E: cleaning crew")
    pg.click("#arm-submit")
    pg.wait_for_selector(".site-item .chip.disarmed", timeout=5000)
    check(count(pg, ".pin.disarmed") == 1, "map pin marked DISARMED")
    check("re-arms" in pg.inner_text("#site-detail .arm-line"), "site panel shows the re-arm time: "
          + pg.inner_text("#site-detail .arm-line"))

    # --- 2. security event while disarmed: stored, not raised, silent
    pg.wait_for_timeout(500)
    osc0, feed0 = pg.evaluate("window.__osc"), count(pg, ".feed-item")
    inject("line")
    pg.wait_for_timeout(3000)
    check(count(pg, ".feed-item") == feed0 and pg.evaluate("window.__osc") == osc0,
          "line crossing while disarmed: no feed card, no sound")
    stored = call(pg, "GET", "/api/alarms?state=disarmed")["body"]
    check(len(stored) == 1 and stored[0]["event_type"] == "analytics", "…and it is stored as 'while disarmed'")

    # --- 3. system alarm and #24h rule still raise while disarmed (latency measured)
    n = count(pg, ".feed-item.p3")
    inject("dock")
    ms = wait_more(pg, ".feed-item.p3", n)
    check(True, f"camera-disconnected warning while disarmed raised in {ms} ms")
    t = time.time()
    inject("panic24")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    check(True, f"#24h panic while disarmed popped up in {round((time.time() - t) * 1000)} ms")
    pg.keyboard.press("Escape")

    # --- 4. arm again: alarms raise at normal speed
    pg.click("#site-detail [data-arm=arm]")
    pg.wait_for_selector(".site-item .chip.disarmed", state="detached", timeout=5000)
    n = count(pg, ".feed-item.p2")
    inject("line")
    ms = wait_more(pg, ".feed-item.p2", n)
    check(ms < 1000, f"armed again: line crossing on screen in {ms} ms")

    # --- 5. disarm timer runs out -> re-armed by itself, browser updated live
    r = call(pg, "POST", f"/api/sites/{site['id']}/disarm", {"minutes": 1, "note": "E2E timer"})
    until = r["body"]["arming"]["until_ms"] / 1000
    pg.wait_for_selector(".site-item .chip.disarmed", timeout=5000)
    pg.wait_for_selector(".site-item .chip.disarmed", state="detached", timeout=75000)
    check(True, f"1-minute disarm timer re-armed the site, shown {time.time() - until:.1f} s after it ran out")

    # --- 6. scheduled disarm at the next whole minute (UTC)
    now = datetime.now(timezone.utc)
    at = (now + timedelta(minutes=2 if now.second > 45 else 1)).replace(second=0, microsecond=0)
    sched = [{"action": "disarm", "time": at.strftime("%H:%M"), "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}]
    r = call(pg, "PUT", f"/api/sites/{site['id']}", site_body(site, timezone="UTC", arm_schedule=sched))
    a = r["body"]["arming"]
    check(r["status"] == 200 and a["armed"] and a["next_ms"] == int(at.timestamp() * 1000),
          f"schedule saved without changing state; next: disarm at {at:%H:%M} UTC")
    pg.wait_for_selector(".site-item .chip.disarmed", timeout=150000)
    check(True, f"scheduled disarm shown live {time.time() - at.timestamp():.1f} s after {at:%H:%M:%S}")
    audit = call(pg, "GET", f"/api/audit?site_id={site['id']}&action=site.disarmed")["body"]
    check(audit[0]["detail"].get("source") == "schedule", "audit log: site.disarmed by schedule")

    # --- 7. schedule editor round trip
    pg.goto(f"{PORTAL}/sites/{site['id']}/edit")
    check(count(pg, ".sched-row") == 1 and pg.input_value("#timezone") == "UTC", "site form shows the saved schedule")
    pg.click("#sched-preset")
    pg.click("#save-btn")
    pg.wait_for_url(f"{PORTAL}/sites")
    s2 = call(pg, "GET", "/api/sites")["body"][0]
    check([e["action"] for e in s2["arm_schedule"]] == ["disarm", "arm"] and not s2["arming"]["armed"],
          "business-hours preset saved; the site stayed disarmed (no flip on save)")
    pg.click("[data-arm=arm]")
    pg.wait_for_selector("td .chip.armed", timeout=5000)
    check(True, "armed from the Sites page")

    check(not errors, f"no JavaScript errors ({errors[:3]})")
    b.close()

print(f"\n{len(failures)} failure(s)" if failures else "\nall arming checks passed")
raise SystemExit(1 if failures else 0)

"""Texts when an operator confirms an alarm (a mobile guard), end to end against the fake Twilio in tools/fake_nx.py.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.sms_confirmed [screenshot_dir]

A guard set to "Only when an operator confirms it's real" gets nothing when the alarm goes off, gets a
CONFIRMED text (with the operator's note and a link that shows it confirmed) when the operator clicks Real
event in the critical pop-up, and a STAND DOWN text when it's changed to False alarm. Timed: alarm to the
screen, click to the text reaching Twilio, and the acknowledgement and the next alarm with Twilio hanging.
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
GUARD = "+15705550301"
SID = "AC" + "1" * 32
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def texts() -> list[dict]:
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/_twilio"))


def twilio_mode(mode: str) -> None:
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_twilio/mode/{mode}", method="POST"))


def wait_texts(n: int, timeout=10.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        t = texts()
        if len(t) >= n:
            return t
        time.sleep(0.05)
    return texts()


def twilio_reply(frm: str, body: str) -> int:
    """A text someone sends to our number, signed as Twilio would (dev Auth Token)."""
    params = {"From": frm, "To": "+15705550000", "Body": body, "MessageSid": "SMreply", "AccountSid": SID}
    url = f"{PORTAL}/twilio/sms"
    sig = base64.b64encode(hmac.new(b"dev-token", (url + "".join(k + v for k, v in sorted(params.items()))).encode(),
                                    hashlib.sha1).digest()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers={"X-Twilio-Signature": sig})
    return urllib.request.urlopen(req).status


def alarm_on_screen(pg, kind: str) -> float:
    """Inject an alarm and time it to the feed (ms)."""
    before = pg.locator(".feed-item").count()
    t0 = time.time()
    inject(kind)
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {before}", timeout=10000, polling=10)
    return (time.time() - t0) * 1000

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    api = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
        const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
        return [r.status, await r.json()]; }"""
    st, _ = pg.evaluate(api, ["/api/platform/settings/email", "PUT", {"enabled": False, "portal_url": PORTAL}])
    st2, _ = pg.evaluate(api, ["/api/platform/settings/sms", "PUT", {"enabled": True, "sid": SID, "token": "dev-token",
                                                                      "sender": "+15705550000"}])
    check(st == 200 and st2 == 200, "portal address and Twilio set up")

    # --- 1. Users page: add the guard with "Only when an operator confirms it's real"
    pg.goto(f"{PORTAL}/users")
    pg.click("#add-user"); pg.wait_for_selector("#add-dialog[open]")
    pg.fill("#u-email", "guard@twgsecurity.com"); pg.fill("#u-name", "Patrol Guard"); pg.fill("#u-phone", "570 555 0301")
    pg.select_option("#u-when", "confirmed")
    pg.screenshot(path=f"{out}/conf1-add-guard.png")
    pg.click("#u-submit")
    pg.wait_for_selector("#u-link .alert", timeout=8000)
    pg.click("#u-cancel")
    check(wait_texts(1)[-1]["to"] == GUARD, "adding the guard texts the opt-in request")
    check(twilio_reply(GUARD, "YES") == 200, "guard replied YES")
    wait_texts(2)
    pg.reload(); pg.wait_for_selector(".users-table .chip:has-text('Only when confirmed')", timeout=5000)
    check("Any level" in pg.inner_text(".users-table"), "Users table: 'Any level' + 'Only when confirmed'")
    pg.click(".users-table tr:has-text('Patrol Guard') [data-edit]"); pg.wait_for_selector("#edit-dialog[open]")
    check(pg.input_value("#ed-when") == "confirmed", "Edit dialog shows Send = only when confirmed")
    pg.screenshot(path=f"{out}/conf2-edit-guard.png")
    pg.click("#ed-cancel")

    # --- 2. The alarm goes off: no text to the guard
    pg.goto(f"{PORTAL}/"); pg.wait_for_timeout(1500)
    n0 = len(texts())
    ms = alarm_on_screen(pg, "panic")
    check(ms < 1000, f"alarm on screen in {ms:.0f} ms")
    pg.wait_for_selector("#critical-dialog[open]", timeout=5000)
    time.sleep(1.5)
    check(len(texts()) == n0, "the guard gets nothing when the alarm goes off")

    # --- 3. Operator clicks Real event with a note: CONFIRMED text
    pg.fill("#crit-note", "Person forcing the back door, police called")
    t0 = time.time()
    pg.click('#critical-dialog [data-verdict="real"]')
    t = wait_texts(n0 + 1)
    took = (time.time() - t0) * 1000
    check(len(t) == n0 + 1 and t[-1]["to"] == GUARD, f"CONFIRMED text reached Twilio {took:.0f} ms after the click")
    # It carries the GIF (2026-10-14), made from the seconds around the alarm: confirmed this soon after the alarm, it
    # waits for NX to record them; never past alarm_gif.GIF_BUDGET_S (6 s), then it goes with the still.
    check(took < 7500, f"confirmed text within 7.5 s of the click ({took:.0f} ms)")
    body = t[-1]["body"]
    print(f"      text: {body!r}")
    lines = body.split("\n")
    check(lines[0].startswith("TWG CONFIRMED: ") and any(l.startswith("Confirmed real by ") for l in lines)
          and "Note: Person forcing the back door, police called" in lines, "text: CONFIRMED, who confirmed it, the note")
    check(t[-1]["media"].endswith("/alarm.gif"), f"confirmed text carries the GIF ({t[-1]['media'][-10:]})")
    link = lines[lines.index("VIDEO (alarm clip + live cameras):") + 1].strip()
    phone = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    m = phone.new_page()
    m.goto(link)
    check("confirmed real by" in m.inner_text(".link-title").lower() and "police called" in m.inner_text(".link-facts"),
          "guard's link shows Confirmed real and the operator's note")
    m.screenshot(path=f"{out}/conf3-guard-link.png", full_page=True)
    phone.close()

    # --- 4. Changed to False alarm: STAND DOWN
    alarm_id = pg.evaluate("fetch('/api/alarms?state=all&limit=1').then(r => r.json()).then(a => a[0].id)")
    st, r = pg.evaluate(api, ["/api/alarms/bulk", "POST", {"ids": [alarm_id], "verdict": "false", "note": "keyholder"}])
    t = wait_texts(n0 + 2)
    check(st == 200 and len(t) == n0 + 2 and t[-1]["to"] == GUARD and t[-1]["body"].startswith("TWG STAND DOWN: "),
          "False alarm afterwards -> STAND DOWN text to the guard")
    print(f"      text: {t[-1]['body']!r}")

    # --- 5. Twilio hangs: the acknowledgement and the next alarm are still instant
    twilio_mode("hang")
    pg.goto(f"{PORTAL}/"); pg.wait_for_timeout(1500)
    ms = alarm_on_screen(pg, "panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=5000)
    t0 = time.time()
    pg.click('#critical-dialog [data-verdict="real"]')
    pg.wait_for_selector("#critical-dialog[open]", state="detached", timeout=5000)
    ack_ms = (time.time() - t0) * 1000
    check(ack_ms < 1000, f"Twilio hanging: Real event closes the pop-up in {ack_ms:.0f} ms")
    times = [alarm_on_screen(pg, "line") for _ in range(3)]
    check(max(times + [ms]) < 1000, f"Twilio hanging: alarms on screen {[round(x) for x in [ms, *times]]} ms")
    twilio_mode("ok")
    check(not errors, f"no script errors {errors}")
    b.close()

print(f"\n{'ALL PASSED' if not fails else f'{len(fails)} FAILED'}")
sys.exit(1 if fails else 0)

"""Dispatch, guards sharing their location from a phone browser, Next / oldest-first, offline and history, end to
end against the fake NX + fake Twilio in tools/fake_nx.py.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.dispatch [screenshot_dir]
    Safari's engine (WebKit) from the Playwright image (the host lacks WebKit's libraries):
    docker run --rm --network host -e E2E_BROWSER=webkit -v $PWD:/repo:ro -v /tmp:/out -w /repo \
      mcr.microsoft.com/playwright/python:v1.63.0-noble sh -c "pip -q install pillow && python -m tools.e2e.dispatch /out"

Two guards without routers are added on the Sites page; each gets a text with a location link, opens it on a
(simulated) phone with GPS and taps Start sharing; their markers appear on the operator's map. Three alarms: the
feed lists the critical first, then the oldest first; Next walks through them in that order. The alarm details
list the guards closest first; Dispatch texts the closest. Real event texts only the closest guard automatically.
A guard stopping shows OFFLINE at the last position; History draws where they were. Alarms are timed throughout.
"""
import base64
import hashlib
import os
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

from tools.e2e.common import FAKE_NX, PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
SID = "AC" + "1" * 32
SITE = (40.2665, -76.8838)
GUARDS = {"near": ("+15705550401", "Ana Near", 120), "far": ("+15705550402", "Ben Far", 900)}      # metres north of the site
fails = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def texts() -> list[dict]:
    return json.load(urllib.request.urlopen(f"{FAKE_NX}/_twilio"))


def wait_texts(n: int, timeout=10.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        t = texts()
        if len(t) >= n:
            return t
        time.sleep(0.05)
    return texts()


def twilio_reply(frm: str, body: str) -> int:
    params = {"From": frm, "To": "+15705550000", "Body": body, "MessageSid": "SMreply", "AccountSid": SID}
    url = f"{PORTAL}/twilio/sms"
    sig = base64.b64encode(hmac.new(b"dev-token", (url + "".join(k + v for k, v in sorted(params.items()))).encode(),
                                    hashlib.sha1).digest()).decode()
    return urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                                                         headers={"X-Twilio-Signature": sig})).status


def alarm_on_screen(pg, kind: str) -> float:
    before = pg.locator(".feed-item").count()
    t0 = time.time()
    inject(kind)
    pg.wait_for_function(f"document.querySelectorAll('.feed-item').length > {before}", timeout=10000, polling=10)
    return (time.time() - t0) * 1000


def link_after(body: str, label: str) -> str:
    lines = body.split("\n")
    return lines[lines.index(label) + 1].strip()


API = """async ([url, method, body]) => { const t = document.querySelector('meta[name=csrf-token]').content;
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': t}, body: body ? JSON.stringify(body) : undefined});
    return [r.status, await r.json()]; }"""

with sync_playwright() as p:
    engine = os.environ.get("E2E_BROWSER", "chromium")
    b = getattr(p, engine).launch(**({"args": ["--autoplay-policy=no-user-gesture-required"]} if engine == "chromium" else {}))   # E2E_BROWSER=webkit: Safari's engine
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    # every tone the operator's page plays (sound.js), by frequency: the guard-update chime is 1319 then 1760 Hz
    ctx.add_init_script("""(() => { window.__tones = []; const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
      const make = AC.prototype.createOscillator; AC.prototype.createOscillator = function () { const o = make.call(this);
        const set = o.frequency.setValueAtTime.bind(o.frequency); o.frequency.setValueAtTime = (v, t) => { window.__tones.push(Math.round(v)); return set(v, t); };
        return o; }; })();""")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(f"operator: {e}"))
    pg.on("dialog", lambda d: d.accept())
    login_and_add_site(pg)
    pg.wait_for_timeout(2500)
    st, _ = pg.evaluate(API, ["/api/platform/settings/email", "PUT", {"enabled": False, "portal_url": PORTAL}])
    st2, _ = pg.evaluate(API, ["/api/platform/settings/sms", "PUT", {"enabled": True, "sid": SID, "token": "dev-token", "sender": "+15705550000"}])
    check(st == 200 and st2 == 200, "portal address and Twilio set up")
    for key, (phone, name, _) in GUARDS.items():
        s, r = pg.evaluate(API, ["/api/users", "POST", {"email": f"{key}@twgsecurity.com", "display_name": name, "password": "Guard-password-123!",
                                                        "phone": phone, "sms_level": "alarm", "sms_when": "confirmed"}])
        check(s == 200, f"guard {name} added ({s})")
        wait_texts(len(texts()) + 1, 3)
        twilio_reply(phone, "YES")
    time.sleep(0.5)

    # ---- 1. Sites page: add both as phone guards; each is texted a location link
    pg.goto(f"{PORTAL}/sites")
    pg.wait_for_selector("#gt-person option[value]:not([value=''])", state="attached", timeout=10000)
    links = {}
    for key, (phone, name, _) in GUARDS.items():
        n0 = len(texts())
        pg.select_option("#gt-person", label=next(o for o in pg.eval_on_selector_all("#gt-person option", "os => os.map(o => o.label)") if o.startswith(name)))
        pg.click("#gt-add-phone")
        pg.wait_for_function("document.querySelector('#gt-phone-result .alert')?.textContent.includes('Link texted')", timeout=8000)
        t = wait_texts(n0 + 1)
        check(t[-1]["to"] == phone and "SHARE YOUR LOCATION" in t[-1]["body"], f"{name} gets a text with the location link")
        links[key] = link_after(t[-1]["body"], "SHARE YOUR LOCATION (open, tap Start, keep it open):")
    check(pg.locator("#gt-trackers tr:has-text('Phone (browser)')").count() == 2, "both listed as Phone (browser) guards")
    check(pg.is_checked("#gt-auto"), "auto-dispatch on confirm is on by default")
    pg.screenshot(path=f"{out}/dispatch1-sites.png", full_page=True)

    # ---- 2. The guards open their links on phones with GPS and tap Start sharing
    phones = {}
    for key, (phone, name, north_m) in GUARDS.items():
        pc = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, permissions=["geolocation"],
                           geolocation={"latitude": SITE[0] + north_m / 111320, "longitude": SITE[1], "accuracy": 8})
        ph = pc.new_page()
        ph.on("pageerror", lambda e, n=name: errors.append(f"{n}'s phone: {e}"))
        ph.goto(links[key])
        check(name in ph.inner_text("h1"), f"{name}'s link opens the sharing page")
        ph.click("#share-start")
        ph.wait_for_function("document.getElementById('share-state').textContent.includes('Sharing your location')", timeout=10000)
        phones[key] = (pc, ph)
    phones["near"][1].screenshot(path=f"{out}/dispatch2-phone-sharing.png")
    check(True, "both phones are sharing")

    pg.goto(f"{PORTAL}/")
    pg.wait_for_function("document.querySelectorAll('.guard-icon .guard').length >= 2", timeout=10000)
    check("Ana Near" in pg.inner_text("#guard-list") and "Ben Far" in pg.inner_text("#guard-list"), "both phone guards on the operator's map")
    ms = alarm_on_screen(pg, "line")
    check(ms < 1000, f"alarm on screen in {ms:.0f} ms with two phones sharing")

    # ---- 3. Queue order: critical first, then oldest first; Next follows it
    time.sleep(1.1)
    alarm_on_screen(pg, "line")
    time.sleep(1.1)
    inject("panic")
    pg.wait_for_selector("#critical-dialog[open]", timeout=10000)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(400)
    feed = pg.eval_on_selector_all(".feed-item", "els => els.map(e => [Number(e.dataset.id), e.classList.contains('p1')])")
    check(feed[0][1] and [i for i, _ in feed[1:]] == sorted(i for i, _ in feed[1:]), f"feed: critical first, then oldest first {feed}")
    first_line = feed[1][0]
    pg.click(f".feed-item[data-id='{first_line}'] [data-detail]")
    pg.wait_for_selector("#drawer:not([hidden]) #drawer-next", timeout=5000)
    check("2 more open" in pg.inner_text("#drawer-next"), "Next shows how many more are open")

    # ---- 4. Real event / False alarm / Dismiss under the title; no Dispatch until it's verified
    pg.wait_for_selector(".ack-auto-hint", timeout=8000)
    tops = pg.evaluate("""() => ['[data-ack=real]', '.drawer-media'].map(q => document.querySelector('#drawer ' + q).getBoundingClientRect().top)""")
    check(tops[0] < tops[1] and tops[0] < 300, f"verdict buttons at the top, above the video ({tops[0]:.0f} px < {tops[1]:.0f} px)")
    check(pg.locator("#drawer [data-dispatch]").count() == 0 and pg.locator(".drawer-dispatch:not([hidden])").count() == 0,
          "not verified: no Dispatch buttons and no guard list")
    pg.screenshot(path=f"{out}/dispatch3-drawer-unverified.png")
    # Real event: the closest guard is texted automatically, then the guards and Dispatch buttons appear.
    n0 = len(texts())
    t0 = time.time()
    pg.click('#drawer [data-ack="real"]')
    t = wait_texts(n0 + 1)
    took = (time.time() - t0) * 1000
    body = t[-1]["body"]
    check(t[-1]["to"] == GUARDS["near"][0] and body.startswith("TWG DISPATCH:"), f"Real event: closest guard's dispatch text at Twilio {took:.0f} ms after the click")
    # The dispatch MMS carries the moving picture (GIF with the analytics boxes), not the still.
    media = t[-1]["media"]
    check(media.endswith("/alarm.gif"), f"dispatch text is an MMS with the alarm GIF ({media[-30:]!r})")
    if media.endswith("/alarm.gif"):
        from PIL import Image
        import io
        t1 = time.time()
        gif = urllib.request.urlopen(media).read()
        g = Image.open(io.BytesIO(gif))
        n_frames, size = getattr(g, "n_frames", 1), g.size
        orange = 0
        for i in range(n_frames):
            g.seek(i)
            px = g.convert("RGB").resize((160, round(160 * size[1] / size[0])))
            orange += any(abs(r - 224) < 40 and abs(gg - 138) < 40 and b_ < 90 for r, gg, b_ in px.getdata())
        check(gif[:6] == b"GIF89a" and len(gif) <= 600_000 and n_frames >= 8,
              f"GIF: {n_frames} frames, {size[0]}x{size[1]}, {len(gif) // 1024} KB, fetched in {(time.time() - t1) * 1000:.0f} ms")
        check(orange >= n_frames // 2, f"GIF: the orange analytics box is drawn ({orange} of {n_frames} frames)")
        open(f"{out}/dispatch-alarm.gif", "wb").write(gif)
    ms = alarm_on_screen(pg, "line")
    check(ms < 1000, f"alarm on screen in {ms:.0f} ms right after a GIF was made")
    pg.wait_for_selector(".drawer-dispatch:not([hidden]) .dispatch-list li", timeout=8000)
    rows = pg.eval_on_selector_all(".dispatch-list li", "els => els.map(e => e.innerText)")
    check(rows[0].startswith("Ana Near") and "Closest" in rows[0] and "Ben Far" in rows[1], f"verified: guards closest first: {rows}")
    pg.wait_for_selector(".dispatch-quick .dispatch-big", timeout=8000)
    big = pg.eval_on_selector_all(".dispatch-quick .dispatch-big", "els => els.map(e => [e.innerText, e.getBoundingClientRect().height])")
    check(len(big) == 2 and big[0][0].startswith("Dispatch Ana Near") and "closest" in big[0][0] and big[1][0].startswith("Dispatch Ben Far")
          and big[0][1] >= 44, f"verified: two big Dispatch buttons under the title: {big}")
    pg.screenshot(path=f"{out}/dispatch3-drawer-guards.png")
    n0 = len(texts())
    t0 = time.time()
    pg.click(".dispatch-quick .dispatch-big:not(.btn-primary)")
    t = wait_texts(n0 + 1)
    took = (time.time() - t0) * 1000
    check(t[-1]["to"] == GUARDS["far"][0] and t[-1]["body"].startswith("TWG DISPATCH:"), f"Dispatch Ben Far: text reached Twilio {took:.0f} ms after the click")
    check("away)" in body and "SHARE YOUR LOCATION" in body and "MAP (directions to the site):" in body,
          "dispatch text: distance, directions, the location link again")
    print("      text:", repr(body))
    pg.wait_for_selector(".mdt-board .mdt-row", timeout=8000)
    check("Ana Near" in pg.inner_text(".mdt-board"), "the alarm shows who was dispatched (status board)")

    # Ana opens the alarm link from her dispatch text: "Share my location now" at the top.
    alarm_page = phones["near"][0].new_page()
    alarm_page.on("pageerror", lambda e: errors.append(f"Ana's alarm page: {e}"))
    alarm_page.goto(link_after(body, "VIDEO (alarm clip + live cameras):"))
    alarm_page.wait_for_selector("#link-share-start", timeout=8000)
    top = alarm_page.eval_on_selector("#link-share-start", "e => e.getBoundingClientRect().top")
    check(top < 400, f"the alarm page from the dispatch text: status buttons + Share my location now at the top ({top:.0f} px)")
    pg.wait_for_function("document.querySelector('.mdt-board').innerText.includes('Opened the link')", timeout=8000)
    check(True, "dispatcher sees Ana opened the link (live)")

    # MDT: En route (also starts sharing), then On scene; the dispatcher's board and timeline follow live.
    chimes = lambda: pg.evaluate("(() => { const t = window.__tones; let n = 0; for (let i = 0; i + 1 < t.length; i++) if (t[i] === 1319 && t[i + 1] === 1760) n++; return n; })()")
    for st, word in (("en_route", "En route"), ("on_scene", "On scene")):
        c0 = chimes()
        t0 = time.time()
        alarm_page.click(f".mdt-btn[data-status='{st}']")
        pg.wait_for_selector(f".mdt-board .mdt-chip--{st}", timeout=8000)
        ms_live = (time.time() - t0) * 1000
        check(ms_live < 3000, f"Ana taps {word}: on the dispatcher's board in {ms_live:.0f} ms")
        # an active call: the update chime (not an alarm sound), a toast at the bottom right, the Active calls card
        pg.wait_for_function(f"document.querySelector('#calls-list .call-latest')?.innerText.includes('{word}')", timeout=8000)
        ms_card = (time.time() - t0) * 1000
        pg.wait_for_function(f"(() => {{ const t = window.__tones; let n = 0; for (let i = 0; i + 1 < t.length; i++) if (t[i] === 1319 && t[i + 1] === 1760) n++; return n; }})() > {c0}", timeout=5000)
        ms_chime = (time.time() - t0) * 1000
        toast_txt = pg.inner_text(".toast.call >> nth=-1")
        check(word in toast_txt and "Ana Near" in toast_txt, f"{word}: update chime {ms_chime:.0f} ms, Active calls card {ms_card:.0f} ms, toast {toast_txt.splitlines()[0]!r}")
    pg.screenshot(path=f"{out}/dispatch7-call-update.png")
    # The operator's own note: on the timeline, but no chime (only guards' updates chime).
    c0 = chimes()
    pg.fill("#note-text", "Keyholder called")
    pg.click("#note-add")
    pg.wait_for_function("document.querySelector('.drawer-timeline').innerText.includes('Keyholder called')", timeout=8000)
    pg.wait_for_timeout(1200)
    check(chimes() == c0, "the operator's own note: no chime")
    # Ana writes a note from her alarm page: chime, toast, card and the alarm's notes + timeline.
    alarm_page.fill("#link-note-text", "Gate chained, nobody on site")
    t0 = time.time()
    alarm_page.click("#link-note-send")
    pg.wait_for_function("document.querySelector('#calls-list .call-latest')?.innerText.includes('Gate chained')", timeout=8000)
    ms_card = (time.time() - t0) * 1000
    pg.wait_for_function(f"(() => {{ const t = window.__tones; let n = 0; for (let i = 0; i + 1 < t.length; i++) if (t[i] === 1319 && t[i + 1] === 1760) n++; return n; }})() > {c0}", timeout=5000)
    pg.wait_for_function("document.querySelector('.drawer .notes-list').innerText.includes('Gate chained')", timeout=5000)
    alarm_page.wait_for_function("document.getElementById('link-notes-list').innerText.includes('Gate chained')", timeout=5000)
    check("Guard note" in pg.inner_text(".toast.call >> nth=-1") and "Guard note" in pg.inner_text(".drawer-timeline")
          and alarm_page.input_value("#link-note-text") == "",
          f"Ana's note: on the dispatcher's card in {ms_card:.0f} ms with the chime; in the alarm's notes and timeline; listed on her page")
    alarm_page.screenshot(path=f"{out}/dispatch7-phone-note.png", full_page=True)
    alarm_page.wait_for_function("document.getElementById('link-share-state').textContent.includes('Sharing your location')", timeout=10000)
    check(True, "En route started sharing her location")
    alarm_page.screenshot(path=f"{out}/dispatch6-phone-alarm-share.png")       # left open: it shares too
    tl = pg.inner_text(".drawer-timeline")
    check(all(w in tl for w in ("Dispatched Ana Near", "Dispatch text", "Opened the alarm link", "En route", "On scene")),
          f"timeline: dispatched, text, opened, en route, on scene {'' if all(w in tl for w in ('Dispatched Ana Near', 'Dispatch text', 'Opened the alarm link', 'En route', 'On scene')) else tl[:1500]!r}")
    pg.screenshot(path=f"{out}/dispatch8-board-timeline.png", full_page=True)

    # All guards: tick "All guards", Dispatch selected.
    n0 = len(texts())
    pg.check("[data-choose-all]")
    pg.wait_for_function("document.querySelector('[data-dispatch-chosen]')?.textContent.includes('(2)')", timeout=5000)
    pg.click("[data-dispatch-chosen]")
    t = wait_texts(n0 + 2)
    check(sorted(x["to"] for x in t[n0:]) == sorted(v[0] for v in GUARDS.values()), "All guards → Dispatch selected texts both")

    # An operator on a phone: the two big Dispatch buttons are the first thing under the alarm.
    mob = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    mp = mob.new_page()
    mp.on("pageerror", lambda e: errors.append(f"operator phone: {e}"))
    mp.goto(f"{PORTAL}/login")
    mp.fill("#email", "admin@twgsecurity.com"); mp.fill("#password", "Smoke-test-password-123!")
    mp.click("button[type=submit]"); mp.wait_for_url(f"{PORTAL}/")
    mp.wait_for_selector(f"[data-call-open='{first_line}']", state="attached", timeout=10000)
    mp.evaluate("id => document.querySelector(`[data-call-open='${id}']`).click()", first_line)
    mp.wait_for_selector(".dispatch-quick .dispatch-big", timeout=10000)
    box = mp.eval_on_selector(".dispatch-quick .dispatch-big", "e => { const r = e.getBoundingClientRect(); return [r.top, r.width, r.height]; }")
    check(box[0] < 844 and box[1] > 300 and box[2] >= 44, f"phone: a full-width Dispatch button on the first screen {box}")
    mp.screenshot(path=f"{out}/dispatch7-phone-operator.png")
    mob.close()
    # Phone, an open alarm: the three verdict buttons fit side by side on the first screen.
    mob = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    mp = mob.new_page()
    mp.goto(f"{PORTAL}/login")
    mp.fill("#email", "admin@twgsecurity.com"); mp.fill("#password", "Smoke-test-password-123!")
    mp.click("button[type=submit]"); mp.wait_for_url(f"{PORTAL}/")
    mp.wait_for_selector(".feed-item:not(.p1) [data-detail]", state="attached", timeout=10000)
    mp.wait_for_timeout(1000)
    while mp.locator("#critical-dialog[open]").count():      # the open critical pops up on a fresh screen
        mp.keyboard.press("Escape"); mp.wait_for_timeout(300)
    mp.evaluate("document.querySelector('.feed-item:not(.p1) [data-detail]').click()")
    mp.wait_for_selector("#drawer [data-ack=real]", timeout=10000)
    vb = mp.eval_on_selector_all("#drawer .ack-top [data-ack]", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.top, r.right, r.height]; })")
    mp.wait_for_timeout(1500)
    over = mp.evaluate("document.getElementById('drawer').scrollWidth - document.getElementById('drawer').clientWidth")
    if over > 1:
        print("      wide:", mp.evaluate("""() => { const d = document.getElementById('drawer'); return [...d.querySelectorAll('*')].filter(e => e.getBoundingClientRect().right > d.clientWidth + 1 && !e.closest('.leaflet-container')).slice(0, 8).map(e => e.tagName + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().right)); }"""))
    check(len(vb) == 2 and all(v[0] < 400 and v[1] <= 391 and v[2] >= 44 for v in vb) and len({round(v[0]) for v in vb}) == 1 and over <= 1,
          f"phone: Real event / False alarm side by side near the top {vb}, overflow {over}")
    dis = mp.eval_on_selector("#drawer [data-ack=dismissed]", "e => [e.closest('.ack-top') === null, e.getBoundingClientRect().height]")
    check(dis[0] and dis[1] < 40, f"Dismiss: small and further down, not with the verdict buttons {dis}")
    mp.screenshot(path=f"{out}/dispatch7b-phone-verdicts.png")
    mob.close()

    # End the first call so Active calls holds only the critical's later.
    n0 = len(texts())
    pg.fill("#call-note", "Checked, all clear")
    pg.click("#call-end")
    pg.wait_for_function("!document.querySelector('#call-end')", timeout=8000)
    wait_texts(n0 + 1)
    time.sleep(1.5)

    # Next from a handled alarm: the most urgent open one (the critical) first.
    pg.click("#drawer-next")
    pg.wait_for_function("document.querySelector('#drawer .chip.p1')", timeout=5000)
    check(pg.locator("#drawer [data-dispatch]").count() == 0, "Next → the critical; not verified, so no Dispatch yet")

    # ---- 5. Real event on the critical: only the closest guard is texted, automatically
    n0 = len(texts())
    t0 = time.time()
    pg.click('#drawer [data-ack="real"]')
    t = wait_texts(n0 + 1)
    took = (time.time() - t0) * 1000
    time.sleep(1.5)
    new = texts()[n0:]
    check(len(new) == 1 and new[0]["to"] == GUARDS["near"][0] and new[0]["body"].startswith("TWG DISPATCH:"),
          f"confirming texts only the closest guard, automatically ({took:.0f} ms after the click) {[(x['to'], x['body'][:20]) for x in new]}")
    # Real event + a dispatched guard: the alarm stays open as an active call until End call.
    pg.wait_for_selector("#drawer .call-box #call-end", timeout=8000)
    # The details fill the map's middle pane: site panel (left) and live alarms (right) stay in view (2026-10-14).
    w = pg.evaluate("""() => { const d = document.getElementById('drawer').getBoundingClientRect(), m = document.querySelector('.map-canvas').getBoundingClientRect();
        return [Math.round(d.left - m.left), Math.round(d.right - m.right), getComputedStyle(document.querySelector('.feed')).visibility, document.getElementById('drawer-backdrop').hidden]; }""")
    check(abs(w[0]) < 2 and abs(w[1]) < 2 and w[2] == "visible" and w[3] and "Back to map" in pg.inner_text("#drawer-close"),
          f"alarm details in the map pane, live alarms and site panel still in view, Back to map {w}")
    crit_id = pg.evaluate("Number(document.querySelector('#drawer [data-dispatch]')?.closest('#drawer') && 0) || null")
    pg.click("#drawer-close")
    pg.wait_for_selector("#calls:not([hidden]) .call-item", timeout=8000)
    check(pg.locator("#calls-list .call-item").count() == 1 and "Soft trigger" in pg.inner_text("#calls-list"),
          "map: the confirmed, dispatched alarm is under Active calls")
    pg.screenshot(path=f"{out}/dispatch9-active-calls.png")
    pg.click("#calls-list .call-item [data-call-open]")
    pg.wait_for_selector("#drawer .call-box #call-end", timeout=8000)
    pg.wait_for_selector(".drawer-timeline .timeline .tl", timeout=8000)
    tl = pg.eval_on_selector_all(".timeline .tl b", "els => els.map(e => e.textContent)")
    check(tl and tl[0] != "Event occurred in NX" and tl[-1] == "Event occurred in NX", f"timeline newest first ({tl[0]!r} … {tl[-1]!r})")
    pg.fill("#call-note", "Police took over, site secured")
    n0 = len(texts())
    pg.click("#call-end")
    pg.wait_for_function("!document.querySelector('#call-end')", timeout=8000)
    t = wait_texts(n0 + 1)
    check(t[-1]["to"] == GUARDS["near"][0] and t[-1]["body"].startswith("TWG CALL ENDED:") and "Police took over" in t[-1]["body"],
          "End call: the dispatched guard gets CALL ENDED with the note")
    pg.wait_for_function("document.querySelector('.timeline .tl b')?.textContent.startsWith('Call-ended text') || document.querySelector('.timeline .tl b')?.textContent === 'Call ended'", timeout=8000)
    check(True, "timeline: Call ended at the top")
    pg.click("#drawer-close")
    pg.wait_for_selector("#calls", state="hidden", timeout=8000)
    check(True, "Active calls empties once the call is ended")

    # ---- 6. Ana stops sharing: OFFLINE at her last position
    pos = pg.evaluate("[...document.querySelectorAll('.guard-icon .guard')].find(e => e.textContent.includes('Ana'))?.getBoundingClientRect().x")
    phones["near"][1].click("#share-stop")
    pg.wait_for_function("document.querySelector('#guard-list').innerText.includes('OFFLINE (stopped sharing)')", timeout=8000)
    pos2 = pg.evaluate("[...document.querySelectorAll('.guard-icon .guard')].find(e => e.textContent.includes('Ana'))?.getBoundingClientRect().x")
    check(pos2 is not None and abs((pos or 0) - pos2) < 2, "stopped sharing: shown OFFLINE, still at the last position")
    pg.screenshot(path=f"{out}/dispatch4-offline.png")

    # ---- 7. History from the server
    pg.click("[data-history]:near(:text('Ben Far'))") if False else pg.click("li:has-text('Ben Far') [data-history]")
    try:
        pg.wait_for_selector("[data-hist-slider]", timeout=8000)
    except Exception:
        print("      history panel:", repr(pg.inner_text("#guard-list")[-400:]))
        raise
    check("position" in pg.inner_text(".guard-history"), "History: Ben's path from the server, with a time slider")
    pg.screenshot(path=f"{out}/dispatch5-history.png")
    pg.click("[data-hist-close]")

    # Locate with no alarm: 📍 next to Ben texts him his location link.
    n0 = len(texts())
    pg.click("li:has-text('Ben Far') [data-locate]")
    t = wait_texts(n0 + 1)
    check(t[-1]["to"] == GUARDS["far"][0] and "asks where you are" in t[-1]["body"] and "/g/" in t[-1]["body"],
          "📍 Locate (no alarm): Ben is texted his location link")
    pg.wait_for_selector(".toast:has-text('Asked Ben Far')", timeout=5000)

    ms = alarm_on_screen(pg, "line")
    check(ms < 1000, f"alarm on screen in {ms:.0f} ms after all of it")
    check(not errors, f"no page errors {errors}")
    for pc, _ in phones.values():
        pc.close()
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

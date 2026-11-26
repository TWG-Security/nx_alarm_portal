"""Site VPN connections end to end (app/tunnels.py) with the stand-in tunnel programs (tools/fake_vpn: a real
SOCKS5 proxy that logs what it carries). Adds a WireGuard connection on the Sites page, moves the site onto it,
checks every NX call (HTTP and the live push websocket) goes through it, times alarms, then breaks the tunnel.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.vpn [screenshot_dir]
"""
import base64
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from tools.e2e.common import PORTAL, inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "."
fails = []
K1, K2 = base64.b64encode(b"a" * 32).decode(), base64.b64encode(b"b" * 32).decode()
UNIFI = f"[Interface]\nPrivateKey = {K1}\nAddress = 192.168.3.2/32\nDNS = 192.168.3.1\n\n[Peer]\nPublicKey = {K2}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 203.0.113.5:51820\n"


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


def arrive(pg, timeout=10000):
    n = pg.locator(".feed-item.p2").count()
    t = time.time(); inject("line")
    pg.wait_for_function(f"document.querySelectorAll('.feed-item.p2').length > {n}", timeout=timeout)
    return round((time.time() - t) * 1000)


def site(pg):
    return pg.evaluate("fetch('/api/sites').then(r => r.json()).then(s => s[0])")


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    login_and_add_site(pg)
    sid = site(pg)["id"]

    # 1. Sites page → VPN connections: a bad file is explained, the UniFi file is accepted and connects.
    pg.goto(f"{PORTAL}/sites#vpn")
    pg.wait_for_selector("#vpn-rows .empty")
    pg.fill("#vpn-name", "Broken"); pg.fill("#vpn-config", "[Interface]\nPrivateKey = nope\n")
    pg.click("#vpn-form button[type=submit]")
    pg.wait_for_selector("#vpn-error .alert")
    check("isn't a WireGuard key" in pg.inner_text("#vpn-error") or "needs" in pg.inner_text("#vpn-error"),
          f"a bad file is explained: {pg.inner_text('#vpn-error')!r}")
    pg.fill("#vpn-name", ""); pg.fill("#vpn-config", "")
    pg.set_input_files("#vpn-file", files=[{"name": "Warehouse UniFi.conf", "mimeType": "text/plain", "buffer": UNIFI.encode()}])
    pg.wait_for_function("document.getElementById('vpn-name').value === 'Warehouse UniFi'")
    pg.click("#vpn-form button[type=submit]")
    pg.wait_for_selector("#vpn-rows tr[data-id] b:text('Connected')", timeout=20000)
    row = pg.inner_text("#vpn-rows tr[data-id]")
    check("203.0.113.5:51820" in row and K1 not in pg.content(), f"connection listed, key never shown: {row.splitlines()[0]!r}")
    pg.screenshot(path=f"{out}/vpn-1-sites.png", full_page=True)

    # 2. Edit site → Connection: the site goes through it.
    pg.goto(f"{PORTAL}/sites/{sid}/edit")
    pg.wait_for_selector("#tunnel option:nth-child(2)", state="attached")
    pg.select_option("#tunnel", index=1)
    check("on the site's network" in pg.inner_text("#tunnel-hint"), "the form says to use the NX address on the site's LAN")
    pg.click("#test-btn")
    pg.wait_for_function("/Connected|failed|Could not/.test(document.getElementById('conn-result').textContent)", timeout=15000)
    check("Connected" in pg.inner_text("#conn-result"), f"Test connection through the VPN: {pg.inner_text('#conn-result')[:80]!r}")
    pg.screenshot(path=f"{out}/vpn-2-edit.png", full_page=True)
    pg.click("#save-btn")
    pg.wait_for_url(f"{PORTAL}/sites", timeout=15000)
    s = site(pg)
    check(s["tunnel"] and s["tunnel"]["name"] == "Warehouse UniFi", f"site saved with the connection: {s['tunnel']}")
    log = Path("tunnels-data") / f"wi-{s['tunnel']['id']}" / "connections.log"

    # 3. Alarms arrive by push through the tunnel (polling is 60 s).
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    for _ in range(40):
        if pg.evaluate(f"fetch('/api/sites').then(r => r.json()).then(s => s[0].push)") and log.exists():
            break
        time.sleep(0.25)
    lat = [arrive(pg) for _ in range(3)]
    check(max(lat) < 1000, f"alarms through the VPN on screen in {lat} ms")
    carried = log.read_text().splitlines()
    check(len([c for c in carried if c == "127.0.0.1:8199"]) >= 2, f"NX calls and the push websocket went through the tunnel ({len(carried)} connections)")

    # 4. The tunnel program dies: the portal restarts it and alarms come back by themselves.
    subprocess.run(["pkill", "-f", "fake.py wireproxy"], check=False)
    t = time.time()
    ms = arrive(pg, timeout=90000)
    check(True, f"tunnel program killed: next alarm on screen {round(time.time() - t, 1)} s later (restart + push reconnect)")

    # 5. Switched off: the site says why; switched on: back.
    pg.goto(f"{PORTAL}/sites#vpn")
    pg.wait_for_selector("#vpn-rows [data-enabled]")
    pg.uncheck("#vpn-rows [data-enabled]")
    pg.wait_for_selector("#vpn-rows b:text('Switched off')")
    t = time.time()
    for _ in range(120):
        s = site(pg)
        if s["status"] == "offline":
            break
        time.sleep(1)
    check(s["status"] == "offline" and "VPN" in s["status_detail"], f"site offline {round(time.time() - t)} s after: {s['status_detail'][:110]!r}")
    pg.check("#vpn-rows [data-enabled]")
    pg.wait_for_selector("#vpn-rows b:text('Connected')", timeout=20000)
    t = time.time()
    for _ in range(120):
        if site(pg)["status"] == "online":
            break
        time.sleep(1)
    check(site(pg)["status"] == "online", f"back online {round(time.time() - t)} s after switching it on")
    pg.goto(f"{PORTAL}/"); pg.mouse.click(700, 500)
    pg.wait_for_timeout(3000)
    lat = [arrive(pg) for _ in range(2)]
    check(max(lat) < 1000, f"alarms after the outage in {lat} ms")
    check(not errors, f"no page errors {errors[:2]}")
    b.close()

print("ALL PASS" if not fails else f"{len(fails)} FAILED")

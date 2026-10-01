"""Probe a running portal over its LAN address and its public (Cloudflare Tunnel) name at once.

    PROBE_EMAIL=... PROBE_PASS_FILE=... .venv/bin/python -m tools.e2e.prod_probe listen 150
    ... prod_probe disarm 1 5 | arm 1 | alarms disarmed 1

listen: opens the live-update stream (SSE) on both paths, logs every event's arrival, and prints
how long each alarm took on each path (vs the NX event time) plus the largest gap between pings.
Fire soft triggers in NX while it runs.
"""
import asyncio
import json
import os
import sys
import time

import httpx

BASES = {"lan": "https://10.1.10.97", "tunnel": "https://alarmportal.twgsecurity.net"}
EMAIL = os.environ.get("PROBE_EMAIL", "e2e-probe@twgsecurity.com")
PASSWORD = open(os.environ["PROBE_PASS_FILE"]).read().strip()


async def session(base: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(base_url=base, verify=False, timeout=30, follow_redirects=False)
    page = await c.get("/login")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = await c.post("/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": csrf})
    assert r.status_code == 303, f"{base}: login failed ({r.status_code})"
    home = await c.get("/")
    c.headers["X-CSRF-Token"] = home.text.split('name="csrf-token" content="')[1].split('"')[0]
    return c


async def stream(name: str, c: httpx.AsyncClient, seconds: float, log: list) -> None:
    event = None
    try:
        async with c.stream("GET", "/api/events", timeout=httpx.Timeout(30, read=40)) as r:
            log.append((time.time(), name, "open", {"status": r.status_code}))
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip() or "{}")
                    log.append((time.time(), name, event, data))
                elif line.startswith(":"):
                    log.append((time.time(), name, "comment", {}))
    except (httpx.HTTPError, asyncio.CancelledError) as exc:
        log.append((time.time(), name, "closed", {"error": exc.__class__.__name__}))


async def listen(seconds: float) -> None:
    clients = {n: await session(b) for n, b in BASES.items()}
    log: list = []
    tasks = [asyncio.create_task(stream(n, c, seconds, log)) for n, c in clients.items()]
    print(f"listening {seconds:.0f} s on {', '.join(BASES)}; fire triggers now", flush=True)
    await asyncio.sleep(seconds)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in clients.values():
        await c.aclose()

    for name in BASES:
        # The page's watchdog counts any message as a heartbeat (pings only fill 5 s of quiet).
        beats = [t for t, n, ev, _ in log if n == name and ev not in ("open", "closed")]
        gaps = [round(b - a, 2) for a, b in zip(beats, beats[1:])]
        opened = [d for t, n, ev, d in log if n == name and ev == "open"]
        closed = [d for t, n, ev, d in log if n == name and ev == "closed" and d.get("error") != "CancelledError"]
        print(f"{name}: stream {opened}, {len(beats)} messages, longest silence {max(gaps) if gaps else '-'} s"
              f"{', CLOSED ' + str(closed) if closed else ''}")
    alarms: dict[int, dict] = {}
    for t, n, ev, d in log:
        if ev == "alarm.new":
            alarms.setdefault(d["id"], {"caption": d["caption"], "site": d["site_name"], "ts": d["event_ts_ms"] / 1000})[n] = t
    for aid, a in alarms.items():
        row = "  ".join(f"{n}: {round((a[n] - a['ts']) * 1000)} ms after NX event" if n in a else f"{n}: NOT RECEIVED"
                        for n in BASES)
        diff = f"  tunnel - lan: {round((a['tunnel'] - a['lan']) * 1000)} ms" if "lan" in a and "tunnel" in a else ""
        print(f"alarm #{aid} {a['caption']} @ {a['site']}: {row}{diff}")
    if not alarms:
        print("no alarm.new events seen")


async def call(method: str, path: str, body=None) -> dict:
    c = await session(BASES["tunnel"])
    r = await c.request(method, path, json=body)
    await c.aclose()
    r.raise_for_status()
    return r.json()


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "listen":
        asyncio.run(listen(float(args[0]) if args else 120))
    elif cmd in ("arm", "disarm"):
        body = {"note": "E2E probe"} | ({"minutes": int(args[1])} if cmd == "disarm" and len(args) > 1 else {})
        s = asyncio.run(call("POST", f"/api/sites/{args[0]}/{cmd}", body))
        print(s["name"], json.dumps(s["arming"]))
    elif cmd == "alarms":
        rows = asyncio.run(call("GET", f"/api/alarms?state={args[0]}&site_id={args[1]}&limit=5"))
        for a in rows:
            print(a["id"], a["state"], a["event_type"], a["caption"], a["event_ts_ms"], a["received_at"])


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    main()

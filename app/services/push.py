"""Sub-second alarms: NX pushes new event-log entries over its JSON-RPC WebSocket.

    wss://<server>/jsonrpc?_ticket=<one-time ticket from POST /rest/v4/login/tickets>
    -> {"method": "rest.v4.events.log.subscribe", "params": {"startTimeMs": ...}}
    <- {"method": "rest.v4.events.log.update", "params": <event-log row>}   (per new event)

Verified against Nx 6.1.2 through the Nx Cloud relay: the update arrives within a
second of the event. The poller keeps running as a backstop, so nothing is lost while
this connection is down or reconnecting; duplicate rows are dropped by event key.
Over JSON-RPC, NX 6.1.2 ignores startTimeMs and answers the subscribe with its whole
event log (104k rows / 145 MB on the TWG site). `limit` is honoured, so we subscribe
with limit=1. NX still takes ~10 s server-side to set the subscription up; the poller
covers that window.
"""

import asyncio
import json
import logging
import ssl
import time

import websockets

from app.services.bus import bus

log = logging.getLogger("portal.push")

SUBSCRIBE = "rest.v4.events.log.subscribe"
UPDATE = "rest.v4.events.log.update"
MAX_BACKOFF_S = 30


def _ssl() -> ssl.SSLContext:
    # NX servers ship self-signed certificates; the MCP client doesn't verify either.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _set_connected(manager, rt, connected: bool) -> None:
    if rt.push_connected != connected:
        rt.push_connected = connected
        bus.publish(rt.tenant_id, "site.push", {"site_id": rt.site_id, "push": connected})


async def _session(manager, rt) -> None:
    client = rt.client
    if not client._token:
        await client._login()
    try:
        ticket = (await client._post("/rest/v4/login/tickets"))["token"]
    except Exception:  # token may have expired: log in again once
        await client._login()
        ticket = (await client._post("/rest/v4/login/tickets"))["token"]
    url = client.base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/jsonrpc?_ticket={ticket}"
    kwargs = {"ssl": _ssl()} if url.startswith("wss://") else {}
    async with websockets.connect(url, max_size=None, open_timeout=15, ping_interval=15, ping_timeout=15,
                                  **kwargs) as ws:
        start = int(time.time() * 1000) - 10_000
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": SUBSCRIBE,
                                  "params": {"limit": 1, "order": "desc"}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == UPDATE and isinstance(msg.get("params"), dict):
                row = msg["params"]
                lag = int(time.time() * 1000) - int(row.get("timestampMs") or 0)
                created = await manager.ingest_pushed(rt, [row])
                ev = row.get("eventData") or {}
                # Delivery metric: how long after its own timestamp NX pushed the event.
                # A large lag here is upstream of the portal (camera -> NX), not portal delay.
                log.info("site %s: pushed %s %s %d ms after its timestamp -> %s", rt.site_id, ev.get("type"),
                         (ev.get("eventTypeId") or "")[:60], lag,
                         f"new alarm {created[0].id}" if created else "no new alarm (duplicate or not an alarm)")
            elif msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"NX refused the event subscription: {msg['error']}")
                _set_connected(manager, rt, True)
                log.info("site %s: live push connected", rt.site_id)
                # Safety: only rows from our start time on, even if NX sent more history.
                rows = [r for r in (msg.get("result") or []) if int(r.get("timestampMs") or 0) >= start]
                if rows:
                    await manager.ingest_pushed(rt, rows)


async def run_push(manager, rt) -> None:
    backoff = 1
    while True:
        started = time.monotonic()
        try:
            await _session(manager, rt)
        except asyncio.CancelledError:
            _set_connected(manager, rt, False)
            raise
        except Exception as exc:  # noqa: BLE001 — any failure: fall back to polling and retry
            log.warning("site %s: live push down (%s: %s); polling continues", rt.site_id,
                        exc.__class__.__name__, str(exc)[:200])
        _set_connected(manager, rt, False)
        if time.monotonic() - started > 60:
            backoff = 1                                       # it was up for a while; retry fast
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_S)

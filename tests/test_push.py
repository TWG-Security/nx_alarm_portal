import asyncio
import json
import time

import httpx
import respx
import websockets
from sqlalchemy import select

from app.models import Alarm, Site
from app.services import push
from app.services.bus import bus
from app.services.poller import manager
from tests.conftest import make_site, nx_row


async def test_push_ingests_events_within_a_second(session, admin):
    """A stand-in for NX's JSON-RPC websocket: answer the subscribe, then push one event."""
    tenant, _ = admin
    subscribed = asyncio.Event()
    got_params = {}

    async def fake_nx(ws):
        req = json.loads(await ws.recv())
        got_params.update(req)
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": []}))
        subscribed.set()
        await asyncio.sleep(0.05)
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "rest.v4.events.log.update",
                                  "params": nx_row(int(time.time() * 1000), type_="softTrigger")}))
        await asyncio.sleep(5)

    server = await websockets.serve(fake_nx, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    host = f"http://127.0.0.1:{port}"
    site = await make_site(session, tenant, host=host)
    sub = bus.subscribe(tenant.id)
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{host}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
        r.post(f"{host}/rest/v4/login/tickets").mock(return_value=httpx.Response(200, json={"token": "tkt"}))
        r.get(f"{host}/rest/v4/events/rules").mock(return_value=httpx.Response(200, json=[]))
        r.route(host=f"127.0.0.1").pass_through()
        rt = manager._runtime(site)
        t0 = time.monotonic()
        task = asyncio.create_task(push.run_push(manager, rt))
        try:
            while time.monotonic() - t0 < 3:
                if (await session.scalar(select(Alarm).execution_options(populate_existing=True))):
                    break
                await asyncio.sleep(0.02)
            elapsed = time.monotonic() - t0
        finally:
            task.cancel()
            server.close()
    alarm = await session.scalar(select(Alarm))
    assert alarm is not None and alarm.event_type == "softTrigger" and elapsed < 1.0
    assert got_params["method"] == "rest.v4.events.log.subscribe" and got_params["params"]["limit"] == 1
    events = [sub.queue.get_nowait()[0] for _ in range(sub.queue.qsize())]
    assert "alarm.new" in events and "site.push" in events
    bus.unsubscribe(sub)


async def test_losing_a_site_raises_an_alarm_and_restore_is_noted(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    rt = manager._runtime(site)
    await manager._set_status(rt, "offline", "Timed out reaching the NX server")
    alarm = (await session.execute(select(Alarm).execution_options(populate_existing=True))).scalar_one()
    assert (alarm.event_type, alarm.priority, alarm.caption) == ("portalSiteOffline", 2, "Site connection lost")
    await manager._set_status(rt, "offline", "still down")          # no duplicate while it stays down
    assert len((await session.scalars(select(Alarm))).all()) == 1
    s = (await session.execute(select(Site).execution_options(populate_existing=True))).scalar_one()
    await manager._mark_restored(session, s)
    await session.refresh(alarm)
    assert "Connection restored" in alarm.description and alarm.state == "new"

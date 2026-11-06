"""Arming tells NX (app/services/arm_event.py): a site set up for it sends a Generic Event "Site armed" / "Site disarmed"
on every change (button, schedule, timer), after switching NX rules tagged #arm on / before switching them off; it is
retried when NX refuses, and the portal doesn't raise its own message as an alarm. Webhooks ticked "run when the site
arms / disarms" (e.g. a soft trigger) go with it; switching #arm rules is an option too (off = they stay on in NX).
NX is tools/fake_nx.py on a port."""
import asyncio
import copy
import time

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.services import arm_event, arm_rules, arming
from app.services.poller import ingest, manager
from tests.conftest import login, make_site
from tests.test_webhooks import nx  # noqa: F401 - fixture
from tools import fake_nx

ON = {"rules": True, "enabled": True, "source": "TWG Alarm Portal", "armed": "Site armed", "disarmed": "Site disarmed"}


@pytest.fixture(autouse=True)
def fresh():
    rules = copy.deepcopy(fake_nx.RULES)
    fake_nx.RULES["line"]["comment"] = "Siren on Site armed #arm"          # a reacting rule that is itself #arm
    fake_nx.NX_ORDER.clear()
    yield
    fake_nx.RULES.clear(); fake_nx.RULES.update(rules)
    fake_nx.RULE_MODE["write"] = "ok"


async def site_with_tasks(session, tenant, nx_url, cfg=ON):  # noqa: F811
    site = await make_site(session, tenant, host=nx_url)
    site.arm_event = cfg
    await session.commit()
    manager.client_for(site)
    rt = manager._rt[site.id]
    rt.arm_task = asyncio.create_task(arm_rules.run(manager, rt))
    return site, rt


async def settle():
    for _ in range(100):
        await asyncio.sleep(0.05)
        if not arm_event._tasks:
            return


def test_captions_must_tell_apart():
    assert arm_event.clean({"enabled": True}) == {**ON}
    with pytest.raises(ValueError, match="contain"):
        arm_event.clean({"enabled": True, "armed": "Armed", "disarmed": "Disarmed"})     # "Disarmed" contains "armed"
    assert arm_event.clean({"enabled": True, "armed": "ARM", "disarmed": "OFF"})["armed"] == "ARM"


async def test_disarm_and_arm_tell_nx_in_the_right_order(client, session, admin, nx):  # noqa: F811
    tenant, user = admin
    site, rt = await site_with_tasks(session, tenant, nx)
    await login(client, user.email)

    assert (await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 30, "note": "Cleaning crew"})).status_code == 200
    await settle()
    for _ in range(40):
        if ("rule", "Siren on Site armed #arm", False) in fake_nx.NX_ORDER:
            break
        await asyncio.sleep(0.05)
    sent = fake_nx.NX_SENT[-1]
    assert (sent["source"], sent["caption"]) == ("TWG Alarm Portal", "Site disarmed")
    assert sent["description"].startswith(f"Disarmed by {user.label}") and "Cleaning crew" in sent["description"]
    assert "re-arms at" in sent["description"]
    # disarm: the event first, then the #arm rule off
    assert fake_nx.NX_ORDER == [("generic", "Site disarmed"), ("rule", "Siren on Site armed #arm", False)]

    fake_nx.NX_ORDER.clear()
    assert (await client.post(f"/api/sites/{site.id}/arm", json={})).status_code == 200
    await settle()
    # arm: the #arm rule on first, then the event (so that rule hears it)
    assert fake_nx.NX_ORDER == [("rule", "Siren on Site armed #arm", True), ("generic", "Site armed")]
    rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.arm_event").order_by(AuditLog.id))).all()
    assert [(r.detail["caption"], r.detail["ok"]) for r in rows] == [("Site disarmed", True), ("Site armed", True)]
    # The site's data and the MCP see the setting.
    assert (await client.get(f"/api/sites/{site.id}")).json()["arm_event"]["enabled"] is True
    rt.arm_task.cancel()


async def test_the_schedule_tells_nx_too(session, admin, nx):  # noqa: F811
    tenant, _ = admin
    site, rt = await site_with_tasks(session, tenant, nx)
    site.timezone = "UTC"
    site.arm_schedule = {"entries": [{"action": "disarm", "time": "00:00", "days": list(arming.DAYS)}], "since_ms": 0}
    await session.commit()
    assert await arming.check_all(int(time.time() * 1000)) == 1
    await settle()
    assert fake_nx.NX_SENT[-1]["caption"] == "Site disarmed" and "by the schedule" in fake_nx.NX_SENT[-1]["description"]
    rt.arm_task.cancel()


async def test_off_by_default_and_retried_when_nx_refuses(client, session, admin, nx, monkeypatch):  # noqa: F811
    tenant, user = admin
    site, rt = await site_with_tasks(session, tenant, nx, cfg=None)
    await login(client, user.email)
    fake_nx.NX_SENT.clear()
    await client.post(f"/api/sites/{site.id}/disarm", json={})
    await settle()
    assert fake_nx.NX_SENT == []                                         # not set up: nothing sent
    for _ in range(40):                                                  # ...but the #arm rules still follow
        if ("rule", "Siren on Site armed #arm", False) in fake_nx.NX_ORDER:
            break
        await asyncio.sleep(0.05)
    assert ("rule", "Siren on Site armed #arm", False) in fake_nx.NX_ORDER

    # Set up from Edit site → Arming; NX refuses twice, then takes it.
    r = await client.put(f"/api/sites/{site.id}/arm-schedule", json={"timezone": "", "arm_schedule": [], "arm_event": ON})
    assert r.status_code == 200 and r.json()["arm_event"]["enabled"] is True
    bad = await client.put(f"/api/sites/{site.id}/arm-schedule",
                           json={"timezone": "", "arm_schedule": [], "arm_event": {**ON, "armed": "armed"}})
    assert bad.status_code == 400
    monkeypatch.setattr(arm_event, "RETRY_S", (0.1, 0.1, 0.1, 0.1))
    fake_nx.NX_EVENTS_MODE["mode"] = "refuse"
    await client.post(f"/api/sites/{site.id}/arm", json={})

    async def allow_later():
        await asyncio.sleep(0.15)
        fake_nx.NX_EVENTS_MODE["mode"] = "ok"
    await asyncio.gather(allow_later(), settle())
    await settle()
    row = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.arm_event"))).one()
    assert row.detail["ok"] is True and row.detail["tries"] > 1 and row.detail["caption"] == "Site armed"
    rt.arm_task.cancel()


async def test_the_portal_doesnt_alarm_on_its_own_message(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.arm_event = ON
    await session.commit()

    def row(source, caption, ts):
        return {"timestampMs": ts, "ruleId": "", "actionData": {"id": f"a{ts}", "type": "writeToLog"},
                "eventData": {"type": "generic", "state": "instant", "source": source, "caption": caption,
                              "timestamp": str(ts * 1000)}}
    now = int(time.time() * 1000)
    created = await ingest(session, site, [row("TWG Alarm Portal", "Site armed", now), row("Ajax", "Intrusion", now + 1)], {})
    assert [a.caption for a in created] == ["Intrusion"]


async def test_webhooks_run_with_it_and_arm_rules_are_optional(client, session, admin, nx):  # noqa: F811
    from tests.test_webhooks import GATE, STROBE
    tenant, user = admin
    site, rt = await site_with_tasks(session, tenant, nx, cfg={**ON, "enabled": False})
    await login(client, user.email)
    r = await client.post("/api/webhooks", json={"site_id": site.id, "name": "Strobe armed", "kind": "trigger", "config": {
        "trigger_id": STROBE, "trigger_name": "Strobe speaker armed", "device_id": GATE, "on_arm": True}})
    assert r.status_code == 200 and r.json()["config"]["on_arm"] is True
    await client.post("/api/webhooks", json={"site_id": site.id, "name": "Panel", "kind": "generic", "config": {
        "source": "Portal", "caption": "Site {arming} by {by}", "on_arm": True, "on_disarm": True}})
    fake_nx.NX_SENT.clear()
    await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 10})
    await settle()
    assert [(x["kind"], x.get("caption")) for x in fake_nx.NX_SENT] == [("generic", f"Site disarmed by {user.label}")]
    fake_nx.NX_SENT.clear(); fake_nx.NX_ORDER.clear()
    await client.post(f"/api/sites/{site.id}/arm", json={})
    await settle()
    assert sorted((x["kind"], x.get("caption", x.get("triggerId"))) for x in fake_nx.NX_SENT) == [
        ("generic", f"Site armed by {user.label}"), ("trigger", STROBE)]
    assert fake_nx.NX_ORDER[0] == ("rule", "Siren on Site armed #arm", True)          # rules on before the event
    hooks = (await client.get("/api/webhooks")).json()
    assert all(h["recent"] and h["recent"][0]["from"] == "site armed" for h in hooks)

    # #arm rules switched off for this site: disarming leaves them on (and turning it off puts them back on).
    r = await client.put(f"/api/sites/{site.id}/arm-schedule", json={"timezone": "", "arm_schedule": [],
                                                                     "arm_event": {**ON, "enabled": False, "rules": False}})
    assert r.json()["arm_event"]["rules"] is False
    fake_nx.NX_ORDER.clear()
    await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 10})
    await settle()
    await asyncio.sleep(0.5)
    assert not [o for o in fake_nx.NX_ORDER if o[0] == "rule"] and fake_nx.RULES["line"]["enabled"] is True
    rt.arm_task.cancel()


async def test_its_own_soft_trigger_isnt_an_alarm_but_a_real_press_is(session, admin):
    from tests.test_webhooks import STROBE
    tenant, _ = admin
    site = await make_site(session, tenant)
    from types import SimpleNamespace
    arm_event._expect_echo(site.id, SimpleNamespace(kind="trigger"), {"trigger_id": STROBE})

    def press(ts, rule):
        return {"timestampMs": ts, "ruleId": rule, "actionData": {"id": f"a{ts}{rule}", "type": "writeToLog"},
                "eventData": {"type": "softTrigger", "state": "instant", "triggerId": "{%s}" % STROBE, "deviceId": "{d1}",
                              "triggerName": "Strobe speaker armed", "timestamp": str(ts * 1000)}}
    now = int(time.time() * 1000)
    # the portal's own press, logged by two NX rules: no alarm
    assert await ingest(session, site, [press(now, "{r1}"), press(now, "{r2}")], {}) == []
    # a real press a moment later: an alarm as usual
    created = await ingest(session, site, [press(now + 4000, "{r1}")], {})
    assert [a.event_type for a in created] == ["softTrigger"]

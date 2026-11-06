"""NX rules tagged #arm follow the site's arming (app/services/arm_rules.py): switched off in NX while disarmed and back
on when armed; untagged and #24h rules untouched; a failed switch retried and, if it keeps failing, raised as a system
alarm. NX is tools/fake_nx.py, answered in-process."""
import asyncio
import copy

import pytest
from sqlalchemy import select

from app.models import Alarm, AuditLog
from app.services import arm_rules, arming, rule_edit
from app.services.alarm_filter import Policy, rule_tag
from app.services.poller import SiteRuntime, manager
from tests.conftest import login, make_site
from tests.test_rule_editor import AsgiNX
from tools import fake_nx


class NX(AsgiNX):
    async def get_rules(self): return await self._get("/rest/v4/events/rules")


class Mgr:
    """Stands in for the poller manager: collects the alarms it would announce."""
    def __init__(self):
        self.announced = []

    def _announce(self, created):
        self.announced += created


@pytest.fixture(autouse=True)
def fresh_fake():
    rules = copy.deepcopy(fake_nx.RULES)
    fake_nx.RULES["line"]["comment"] = "Strobe + speaker on line crossing #arm #alarm"
    fake_nx.RULES["panic24"]["comment"] = "Hold-up button #arm #24h"
    fake_nx.RULE_MODE.update(patch="replace", write="ok")
    yield
    fake_nx.RULES.clear(); fake_nx.RULES.update(rules)
    fake_nx.RULE_MODE.update(patch="replace", write="ok")


def on(kind):
    return fake_nx.RULES[kind]["enabled"] is not False


async def site_rt(session, tenant):
    site = await make_site(session, tenant)
    rt = SiteRuntime(site.id, tenant.id, NX())
    manager._rt[site.id] = rt
    return site, rt


@pytest.fixture(autouse=True)
def forget_runtimes():
    yield
    manager._rt.clear()


def test_the_arm_tag():
    assert arming.rule_follows_arming("Front door #arm #alarm") and rule_tag("Front door #arm #alarm") == "alarm"
    assert arming.rule_follows_arming("#ARM strobe")
    assert not arming.rule_follows_arming("Front door #alarm")              # #alarm isn't #arm
    assert not arming.rule_follows_arming("Front door #armed")
    assert not arming.rule_follows_arming("Hold-up #arm #24h")             # #24h wins: never switched off
    assert rule_edit.with_tags("Front door #alarm", "alarm", None, True) == "Front door #arm #alarm"
    assert rule_edit.with_tags("Front door #arm #alarm", rule_edit.UNCHANGED, None, False) == "Front door #alarm"
    assert rule_edit.with_tags("Front door #arm", "critical") == "Front door #arm #critical"   # other edits keep #arm


async def test_disarm_switches_tagged_rules_off_and_arm_switches_them_back(client, session, admin):
    tenant, user = admin
    site, rt = await site_rt(session, tenant)
    m = Mgr()
    await login(client, user.email)

    assert (await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 60})).status_code == 200
    assert rt.arm_kick.is_set()                                   # the button wakes the site's switching task
    assert await arm_rules._once(m, rt, 0) == 0
    assert not on("line")                                         # #arm #alarm: off in NX
    assert on("panic") and on("dock") and on("panic24")           # untagged and #24h: untouched
    assert rt.arm_rules["state"] == "ok" and rt.arm_rules["armed"] is False
    assert rt.arm_rules["rules"] == ["Strobe + speaker on line crossing #arm #alarm"]
    assert [r["enabled"] for r in rt.rules if "#arm #alarm" in r["comment"]] == [False]   # the rules view shows it at once

    rt.arm_kick.clear()
    assert (await client.post(f"/api/sites/{site.id}/arm", json={})).status_code == 200
    assert rt.arm_kick.is_set()
    await arm_rules._once(m, rt, 0)
    assert on("line") and rt.arm_rules["armed"] is True
    assert await arm_rules._once(m, rt, 0) == 0                   # nothing out of step: nothing written
    rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.arm_rules").order_by(AuditLog.id))).all()
    assert [(r.detail["armed"], r.detail["switched"]) for r in rows] == [
        (False, ["Strobe + speaker on line crossing #arm #alarm"]), (True, ["Strobe + speaker on line crossing #arm #alarm"])]
    assert m.announced == []
    # The site's API data carries it (Sites page, map).
    assert (await client.get(f"/api/sites/{site.id}")).json()["arm_rules"]["state"] == "ok"


async def test_the_schedule_and_rule_reads_bring_rules_into_step(session, admin):
    tenant, _ = admin
    site, rt = await site_rt(session, tenant)
    site.timezone = "UTC"
    site.arm_schedule = {"entries": [{"action": "disarm", "time": "00:00", "days": list(arming.DAYS)}], "since_ms": 0}
    await session.commit()
    import time
    assert await arming.check_all(int(time.time() * 1000)) == 1  # the schedule disarms it...
    assert rt.arm_kick.is_set()                                    # ...and the switching task is woken
    rt.arm_kick.clear()
    # A rule tagged #arm in Nx Witness while disarmed: the poller's next rule read spots it.
    await manager._refresh_rules(rt, session, site, Policy(), force=True)
    assert rt.arm_kick.is_set() and rt.arm_rules["state"] == "switching"
    await arm_rules._once(Mgr(), rt, 0)
    assert not on("line") and rt.arm_rules["state"] == "ok"


async def test_a_failed_switch_back_on_is_retried_and_raised_as_an_alarm(client, session, admin):
    tenant, user = admin
    site, rt = await site_rt(session, tenant)
    m = Mgr()
    fake_nx.RULES["line"]["enabled"] = False                       # left off from a disarm
    fake_nx.RULE_MODE["write"] = "refuse"                          # the NX account lost its rights
    fails = 0
    for _ in range(arm_rules.FAIL_ALARM_AFTER):
        fails = await arm_rules._once(m, rt, fails)
    assert fails == arm_rules.FAIL_ALARM_AFTER and not on("line")
    assert rt.arm_rules["state"] == "failed" and "Power Users" in rt.arm_rules["detail"]
    [a] = m.announced
    assert (a.event_type, a.state, a.priority) == (arm_rules.ALARM_ON, "new", 2)
    assert "didn't switch back on" in a.caption and "Power Users" in a.description
    await arm_rules._once(m, rt, fails)                             # still failing: no second alarm
    assert len(m.announced) == 1
    audits = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.arm_rules"))).all()
    assert len(audits) == 1 and audits[0].detail["failed"]          # one audit row per failing run, not per try

    fake_nx.RULE_MODE["write"] = "ok"
    assert await arm_rules._once(m, rt, fails) == 0
    assert on("line") and rt.arm_rules["state"] == "ok"
    a = await session.get(Alarm, a.id, populate_existing=True)
    assert "Switched at" in a.description and a.state == "new"    # stays open for the operator, says it's fixed


async def test_a_failed_switch_off_raises_even_while_disarmed(client, session, admin):
    tenant, user = admin
    site, rt = await site_rt(session, tenant)
    await login(client, user.email)
    await client.post(f"/api/sites/{site.id}/disarm", json={})
    fake_nx.RULE_MODE["write"] = "refuse"
    m, fails = Mgr(), 0
    for _ in range(arm_rules.FAIL_ALARM_AFTER):
        fails = await arm_rules._once(m, rt, fails)
    [a] = m.announced
    assert (a.event_type, a.state, a.priority) == (arm_rules.ALARM_OFF, "new", 3)   # a warning, raised though disarmed


async def test_no_alarm_while_the_site_is_offline(session, admin):
    tenant, _ = admin
    site, rt = await site_rt(session, tenant)
    site.status = "offline"
    await session.commit()
    fake_nx.RULES["line"]["enabled"] = False
    fake_nx.RULE_MODE["write"] = "refuse"
    m, fails = Mgr(), 0
    for _ in range(arm_rules.FAIL_ALARM_AFTER + 1):
        fails = await arm_rules._once(m, rt, fails)
    assert m.announced == []                                        # "Site connection lost" already covers it
    assert not (await session.scalars(select(Alarm).where(Alarm.event_type == arm_rules.ALARM_ON))).all()


async def test_the_task_retries_by_itself(session, admin, monkeypatch):
    tenant, _ = admin
    site, rt = await site_rt(session, tenant)
    monkeypatch.setattr(arm_rules, "RETRY_S", (0.05,))
    fake_nx.RULES["line"]["enabled"] = False
    fake_nx.RULE_MODE["write"] = "refuse"
    task = asyncio.create_task(arm_rules.run(Mgr(), rt))
    try:
        rt.arm_kick.set()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if rt.arm_rules.get("state") == "failed":
                break
        assert rt.arm_rules["state"] == "failed"
        fake_nx.RULE_MODE["write"] = "ok"                           # rights given back: the next retry switches it
        for _ in range(40):
            await asyncio.sleep(0.05)
            if on("line"):
                break
        assert on("line") and rt.arm_rules["state"] == "ok"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_rules_list_and_editor_know_the_tag(client, session, admin):
    tenant, user = admin
    site, rt = await site_rt(session, tenant)
    await manager._refresh_rules(rt, session, site, Policy(), force=True)
    from app.services.poller import describe_rules
    by = {r["name"]: r for r in describe_rules(rt.rules, Policy())}
    assert by["Strobe + speaker on line crossing #arm #alarm"]["arm"] is True
    assert by["Hold-up button #arm #24h"]["arm"] is False and by["Hold-up button #arm #24h"]["always"] is True
    # Tagging from the portal's rule editor.
    from app.services import rule_edit as re_
    _, changes, after = await re_.edit(NX(), fake_nx.RULES["panic"]["id"].strip("{}"), arm=True)
    assert after["comment"] == "Panic button #arm"

"""Editing NX rules from the portal (administrators): tags in the comment, on/off, Interval of action, the read-back
check that puts a rule back if NX didn't apply the change as sent, and who may do it."""
import copy

import pytest

from app.services import rule_edit
from app.services.rule_edit import UNCHANGED, with_tags
from tests.conftest import login, make_site, make_tenant_user

RID = "4c1d77c5-5f11-4567-8cfa-6db360231186"


def test_tags_are_rewritten_and_the_text_kept():
    assert with_tags("Panic Alarm #alarm", "critical") == "Panic Alarm #critical"
    assert with_tags("Panic Alarm #alarm", None) == "Panic Alarm"
    assert with_tags("Hold-up #24h", "critical") == "Hold-up #24h #critical"
    assert with_tags("Hold-up #24h #critical", UNCHANGED, False) == "Hold-up #critical"
    assert with_tags("Gate", "warning", True) == "Gate #24h #warning"
    assert with_tags("Use #alarmed words and #critical", "alarm") == "Use #alarmed words and #alarm"   # only whole tags go


class FakeNX:
    """get_rule / modify_rule like NX's PATCH (top-level fields replaced); `lie` makes it apply something else."""
    def __init__(self, rule, lie=None):
        self.rule, self.lie, self.patches = copy.deepcopy(rule), lie, []

    async def get_rule(self, rid):
        return copy.deepcopy(self.rule)

    async def modify_rule(self, rid, changes):
        self.patches.append(copy.deepcopy(changes))
        self.rule.update(copy.deepcopy(changes))
        if self.lie and len(self.patches) == 1:
            self.rule.update(self.lie)


PLAIN = {"id": RID, "comment": "Panic Alarm #alarm", "enabled": True, "schedule": [],
         "event": {"type": "softTrigger", "triggerId": "t1"}, "action": {"type": "writeToLog", "intervalS": 60}}
HTTP = {**PLAIN, "action": {"type": "http", "url": "http://10.1.30.174/x", "auth": "secret", "intervalS": 30}}


async def test_only_the_edited_fields_are_sent():
    nx = FakeNX(PLAIN)
    before, changes, after = await rule_edit.edit(nx, RID, level="critical", always=True, interval_s=0)
    assert nx.patches == [{"comment": "Panic Alarm #24h #critical", "action": {"type": "writeToLog", "intervalS": 0}}]
    assert after["event"] == PLAIN["event"] and after["enabled"] is True
    nx = FakeNX(PLAIN)
    await rule_edit.edit(nx, RID, enabled=False)
    assert nx.patches == [{"enabled": False}]
    nx = FakeNX(PLAIN)
    assert (await rule_edit.edit(nx, RID, level="alarm"))[1] == {} and nx.patches == []         # nothing to change


async def test_a_rule_with_a_login_never_has_its_action_rewritten():
    nx = FakeNX(HTTP)
    with pytest.raises(rule_edit.RuleEditError, match="holds a login"):
        await rule_edit.edit(nx, RID, interval_s=0)
    assert nx.patches == []
    await rule_edit.edit(nx, RID, level="critical")                 # its tag can still change
    assert nx.patches == [{"comment": "Panic Alarm #critical"}] and nx.rule["action"]["auth"] == "secret"


async def test_nx_doing_something_else_is_put_back_and_reported():
    nx = FakeNX(PLAIN, lie={"event": {"type": "softTrigger"}})      # NX dropped part of the event
    with pytest.raises(rule_edit.RuleEditError, match="put back"):
        await rule_edit.edit(nx, RID, level="critical")
    assert nx.patches[1] == {"comment": "Panic Alarm #alarm", "event": PLAIN["event"]}
    assert nx.rule["comment"] == "Panic Alarm #alarm" and nx.rule["event"] == PLAIN["event"]


async def test_only_admins_edit_rules_and_flip_tagged_only(client, session, monkeypatch):
    from app.routers import api
    from app.services.poller import manager
    from tests.test_users import _user
    tenant, admin = await make_tenant_user(session)
    op = await _user(session, tenant, "op@twgsecurity.com", role="operator")
    site = await make_site(session, tenant)
    nx = FakeNX(PLAIN)
    monkeypatch.setattr(manager, "client_for", lambda s: nx)
    async def no_check(sid): return ""
    monkeypatch.setattr(manager, "check_now", no_check)
    await login(client, op.email)
    assert (await client.patch(f"/api/sites/{site.id}/rules/{RID}", json={"level": "critical"})).status_code == 403
    assert (await client.put("/api/settings/tagged-only", json={"on": True})).status_code == 403
    await login(client, admin.email)
    r = await client.patch(f"/api/sites/{site.id}/rules/{RID}", json={"level": "critical", "interval_s": 0})
    assert r.status_code == 200 and r.json()["changed"] == ["comment", "interval_s"]
    assert nx.rule["comment"] == "Panic Alarm #critical" and nx.rule["action"]["intervalS"] == 0
    assert (await client.patch(f"/api/sites/{site.id}/rules/not-a-rule", json={"level": "alarm"})).status_code == 404
    assert (await client.put("/api/settings/tagged-only", json={"on": True})).json() == {"tagged_only": True}
    from sqlalchemy import select
    from app.models import AuditLog
    row = (await session.scalars(select(AuditLog).where(AuditLog.action == "nx.rule_edited"))).one()
    assert row.detail["comment"] == {"from": "Panic Alarm #alarm", "to": "Panic Alarm #critical"} and row.detail["interval_s"] == {"from": 60, "to": 0}

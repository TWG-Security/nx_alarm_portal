"""The full NX rule editor: the site's rule catalogue (NX's own event/action manifests, cameras, analytics types and
zones found per camera), full saves (event type, cameras, analytics type, filters, action) checked by reading the rule
back, the PUT fallback when NX merges a PATCH, new rules, deletes, and who may do it."""
import copy

import httpx
import pytest
from sqlalchemy import select

from app.models import Alarm, AuditLog
from app.services import rule_catalog, rule_edit
from tests.conftest import login, make_site, make_tenant_user
from tools import fake_nx

GATE, DOCK = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
PANIC = fake_nx.RULES["panic"]["id"].strip("{}")


class AsgiNX:
    """The portal's NX client calls, answered in-process by tools/fake_nx.py."""
    def __init__(self, *a, **kw):
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_nx.app), base_url="http://nx")

    async def _get(self, path, params=None):
        r = await self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def _send(self, method, path, body=None):
        r = await self.http.request(method, path, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get_rule(self, rid): return await self._get(f"/rest/v4/events/rules/{rid}")
    async def modify_rule(self, rid, ch): return await self._send("PATCH", f"/rest/v4/events/rules/{rid}", ch)
    async def replace_rule(self, rid, r): return await self._send("PUT", f"/rest/v4/events/rules/{rid}", r)
    async def create_rule(self, r): return await self._send("POST", "/rest/v4/events/rules", r)
    async def delete_rule(self, rid): return await self._send("DELETE", f"/rest/v4/events/rules/{rid}")
    async def close(self): await self.http.aclose()


@pytest.fixture(autouse=True)
def fresh_fake(monkeypatch):
    rules, events = copy.deepcopy(fake_nx.RULES), list(fake_nx.EVENTS)
    monkeypatch.setattr(rule_catalog, "make_client", AsgiNX)
    rule_catalog._cache.clear()
    fake_nx.RULE_MODE.update(patch="replace", write="ok", manifest="ok")
    yield
    fake_nx.RULES.clear(); fake_nx.RULES.update(rules)
    fake_nx.EVENTS[:] = events
    fake_nx.RULE_MODE.update(patch="replace", write="ok", manifest="ok")


async def _catalog(session, inject_axis=True):
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    if inject_axis:
        await AsgiNX().http.post("/_inject/axis")
    return tenant, admin, site, await rule_catalog.build(site, list(fake_nx.RULES.values()))


async def test_the_catalogue_offers_what_nx_supports_and_finds_the_zones(session):
    tenant, _, site, cat = await _catalog(session)
    ev = {e["id"]: e for e in cat["events"]}
    assert {"analytics", "analyticsObject", "softTrigger", "motion", "cameraInput", "generic"} <= set(ev)
    an = {f["key"]: f for f in ev["analytics"]["fields"]}
    assert an["devices"]["type"] == "device" and not an["devices"]["accept_all"]
    assert an["eventTypeId"]["type"] == "analyticsEventType" and an["caption"]["enum"][:2] == ["containsKeywords", "doesNotContainKeywords"]
    assert an["state"]["enum"] == ["none", "started", "stopped", "instant"]
    acts = {a["id"]: a for a in cat["actions"]}
    assert acts["writeToLog"]["editable"] and acts["desktopNotification"]["editable"] and not acts["http"]["editable"]
    assert {f["key"] for f in acts["bookmark"]["fields"]} >= {"devices", "durationS", "recordBeforeS", "recordAfterS", "tags"}
    assert {f["key"] for f in acts["desktopNotification"]["fields"]} >= {"users", "acknowledge", "intervalS"}
    cams = {c["id"]: c for c in cat["cameras"]}
    assert cams[GATE]["engines"][0]["name"] == "Axis" and cams[GATE]["inputs"] == [{"id": "I0", "name": "Gate contact"}]
    # CVEDIA-style plugin: its camera manifest's event / object types and the zones drawn in its settings
    dock = cat["analytics"][DOCK]
    assert [e["name"] for e in dock["event_types"]] == ["Intrusion", "Loitering"]
    assert {o["name"] for o in dock["object_types"]} == {"Person", "Vehicle"}
    zones = {z["name"]: z for z in dock["zones"]}
    assert set(zones) == {"Dock Apron", "Tripwire 1"}                      # the empty "Area 2" slot isn't a zone
    assert zones["Dock Apron"]["kind"] == "area" and len(zones["Dock Apron"]["points"]) == 4
    assert zones["Tripwire 1"]["kind"] == "line"
    # AXIS Object Analytics: no plugin description, but its scenario shows up in the event log as a zone
    gate = cat["analytics"][GATE]
    axis = next(e for e in gate["event_types"] if e["id"] == fake_nx.AXIS_SCENARIO)
    assert axis["zones"] == ["Front Lot"] and "NX event log" in axis["sources"] and "Front Lot" in axis["label"]
    assert [z["name"] for z in gate["zones"]] == ["Front Lot"]
    src = {s["name"]: s for s in cat["sources"]}
    assert src["Plugin camera manifests"]["ok"] == 1 and src["Plugin camera manifests"]["failed"] == 1
    assert src["Analytics taxonomy"]["detail"] == "not available on this NX"


async def test_zones_also_come_from_the_portals_own_alarms(session):
    tenant, _, site, _ = await _catalog(session, inject_axis=False)
    session.add(Alarm(tenant_id=tenant.id, site_id=site.id, event_key="k1", nx_action_id="", nx_action_server_id="",
                      nx_ack_required=False, rule_id="", event_type="analytics", event_subtype="nx.axis.{abc}",
                      category="security", priority=2, caption="", description="Back Door started", source_name="",
                      device_id=GATE, event_ts_ms=rule_catalog.time.time() * 1000 // 1, state="new", raw={}))
    await session.commit()
    cat = await rule_catalog.build(site, [])
    gate = cat["analytics"][GATE]
    assert any(e["id"] == "nx.axis.{abc}" and e["zones"] == ["Back Door"] for e in gate["event_types"])
    assert rule_catalog.zone_from_text("Front Lot stopped") == "Front Lot" and rule_catalog.zone_from_text("Intrusion") == ""


async def test_a_rule_is_rewritten_to_a_zone_event_and_checked(session):
    _, _, site, cat = await _catalog(session)
    nx = AsgiNX()
    before, changes, after = await rule_edit.save(
        nx, PANIC, cat,
        event={"type": "analytics", "fields": {"devices": {"ids": [GATE]}, "eventTypeId": fake_nx.AXIS_SCENARIO,
                                               "state": "started", "caption": {"checkType": "doesNotContainKeywords", "value": "test"}}},
        action={"type": "desktopNotification", "fields": {"users": {"acceptAll": True}, "acknowledge": True, "intervalS": 0}},
        level="critical")
    assert set(changes) == {"event", "action", "comment"}
    assert after["event"]["type"] == "analytics" and after["event"]["devices"] == {"acceptAll": False, "ids": [GATE]}
    assert after["event"]["description"] == {"checkType": "containsKeywords"}        # new type: NX's empty filter
    assert "triggerId" not in after["event"]                                         # nothing left of the soft trigger
    assert after["action"] == {"type": "desktopNotification", "users": {"acceptAll": True, "ids": []}, "acknowledge": True, "intervalS": 0}
    assert after["comment"] == "Panic button #critical"


async def test_when_nx_merges_a_patch_the_whole_rule_is_sent_instead(session):
    _, _, site, cat = await _catalog(session)
    fake_nx.RULE_MODE["patch"] = "merge"
    fake_nx.RULES["panic"]["event"] = {"type": "softTrigger", "triggerId": "256dacab-d698-4a32-b9ec-135ba73ba127", "users": {"acceptAll": True}}
    nx = AsgiNX()
    _, _, after = await rule_edit.save(nx, PANIC, cat, event={"type": "motion", "fields": {"devices": {"acceptAll": True}}})
    assert after["event"] == {"type": "motion", "devices": {"acceptAll": True, "ids": []}, "state": "started"}
    assert any("replaced" in p for p in fake_nx.RULE_PATCHES if p["id"].strip("{}") == PANIC)


async def test_bad_choices_are_refused_before_anything_is_written(session):
    _, _, site, cat = await _catalog(session)
    nx = AsgiNX()
    n = len(fake_nx.RULE_PATCHES)
    with pytest.raises(rule_edit.RuleEditError, match="pick at least one"):
        await rule_edit.save(nx, PANIC, cat, event={"type": "analytics", "fields": {"eventTypeId": "x"}})
    with pytest.raises(rule_edit.RuleEditError, match="Of Type"):
        await rule_edit.save(nx, PANIC, cat, event={"type": "analytics", "fields": {"devices": {"ids": [GATE]}}})
    with pytest.raises(rule_edit.RuleEditError, match="no event type"):
        await rule_edit.save(nx, PANIC, cat, event={"type": "madeUp"})
    with pytest.raises(rule_edit.RuleEditError, match="doesn't write"):
        await rule_edit.save(nx, PANIC, cat, action={"type": "http", "fields": {"url": "http://x"}})
    with pytest.raises(rule_edit.RuleEditError, match="unknown match type"):
        await rule_edit.save(nx, PANIC, cat, event={"type": "softTrigger", "fields": {}} | {"type": "generic", "fields": {"source": {"checkType": "regex"}}})
    with pytest.raises(rule_edit.RuleEditError, match="valid selection"):
        await rule_edit.save(nx, PANIC, cat, event={"type": "motion", "fields": {"devices": {"ids": ["../etc"]}}})
    assert len(fake_nx.RULE_PATCHES) == n


async def test_an_action_holding_a_login_is_never_rewritten_or_shown(session):
    _, _, site, cat = await _catalog(session)
    fake_nx.RULES["panic"]["action"] = {"type": "http", "url": "http://10.1.30.174/x", "auth": {"login": "root", "password": "pw"}, "intervalS": 0}
    nx = AsgiNX()
    with pytest.raises(rule_edit.RuleEditError, match="holds a login"):
        await rule_edit.save(nx, PANIC, cat, action={"type": "writeToLog"})
    _, changes, after = await rule_edit.save(nx, PANIC, cat, action={"type": "http"},
                                             event={"type": "softTrigger", "fields": {"triggerName": "Panic"}})
    assert set(changes) == {"event"} and after["action"]["auth"]["password"] == "pw"
    shown = rule_edit.redact(after)
    assert shown["action_locked"] and shown["action"]["auth"] == "(hidden)" and "pw" not in str(shown)


async def test_new_rules_are_created_checked_and_deleted(session):
    _, _, site, cat = await _catalog(session)
    nx = AsgiNX()
    made = await rule_edit.create(nx, cat, event={"type": "analytics", "fields": {"devices": {"ids": [DOCK]}, "eventTypeId": "cvedia.rt.intrusion"}},
                                  action={"type": "writeToLog", "fields": {"intervalS": 0}}, comment="Dock apron intrusion", level="alarm", always=True)
    assert made["comment"] == "Dock apron intrusion #24h #alarm" and made["event"]["eventTypeId"] == "cvedia.rt.intrusion"
    assert made["event"]["state"] == "started" and made["action"] == {"type": "writeToLog", "intervalS": 0}
    rid = made["id"].strip("{}")
    gone = await rule_edit.delete(nx, rid)
    assert gone["comment"] == "Dock apron intrusion #24h #alarm"
    assert not any(r["id"].strip("{}") == rid for r in fake_nx.RULES.values())


async def test_only_admins_use_the_editor_and_everything_is_audited(client, session, monkeypatch):
    from app.services.poller import manager
    from tests.test_users import _user
    tenant, admin = await make_tenant_user(session)
    op = await _user(session, tenant, "op@twgsecurity.com", role="operator")
    site = await make_site(session, tenant)
    nx = AsgiNX()
    monkeypatch.setattr(manager, "client_for", lambda s: nx)
    async def no_check(sid): return ""
    monkeypatch.setattr(manager, "check_now", no_check)
    await login(client, op.email)
    for method, path, body in (("GET", f"/api/sites/{site.id}/rules/catalog", None), ("GET", f"/api/sites/{site.id}/rules/{PANIC}", None),
                               ("PUT", f"/api/sites/{site.id}/rules/{PANIC}", {"enabled": False}),
                               ("POST", f"/api/sites/{site.id}/rules", {"event": {"type": "motion"}, "action": {"type": "writeToLog"}}),
                               ("DELETE", f"/api/sites/{site.id}/rules/{PANIC}", None)):
        assert (await client.request(method, path, json=body)).status_code == 403, path
    await login(client, admin.email)
    cat = (await client.get(f"/api/sites/{site.id}/rules/catalog")).json()
    assert cat["analytics"][DOCK]["zones"][0]["name"] == "Dock Apron"
    assert (await client.get(f"/api/sites/{site.id}/rules/{PANIC}")).json()["event"]["type"] == "softTrigger"
    r = await client.put(f"/api/sites/{site.id}/rules/{PANIC}", json={
        "event": {"type": "cameraInput", "fields": {"devices": {"ids": [GATE]}, "inputPortId": "I0"}}, "level": "critical"})
    assert r.status_code == 200 and r.json()["changed"] == ["comment", "event"], r.text
    r = await client.put(f"/api/sites/{site.id}/rules/{PANIC}", json={"action": {"type": "bookmark", "fields": {"durationS": 10}}})
    assert r.status_code == 200 and r.json()["changed"] == ["action"], r.text
    r = await client.put(f"/api/sites/{site.id}/rules/{PANIC}", json={"event": {"type": "analytics", "fields": {}}})
    assert r.status_code == 400 and "pick at least one" in r.json()["detail"]
    r = await client.post(f"/api/sites/{site.id}/rules", json={"event": {"type": "motion", "fields": {"devices": {"ids": [DOCK]}}},
                                                               "action": {"type": "writeToLog"}, "comment": "Dock motion", "level": "warning"})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert (await client.delete(f"/api/sites/{site.id}/rules/{new_id}")).status_code == 200
    assert (await client.get(f"/api/sites/{site.id}/rules/{new_id}")).status_code == 404
    rows = {a.action: a for a in (await session.scalars(select(AuditLog).where(AuditLog.action.like("nx.rule_%")))).all()}
    assert set(rows) == {"nx.rule_edited", "nx.rule_edit_failed", "nx.rule_created", "nx.rule_deleted"}
    edits = (await session.scalars(select(AuditLog).where(AuditLog.action == "nx.rule_edited").order_by(AuditLog.id))).all()
    assert edits[0].detail["event"]["from"]["type"] == "softTrigger" and edits[0].detail["event"]["to"]["inputPortId"] == "I0"
    assert edits[1].detail["nx_action"]["from"]["type"] == "writeToLog" and edits[1].detail["nx_action"]["to"]["durationS"] == 10
    assert rows["nx.rule_deleted"].detail["rule"]["comment"] == "Dock motion #warning"


async def test_nx_refusing_or_not_describing_its_rules_is_reported(client, session, monkeypatch):
    from app.services.poller import manager
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    nx = AsgiNX()
    monkeypatch.setattr(manager, "client_for", lambda s: nx)
    await login(client, admin.email)
    fake_nx.RULE_MODE["write"] = "refuse"
    r = await client.put(f"/api/sites/{site.id}/rules/{PANIC}", json={"enabled": False})
    assert r.status_code == 502 and "Power Users" in r.json()["detail"]
    assert fake_nx.RULES["panic"]["enabled"] is True
    fake_nx.RULE_MODE["manifest"] = "missing"
    rule_catalog._cache.clear()
    r = await client.get(f"/api/sites/{site.id}/rules/catalog")
    assert r.status_code == 502 and "Event catalogue: not available on this NX" in r.json()["detail"]


async def test_a_slow_site_still_gets_an_editor_with_what_was_found(session, monkeypatch):
    import asyncio
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    slow = AsgiNX._get
    async def crawl(self, path, params=None):
        if "/deviceAgents/" in path:
            await asyncio.sleep(30)
        return await slow(self, path, params)
    monkeypatch.setattr(AsgiNX, "_get", crawl)
    monkeypatch.setattr(rule_catalog, "BUILD_MAX_S", 0)          # the 5 s floor applies
    t0 = rule_catalog.time.monotonic()
    cat = await rule_catalog.build(site, [])
    assert rule_catalog.time.monotonic() - t0 < 8
    assert {e["id"] for e in cat["events"]} >= {"analytics", "motion"} and len(cat["cameras"]) == 2
    assert any(s["name"] == "Time limit" for s in cat["sources"])

"""The MCP as TWG's internal tool across every company (the user, 2026-11-06: "a MCP for me to control the alarm portal
not just for twg but for all clients, this is an internal tool only"): TWG staff run customers' sites (arm, disarm,
mute, check), send NX events and soft triggers, edit NX rules and run webhooks; a customer's audit log shows it as TWG
support via MCP; TWG viewers without the support permission can look but not change. NX is tools/fake_nx.py on a port."""
import copy

import pytest
from sqlalchemy import select

from app.models import AuditLog
from tests.test_mcp import _token, call
from tests.test_tenancy import world  # noqa: F401 - fixture
from tests.test_webhooks import GATE, STROBE, nx  # noqa: F401 - fixture
from tools import fake_nx


@pytest.fixture(autouse=True)
def fresh_rules():
    rules = copy.deepcopy(fake_nx.RULES)
    yield
    fake_nx.RULES.clear(); fake_nx.RULES.update(rules)


async def test_twg_runs_a_customers_site_through_the_mcp(client, session, world, nx):  # noqa: F811
    acme, site = world["a"], world["sites"]["Acme Security"]
    site.host = nx
    await session.commit()
    from app.services.poller import manager
    manager.client_for(site)                                             # the site's runtime (tests don't start pollers)
    token = await _token(client, world["twg_admin"].email)

    companies, err = await call(client, token, "list_companies")
    assert not err and {"Acme Security", "Bravo Guards"} <= {c["name"] for c in companies["companies"]}
    sites, _ = await call(client, token, "list_sites", company_id=acme.id)
    assert [s["name"] for s in sites["sites"]] == ["Acme Security HQ"]

    # Disarm needs a length (or indefinitely), then arm.
    bad, err = await call(client, token, "disarm_site", site_id=site.id)
    assert err and "minutes" in bad
    out, err = await call(client, token, "disarm_site", site_id=site.id, minutes=60, note="Customer on site (asked by phone)")
    assert not err and out["site"]["arming"]["armed"] is False and out["site"]["arming"]["next_source"] == "timer"
    out, err = await call(client, token, "arm_site", site_id=site.id)
    assert not err and out["site"]["arming"]["armed"] is True

    # Check now (reads its cameras), then mute a camera for 15 minutes and unmute it.
    out, err = await call(client, token, "check_site", site_id=site.id)
    assert not err and out["error"] is None and out["site"]["status"] == "online"
    out, err = await call(client, token, "mute_site", site_id=site.id, minutes=15, camera_id=GATE)
    assert not err and GATE in str(out["mute"])
    out, err = await call(client, token, "unmute_site", site_id=site.id, camera_id=GATE)
    assert not err and GATE not in str(out["mute"] or {})

    # NX: soft triggers listed and fired (on the trigger's own camera), a generic event, a rule tagged #arm.
    trig, err = await call(client, token, "list_soft_triggers", site_id=site.id)
    assert not err and trig["soft_triggers"][0]["id"] == STROBE
    fake_nx.NX_SENT.clear()
    out, err = await call(client, token, "fire_nx_soft_trigger", site_id=site.id, trigger_id=STROBE)
    assert not err and out["ok"] and fake_nx.NX_SENT[-1]["deviceId"] == GATE and fake_nx.NX_SENT[-1]["state"] == "instant"
    bad, err = await call(client, token, "fire_nx_soft_trigger", site_id=site.id, trigger_id=STROBE,
                          camera_id="22222222-2222-2222-2222-222222222222")
    assert err and "isn't on that camera" in bad
    out, err = await call(client, token, "send_nx_generic_event", site_id=site.id, source="TWG", caption="Test {not a template}")
    assert not err and fake_nx.NX_SENT[-1]["caption"] == "Test {not a template}"
    panic = fake_nx.RULES["panic"]["id"].strip("{}")
    out, err = await call(client, token, "edit_nx_rule", site_id=site.id, rule_id=panic, arm_tag=True, level="critical")
    assert not err and out["changed"] == ["comment"] and fake_nx.RULES["panic"]["comment"] == "Panic button #arm #critical"
    full, err = await call(client, token, "get_site", site_id=site.id)
    assert not err and full["company"] and full["link"].endswith(f"?site={site.id}")

    # Acme's audit log: every change, as TWG support through the MCP.
    rows = (await session.scalars(select(AuditLog).where(AuditLog.tenant_id == acme.id, AuditLog.user_id == world["twg_admin"].id)
                                  .order_by(AuditLog.id))).all()
    actions = [r.action for r in rows]
    for a in ("site.disarmed", "site.armed", "site.muted", "site.unmuted", "nx.event_sent", "nx.rule_edited"):
        assert a in actions, (a, actions)
    assert all((r.detail or {}).get("via", "").startswith("MCP") for r in rows if r.action != "support.viewed")


async def test_twg_viewers_look_but_dont_change(client, session, world, nx):  # noqa: F811
    site = world["sites"]["Bravo Guards"]
    token = await _token(client, world["twg_viewer"].email)              # TWG NOC: platform.view only
    companies, err = await call(client, token, "list_companies")
    assert not err and len(companies["companies"]) >= 3
    bad, err = await call(client, token, "disarm_site", site_id=site.id, minutes=30)
    assert err and "another company" in bad
    bad, err = await call(client, token, "fire_nx_soft_trigger", site_id=site.id, trigger_id=STROBE)
    assert err

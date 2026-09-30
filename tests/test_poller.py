import httpx
import respx
from sqlalchemy import func, select

from app.models import Alarm, AuditLog, Site
from app.services.bus import bus
from app.services.poller import ingest, manager
from tests.conftest import NX, make_site, nx_row


async def test_ingest_dedupes_and_collapses_action_rows(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    rows = [
        # One NX event fired three rules: email, push, and a forced-ack notification.
        nx_row(2_000_000, action_id="email", action_type="sendEmail"),
        nx_row(2_000_000, action_id="push", action_type="pushNotification"),
        nx_row(2_000_000, action_id="ack-me", ack=True, server="srv-9"),
        nx_row(2_000_500, action_id="b", type_="deviceDisconnected", caption="Camera offline"),
        nx_row(2_000_600, action_id="c", type_="integrationDiagnostic"),
    ]
    created = await ingest(session, site, rows, {})
    await session.commit()
    assert len(created) == 2
    door = next(a for a in created if a.event_type == "generic")
    assert (door.nx_action_id, door.nx_action_server_id, door.nx_ack_required, door.priority) == ("ack-me", "srv-9", True, 1)

    # The same rows again (poll overlap) create nothing new.
    assert await ingest(session, site, rows, {}) == []
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Alarm)) == 2
    assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "alarm.received")) == 2


async def test_later_ack_row_upgrades_existing_alarm(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    await ingest(session, site, [nx_row(3_000_000, action_id="email", action_type="sendEmail")], {})
    await session.commit()
    await ingest(session, site, [nx_row(3_000_000, action_id="ack-me", ack=True)], {})
    await session.commit()
    alarm = await session.scalar(select(Alarm))
    assert alarm.nx_ack_required and alarm.nx_action_id == "ack-me" and alarm.priority == 1


@respx.mock
async def test_poll_once_advances_cursor_and_publishes(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant, cursor=5_000_000)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[{"id": "{dev-1}", "name": "Lobby"}]))
    events = respx.get(f"{NX}/rest/v4/events/log").mock(return_value=httpx.Response(200, json=[
        nx_row(5_001_000, action_id="x1"), nx_row(5_002_000, action_id="x2", caption="Gate open"),
    ]))
    sub = bus.subscribe(tenant.id)
    rt = manager._runtime(site)

    created = await manager.poll_once(rt)

    assert len(created) == 2
    sent = events.calls.last.request.url.params
    assert sent["startTimeMs"] == str(5_000_000 - 5000) and sent["order"] == "asc"
    await session.refresh(site)
    assert site.event_cursor_ms == 5_002_000 and site.camera_count == 1 and site.status == "online"
    names = [sub.queue.get_nowait()[0] for _ in range(sub.queue.qsize())]
    assert names.count("alarm.new") == 2
    bus.unsubscribe(sub)


@respx.mock
async def test_one_off_503_does_not_flap_site_offline(session, admin, monkeypatch):
    import asyncio
    from app.config import get_settings
    from app.services import poller
    tenant, _ = admin
    site = await make_site(session, tenant)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(503))
    monkeypatch.setattr(get_settings(), "poll_interval_s", 0.01)
    monkeypatch.setattr(get_settings(), "poll_retry_s", 0.01)
    monkeypatch.setattr(get_settings(), "offline_after_s", 0.2)
    real_sleep = asyncio.sleep
    rt = manager._runtime(site)
    task = asyncio.create_task(manager._run(rt))
    await real_sleep(0.08)                      # several failures, but under offline_after_s
    s = (await session.execute(select(Site).execution_options(populate_existing=True))).scalar_one()
    assert s.status == "online"
    await real_sleep(0.35)                      # now past the threshold
    s = (await session.execute(select(Site).execution_options(populate_existing=True))).scalar_one()
    task.cancel()
    assert s.status == "offline"


@respx.mock
async def test_poll_failure_marks_site_auth_error(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(401))
    rt = manager._runtime(site)
    try:
        await manager.poll_once(rt)
        raise AssertionError("expected failure")
    except httpx.HTTPStatusError as exc:
        from app.services.sites import describe_http_error
        err = describe_http_error(exc)
        await manager._set_status(rt, err.status, str(err))
    s = (await session.execute(select(Site).execution_options(populate_existing=True))).scalar_one()
    assert s.status == "auth_error"


@respx.mock
async def test_nx_rule_tags_set_levels_and_loudest_rule_wins(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant, cursor=6_000_000)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[]))
    rules = respx.get(f"{NX}/rest/v4/events/rules").mock(return_value=httpx.Response(200, json=[
        {"id": "{rule-log}", "comment": "Truss 8 test trigger #warning"},
        {"id": "{rule-panic}", "comment": "Panic #critical"},
        {"id": "{rule-plain}", "comment": ""}]))
    soft = [nx_row(6_001_000, type_="softTrigger", action_id="w"), nx_row(6_002_000, type_="softTrigger", action_id="x")]
    soft[0]["ruleId"] = "rule-log"                        # only the #warning rule fired
    soft[1]["ruleId"] = "rule-log"                        # event 2: both rules fired
    both = dict(soft[1]); both["ruleId"] = "rule-panic"; both["actionData"] = {**soft[1]["actionData"], "id": "y"}
    respx.get(f"{NX}/rest/v4/events/log").mock(return_value=httpx.Response(200, json=[soft[0], soft[1], both]))

    created = await manager.poll_once(manager._runtime(site))

    by_ts = {a.event_ts_ms: a for a in created}
    assert (by_ts[6_001_000].priority, by_ts[6_001_000].level_source) == (3, "rule_tag")
    assert (by_ts[6_002_000].priority, by_ts[6_002_000].level_source) == (1, "rule_tag")
    assert rules.called


@respx.mock
async def test_editing_a_rule_tag_relevels_open_alarms_within_a_poll(session, admin, monkeypatch):
    from app.config import get_settings
    from app.models import Alarm
    monkeypatch.setattr(get_settings(), "rules_refresh_s", 0)
    tenant, _ = admin
    site = await make_site(session, tenant, cursor=7_000_000)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[]))
    rules = respx.get(f"{NX}/rest/v4/events/rules")
    rules.mock(return_value=httpx.Response(200, json=[{"id": "r1", "comment": ""}]))
    row = nx_row(7_001_000, type_="softTrigger"); row["ruleId"] = "r1"
    events = respx.get(f"{NX}/rest/v4/events/log")
    events.mock(return_value=httpx.Response(200, json=[row]))
    rt = manager._runtime(site)
    [a] = await manager.poll_once(rt)
    assert (a.priority, a.level_source) == (1, "default")

    # The operator adds "#warning" to the rule in NX; the next poll picks it up.
    rules.mock(return_value=httpx.Response(200, json=[{"id": "r1", "comment": "Truss 8 test #warning"}]))
    events.mock(return_value=httpx.Response(200, json=[]))
    sub = bus.subscribe(tenant.id)
    await manager.poll_once(rt)
    stored = (await session.execute(select(Alarm).execution_options(populate_existing=True))).scalar_one()
    assert (stored.priority, stored.level_source) == (3, "rule_tag")
    assert "alarms.reload" in [sub.queue.get_nowait()[0] for _ in range(sub.queue.qsize())]
    bus.unsubscribe(sub)


@respx.mock
async def test_forbidden_rules_back_off_quietly(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant, cursor=8_000_000)
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/devices").mock(return_value=httpx.Response(200, json=[]))
    rules = respx.get(f"{NX}/rest/v4/events/rules").mock(return_value=httpx.Response(403))
    row = nx_row(8_001_000, type_="softTrigger"); row["ruleId"] = "r1"
    respx.get(f"{NX}/rest/v4/events/log").mock(return_value=httpx.Response(200, json=[row]))
    rt = manager._runtime(site)
    [a] = await manager.poll_once(rt)
    await manager.poll_once(rt)
    assert a.level_source == "default" and rt.rules_forbidden and rules.call_count == 1


async def test_polls_fast_while_push_is_down(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    rt = manager._runtime(site)
    rt.push_connected = False
    assert manager.poll_delay(rt) == 1.0          # e.g. the ~10 s NX takes to set up push after a restart
    rt.push_connected = True
    assert manager.poll_delay(rt) == 5.0

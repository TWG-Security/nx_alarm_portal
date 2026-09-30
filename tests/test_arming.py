from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Alarm, AuditLog, Site, User
from app.security import hash_password
from app.services import arming
from app.services.arming import clean_entries, state_at
from app.services.bus import bus
from app.services.poller import ingest
from tests.conftest import PASSWORD, login, make_site, nx_row

NY = "America/New_York"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
# Business hours: disarmed 07:00-18:00 on weekdays, armed nights and weekends.
OFFICE = {"entries": [{"action": "arm", "time": "18:00", "days": WEEKDAYS},
                      {"action": "disarm", "time": "07:00", "days": WEEKDAYS}], "since_ms": 0}


def ms(y, mo, d, h=0, mi=0, tz=NY) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


# 2026-09-30 is a Wednesday.
def test_no_schedule_no_override_is_armed():
    st = state_at(None, None, NY, ms(2026, 9, 30, 12))
    assert (st.armed, st.source, st.next_ms) == (True, "default", None)


def test_weekly_schedule_in_site_time_zone():
    wed_noon = state_at(OFFICE, None, NY, ms(2026, 9, 30, 12))
    assert (wed_noon.armed, wed_noon.source, wed_noon.since_ms) == (False, "schedule", ms(2026, 9, 30, 7))
    assert (wed_noon.next_armed, wed_noon.next_ms) == (True, ms(2026, 9, 30, 18))
    assert state_at(OFFICE, None, NY, ms(2026, 9, 30, 18)).armed            # exactly at the arm time
    assert not state_at(OFFICE, None, NY, ms(2026, 9, 30, 17, 59)).armed
    sat = state_at(OFFICE, None, NY, ms(2026, 10, 3, 12))                   # weekend: armed since Friday 18:00
    assert (sat.armed, sat.since_ms, sat.next_ms) == (True, ms(2026, 10, 2, 18), ms(2026, 10, 5, 7))
    # The same instant is 16:00 UTC; a UTC site would already be armed at 18:00 UTC = 14:00 New York.
    assert state_at(OFFICE, None, "UTC", ms(2026, 9, 30, 14, 30)).armed


def test_manual_disarm_holds_until_next_scheduled_arm():
    override = {"armed": False, "at_ms": ms(2026, 9, 30, 20), "by": "op", "note": "cleaning crew"}
    st = state_at(OFFICE, override, NY, ms(2026, 9, 30, 23))
    assert (st.armed, st.source, st.by, st.note) == (False, "manual", "op", "cleaning crew")
    # Thursday 07:00 is a disarm (no change), so the next flip is Thursday 18:00.
    assert (st.next_armed, st.next_ms, st.next_source) == (True, ms(2026, 10, 1, 18), "schedule")
    assert state_at(OFFICE, override, NY, ms(2026, 10, 1, 18)).source == "schedule"


def test_timed_disarm_rearms_by_itself():
    override = {"armed": False, "at_ms": ms(2026, 9, 30, 20), "until_ms": ms(2026, 9, 30, 22)}
    st = state_at(None, override, NY, ms(2026, 9, 30, 21))
    assert (st.armed, st.until_ms, st.next_ms, st.next_source) == (False, ms(2026, 9, 30, 22), ms(2026, 9, 30, 22), "timer")
    after = state_at(None, override, NY, ms(2026, 9, 30, 22))
    assert (after.armed, after.source) == (True, "timer")


def test_timer_is_dropped_when_the_schedule_takes_over_first():
    # Disarmed 17:00 for 3 h; the 18:00 scheduled arm wins, and 20:00 must not re-arm a later schedule disarm.
    sched = {"entries": [{"action": "arm", "time": "18:00", "days": ["wed"]},
                         {"action": "disarm", "time": "19:00", "days": ["wed"]}], "since_ms": 0}
    override = {"armed": False, "at_ms": ms(2026, 9, 30, 17), "until_ms": ms(2026, 9, 30, 20)}
    assert state_at(sched, override, NY, ms(2026, 9, 30, 18, 30)).armed
    late = state_at(sched, override, NY, ms(2026, 9, 30, 20, 30))
    assert (late.armed, late.source) == (False, "schedule")


def test_schedule_edits_ignore_occurrences_before_the_edit():
    sched = {**OFFICE, "since_ms": ms(2026, 9, 30, 10)}                     # saved Wednesday 10:00
    st = state_at(sched, None, NY, ms(2026, 9, 30, 12))
    # Wednesday 07:00 predates the edit, so the site stays armed; 18:00 changes nothing; Thursday 07:00 disarms.
    assert (st.armed, st.source, st.next_armed, st.next_ms) == (True, "default", False, ms(2026, 10, 1, 7))


def test_dst_days_fire_each_entry_once():
    sched = {"entries": [{"action": "disarm", "time": "02:30", "days": ["sun"]},
                         {"action": "arm", "time": "01:30", "days": ["sun"]}], "since_ms": 0}
    tz = arming.zone(NY)
    spring = arming._occurrences(sched, tz, ms(2026, 3, 8), ms(2026, 3, 9))       # 02:00 -> 03:00
    fall = arming._occurrences(sched, tz, ms(2026, 11, 1), ms(2026, 11, 2))       # 02:00 -> 01:00
    assert len(spring) == 2 and len(fall) == 2
    assert datetime.fromtimestamp(spring[1][0] / 1000, tz).strftime("%H:%M") == "03:30"   # skipped time runs late


def test_clean_entries_validates():
    assert clean_entries([{"action": "ARM", "time": "18:00", "days": ["fri", "mon"]}]) == \
        [{"action": "arm", "time": "18:00", "days": ["mon", "fri"]}]
    for bad, msg in [({"action": "x", "time": "18:00", "days": ["mon"]}, "arm or disarm"),
                     ({"action": "arm", "time": "25:00", "days": ["mon"]}, "HH:MM"),
                     ({"action": "arm", "time": "18:00", "days": []}, "at least one day")]:
        with pytest.raises(ValueError, match=msg):
            clean_entries([bad])
    with pytest.raises(ValueError, match="both arm and disarm"):
        clean_entries([{"action": "arm", "time": "18:00", "days": ["mon"]},
                       {"action": "disarm", "time": "18:00", "days": ["mon", "tue"]}])


def test_24h_tag():
    assert arming.rule_always_armed("Lobby panic #24h #critical")
    assert not arming.rule_always_armed("no tag") and not arming.rule_always_armed("#24hours")


# ------------------------------------------------------------------ ingest

async def _disarmed_site(session, tenant, at=1_000_000):
    site = await make_site(session, tenant)
    site.arm_override = {"armed": False, "at_ms": at}
    await session.commit()
    return site


async def test_disarmed_site_stores_security_events_without_raising(session, admin):
    tenant, _ = admin
    site = await _disarmed_site(session, tenant)
    sub = bus.subscribe(tenant.id)
    raised = await ingest(session, site, [
        nx_row(2_000_000, action_id="a", caption="Intrusion"),
        nx_row(2_000_100, action_id="b", type_="storageIssue", caption="Disk failed"),
    ], {}, now=2_000_200)
    await session.commit()
    assert [a.event_type for a in raised] == ["storageIssue"]              # system events always raise
    stored = (await session.scalars(select(Alarm).where(Alarm.event_type == "generic"))).one()
    assert stored.state == "disarmed"
    log = (await session.scalars(select(AuditLog).where(AuditLog.alarm_id == stored.id))).one()
    assert log.detail["site_disarmed"] is True
    assert sub.queue.qsize() == 0                                          # ingest itself never announces
    bus.unsubscribe(sub)


async def test_event_raises_if_armed_at_event_time_or_on_arrival(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.arm_override = {"armed": False, "at_ms": 5_000_000}                # disarmed at 5 000 000
    await session.commit()
    [late] = await ingest(session, site, [nx_row(4_999_000, action_id="late")], {}, now=5_000_500)
    assert late.state == "new"                                             # happened while armed, arrived after


async def test_24h_rule_raises_while_disarmed_even_if_its_row_arrives_second(session, admin):
    tenant, _ = admin
    site = await _disarmed_site(session, tenant)
    email = {**nx_row(3_000_000, action_id="email", action_type="sendEmail"), "ruleId": "rule-email"}
    panic = {**nx_row(3_000_000, action_id="notify"), "ruleId": "{rule-panic}"}
    assert await ingest(session, site, [email], {}, always_armed={"rule-panic"}, now=3_000_100) == []
    await session.commit()
    [raised] = await ingest(session, site, [panic], {}, always_armed={"rule-panic"}, now=3_000_200)
    await session.commit()
    assert raised.state == "new"
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "alarm.raised"))
    # Straight away when the #24h row is first.
    [direct] = await ingest(session, site, [{**nx_row(3_100_000, action_id="n2"), "ruleId": "rule-panic"}], {},
                            always_armed={"rule-panic"}, now=3_100_100)
    assert direct.state == "new"


# ------------------------------------------------------------------ API + scheduler

async def test_operator_disarms_with_timer_and_arms(client, session, admin):
    tenant, _ = admin
    op = User(tenant_id=tenant.id, email="op@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    site = await make_site(session, tenant)
    await login(client, op.email)

    r = await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 90, "note": "Customer on site"})
    assert r.status_code == 200, r.text
    a = r.json()["arming"]
    assert (a["armed"], a["source"], a["note"], a["next_source"]) == (False, "manual", "Customer on site", "timer")
    assert a["until_ms"] - a["since_ms"] == 90 * 60_000
    r = await client.post(f"/api/sites/{site.id}/arm", json={})
    assert r.json()["arming"]["armed"] is True
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.site_id == site.id)
                                     .order_by(AuditLog.id))).all()
    assert actions == ["site.disarmed", "site.armed"]
    assert (await client.post(f"/api/sites/{site.id}/disarm", json={"minutes": 0})).status_code == 422
    # Operators can't reach the admin-only site ops.
    assert (await client.post(f"/api/sites/{site.id}/disable")).status_code == 403


async def test_schedule_saved_from_site_form_keeps_current_state(client, session, admin):
    tenant, user = admin
    site = await make_site(session, tenant)
    await login(client, user.email)
    body = {"name": site.name, "host": site.host, "nx_user": site.nx_user, "timezone": NY,
            "arm_schedule": [{"action": "disarm", "time": "00:00", "days": arming.DAYS},
                             {"action": "arm", "time": "23:59", "days": arming.DAYS}]}
    r = await client.put(f"/api/sites/{site.id}", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["timezone"] == NY and len(data["arm_schedule"]) == 2
    assert data["arming"]["armed"] is True and data["arming"]["next_armed"] is False   # no flip on save
    assert (await client.put(f"/api/sites/{site.id}", json={**body, "timezone": "Mars/Base"})).status_code == 400
    bad = {**body, "arm_schedule": [{"action": "arm", "time": "7pm", "days": ["mon"]}]}
    r = await client.put(f"/api/sites/{site.id}", json=bad)
    assert r.status_code == 400 and "HH:MM" in r.text
    r = await client.put(f"/api/sites/{site.id}", json={**body, "arm_schedule": None})   # None keeps it
    assert len(r.json()["arm_schedule"]) == 2


async def test_scheduler_announces_scheduled_changes(session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.timezone = "UTC"
    site.arm_schedule = {"entries": [{"action": "disarm", "time": "07:00", "days": list(arming.DAYS)}], "since_ms": 0}
    await session.commit()
    sub = bus.subscribe(tenant.id)
    now = ms(2026, 9, 30, 8, tz="UTC")
    assert await arming.check_all(now) == 1
    assert await arming.check_all(now + 1000) == 0                          # announced once
    event, data = sub.queue.get_nowait()
    assert event == "site.updated" and data["arming"]["armed"] is False
    await session.refresh(site)
    assert site.armed is False
    row = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.disarmed"))).one()
    assert row.detail["source"] == "schedule" and row.user_id is None
    bus.unsubscribe(sub)


async def test_disarmed_alarms_listed_separately_and_not_ackable(client, session, admin):
    tenant, user = admin
    site = await _disarmed_site(session, tenant)
    await ingest(session, site, [nx_row(2_000_000)], {}, now=2_000_100)
    await session.commit()
    stored = await session.scalar(select(Alarm))
    await login(client, user.email)
    assert (await client.get("/api/alarms?state=open")).json() == []
    assert [a["id"] for a in (await client.get("/api/alarms?state=disarmed")).json()] == [stored.id]
    assert (await client.post(f"/api/alarms/{stored.id}/ack", json={"note": ""})).status_code == 409
    assert (await client.get("/api/summary")).json()["open_alarm"] == 0
    assert (await session.get(Site, site.id)).armed is True                 # column only moves via announce

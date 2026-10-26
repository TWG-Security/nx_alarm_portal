"""Client profile per site (app/services/profile.py): package, contacts, police number; call buttons logged."""
from sqlalchemy import select

from app.models import Alarm, AuditLog, User
from app.security import hash_password
from app.services.serialize import alarm_dict
from tests.conftest import PASSWORD, login, make_site

PROFILE = {"client_name": "J. Smith", "phone": "416-555-0100", "package": "monitoring_only", "police_phone": "416-808-2222",
           "passcode": "bluebird", "notes": "Dog in the yard", "ajax_url": "https://app.ajax.systems/hub/123",
           "contacts": [{"name": "Ann Smith", "role": "spouse", "phone": "+1 416 555 0101"}, {"name": "", "phone": ""}]}


async def test_admin_sets_profile_operator_cannot(client, session, admin):
    tenant, _ = admin
    op = User(tenant_id=tenant.id, email="op@twgsecurity.com", password_hash=hash_password(PASSWORD), role="operator")
    session.add(op)
    site = await make_site(session, tenant)
    await login(client, "admin@twgsecurity.com")

    r = await client.put(f"/api/sites/{site.id}/profile", json=PROFILE)
    assert r.status_code == 200, r.text
    p = r.json()["profile"]
    assert (p["package"], p["package_label"], len(p["contacts"])) == ("monitoring_only", "Monitoring only", 1)
    row = (await session.scalars(select(AuditLog).where(AuditLog.action == "site.profile_updated"))).one()
    assert "passcode" in row.detail["fields"] and "bluebird" not in str(row.detail)        # names only, never the values
    assert (await client.put(f"/api/sites/{site.id}/profile", json={**PROFILE, "package": "gold"})).status_code == 422
    assert (await client.put(f"/api/sites/{site.id}/profile", json={**PROFILE, "phone": "call me"})).status_code == 422
    assert (await client.put(f"/api/sites/{site.id}/profile", json={**PROFILE, "ajax_url": "javascript:alert(1)"})).status_code == 422
    # Saving it unchanged logs nothing; clearing everything stores nothing.
    await client.put(f"/api/sites/{site.id}/profile", json=PROFILE)
    assert len((await session.scalars(select(AuditLog).where(AuditLog.action == "site.profile_updated"))).all()) == 1
    r = await client.put(f"/api/sites/{site.id}/profile", json={})
    assert r.json()["profile"] == {"package_label": ""}

    await login(client, op.email)
    assert (await client.put(f"/api/sites/{site.id}/profile", json=PROFILE)).status_code == 403


async def test_call_buttons_are_logged_on_the_timeline(client, session, admin):
    tenant, _ = admin
    site = await make_site(session, tenant)
    site.profile = {"package": "ground_response", "phone": "416-555-0100"}
    alarm = Alarm(tenant_id=tenant.id, site_id=site.id, event_key="k", state="new", category="security", priority=2,
                  event_type="analytics", caption="Line crossed", event_ts_ms=1, raw={})
    session.add(alarm)
    await session.commit()
    await session.refresh(alarm, ["site"])
    assert alarm_dict(alarm)["site_package"] == "ground_response"
    await login(client, "admin@twgsecurity.com")
    r = await client.post(f"/api/alarms/{alarm.id}/called", json={"to": "police", "name": "Police", "number": "911"})
    assert r.status_code == 200
    assert (await client.post(f"/api/alarms/{alarm.id}/called", json={"to": "pizza"})).status_code == 422
    tl = (await client.get(f"/api/alarms/{alarm.id}/timeline")).json()
    rows = tl if isinstance(tl, list) else tl.get("rows") or tl.get("timeline") or []
    assert any(x.get("what") == "Called the police" and "911" in x.get("detail", "") for x in rows)

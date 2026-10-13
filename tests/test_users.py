"""Deleting users (api.delete_user): gone from Users and groups, locked out everywhere, no more texts,
the email can be reused, and alarm history still shows who handled what."""
import httpx
from sqlalchemy import select

from app.models import AuditLog, MfaRecoveryCode, Tenant, User, UserGroup, UserGroupMember
from app.security import hash_password
from app.services.poller import ingest
from tests.conftest import PASSWORD, login, make_site, make_tenant_user, nx_row


async def _user(session, tenant, email, role="operator", **kw):
    u = User(tenant_id=tenant.id, email=email, display_name=email.split("@")[0].title(),
             password_hash=hash_password(PASSWORD), role=role, **kw)
    session.add(u)
    await session.commit()
    return u


async def test_deleting_a_user_locks_them_out_and_keeps_history(client, session):
    from app.main import app
    tenant, admin = await make_tenant_user(session)
    op = await _user(session, tenant, "night@twgsecurity.com", phone="+15705551234", sms_level="alarm", sms_consent="yes")
    group = UserGroup(tenant_id=tenant.id, name="Supervisors", permissions=["alarms.bulk_edit"])
    session.add(group)
    await session.flush()
    session.add_all([UserGroupMember(group_id=group.id, user_id=op.id), MfaRecoveryCode(user_id=op.id, code_hash="x" * 64)])
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(4_000_000)], {})
    await session.commit()

    # The operator is signed in elsewhere and acknowledges an alarm.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://portal.test") as theirs:
        await login(theirs, op.email)
        assert (await theirs.post(f"/api/alarms/{alarm.id}/ack", json={"verdict": "false", "note": "wind"})).status_code == 200

        await login(client, admin.email)
        r = await client.delete(f"/api/users/{op.id}")
        assert r.status_code == 200, r.text

        assert (await theirs.get("/api/alarms?state=all")).status_code == 401         # signed out at once
    assert [u["email"] for u in (await client.get("/api/users")).json()] == [admin.email]
    assert (await client.get("/api/groups")).json()["groups"][0]["member_ids"] == []
    assert (await client.get(f"/api/alarms/{alarm.id}")).json()["acked_by"] == "Night"   # history keeps the name
    for method in ("PUT", "DELETE"):
        assert (await client.request(method, f"/api/users/{op.id}", json={"display_name": "x"} if method == "PUT" else None)).status_code == 404

    op_id, admin_id = op.id, admin.id
    session.expire_all()
    gone = await session.scalar(select(User).where(User.id == op_id))
    assert gone.deleted_at and not gone.is_active and gone.phone == "" and gone.sms_level == "off"
    assert gone.email.endswith("@deleted.invalid") and gone.password_hash == "!deleted"
    assert not (await session.scalars(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == op_id))).all()
    row = await session.scalar(select(AuditLog).where(AuditLog.action == "user.deleted"))
    assert row.detail["target"] == "night@twgsecurity.com" and row.user_id == admin_id

    # The old email can't sign in, and can be added again as a new person.
    client.cookies.clear()
    page = await client.get("/login")
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = await client.post("/login", data={"email": "night@twgsecurity.com", "password": PASSWORD, "csrf_token": csrf})
    assert r.status_code != 303
    await login(client, "admin@twgsecurity.com")
    r = await client.post("/api/users", json={"email": "night@twgsecurity.com", "password": "A-long-password-1!"})
    assert r.status_code == 200 and r.json()["id"] != op_id


async def test_deleted_users_texted_links_stop_working(client, session):
    from app import alarm_links
    tenant, admin = await make_tenant_user(session)
    op = await _user(session, tenant, "guard@twgsecurity.com")
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(4_000_000)], {})
    await session.commit()
    token = await alarm_links.mint(session, alarm, op, 24)
    await session.commit()
    assert (await client.get(f"/a/{token}")).status_code == 200
    await login(client, admin.email)
    assert (await client.delete(f"/api/users/{op.id}")).status_code == 200
    client.cookies.clear()
    assert (await client.get(f"/a/{token}")).status_code in (404, 410)


async def test_you_cant_delete_yourself_or_the_last_admin(client, session):
    tenant, admin = await make_tenant_user(session)
    await login(client, admin.email)
    r = await client.delete(f"/api/users/{admin.id}")
    assert r.status_code == 400 and "your own" in r.text

    # TWG support deleting a customer's only admin is refused; with a second admin it's allowed.
    tenant.kind = "platform"
    await session.commit()
    acme = Tenant(name="Acme", kind="customer")
    session.add(acme)
    await session.commit()
    only = await _user(session, acme, "boss@acme.test", role="admin")
    await client.post("/api/scope", json={"scope": str(acme.id)})
    r = await client.delete(f"/api/users/{only.id}")
    assert r.status_code == 400 and "only admin" in r.text
    await _user(session, acme, "deputy@acme.test", role="admin")
    assert (await client.delete(f"/api/users/{only.id}")).status_code == 200


async def test_operators_cant_delete_users(client, session):
    tenant, admin = await make_tenant_user(session)
    op = await _user(session, tenant, "noc@twgsecurity.com")
    await login(client, op.email)
    assert (await client.delete(f"/api/users/{admin.id}")).status_code == 403

"""How long a browser stays signed in (app/sessions.py): an open portal page stays signed in, and Monitoring station
users skip the idle and max-age limits. The user, 2026-10-15: "this is monitoring software i dont want operators to
log out while monitoring"."""
import base64
import json
import time
from datetime import timedelta

import itsdangerous
from sqlalchemy import select

from app import sessions
from app.config import get_settings
from app.models import User, UserSession
from tests.conftest import login
from tests.test_two_factor import settings_row, twg  # noqa: F401 - fixture


def _cookie_signed(client, hours_ago: float) -> str:
    """This browser's session cookie, signed as if hours_ago."""
    signer = itsdangerous.TimestampSigner(get_settings().secret_key)
    data = signer.unsign(client.cookies["twg_portal"])

    class Then(itsdangerous.TimestampSigner):
        def get_timestamp(self):
            return int(time.time() - hours_ago * 3600)
    return Then(get_settings().secret_key).sign(data).decode()


async def _row(session, uid) -> UserSession:
    session.expire_all()
    return await session.scalar(select(UserSession).where(UserSession.user_id == uid, UserSession.revoked_at.is_(None)))


async def test_an_open_screen_stays_signed_in_past_12_hours(client, session, twg):  # noqa: F811
    """The bug: the cookie was signed once at sign-in with a 12 h lifetime and never renewed, so every screen was
    signed out 12 h after login however busy it was."""
    await login(client, twg[1].email)
    client.cookies.set("twg_portal", _cookie_signed(client, 13))           # signed in 13 h ago, page open since
    assert (await client.get("/api/sites")).status_code == 200


async def test_the_cookie_is_renewed_while_the_page_is_open(client, session, twg):  # noqa: F811
    uid = twg[1].id
    await login(client, twg[1].email)
    r = await client.get("/api/sites")
    assert "set-cookie" not in r.headers                                    # not on every request
    row = await _row(session, uid)
    row.last_seen_at = sessions.utcnow() - timedelta(seconds=sessions.SEEN_EVERY_S + 5)
    await session.commit()
    r = await client.get("/api/sites")
    assert r.status_code == 200 and "twg_portal=" in r.headers.get("set-cookie", "")
    payload = json.loads(base64.b64decode(itsdangerous.TimestampSigner(get_settings().secret_key).unsign(client.cookies["twg_portal"])))
    assert payload["sid"] == row.id and payload["uid"] == uid         # the same session, freshly signed
    max_age = int(r.headers["set-cookie"].split("Max-Age=")[1].split(";")[0])
    assert max_age >= 90 * 24 * 3600                                        # outlives any "closed browser" setting


async def test_a_closed_browser_still_signs_out(client, session, twg):  # noqa: F811
    """Monitoring station or not, a browser closed longer than the limit must sign in again."""
    twg[1].monitoring_station = True
    await session.commit()
    await login(client, twg[1].email)
    row = await _row(session, twg[1].id)
    row.last_seen_at = sessions.utcnow() - timedelta(hours=13)
    await session.commit()
    assert (await client.get("/api/sites")).status_code == 401


async def test_monitoring_station_skips_max_age_and_idle(client, session, twg):  # noqa: F811
    t, admin = twg
    admin_email = admin.email
    op = User(tenant_id=t.id, email="desk@twgsecurity.com", display_name="Desk", password_hash=admin.password_hash, role="operator")
    session.add(op)
    await session.commit()
    op_id = op.id
    await settings_row(session, session_max_h=8, idle_timeout_min=30)
    try:
        # An ordinary operator: idle sign-out on the page, and signed out 8 h after signing in.
        await login(client, "desk@twgsecurity.com")
        assert '"idleMin": 30' in (await client.get("/")).text
        row = await _row(session, op_id)
        row.created_at, sid = sessions.utcnow() - timedelta(hours=9), row.id
        await session.commit()
        assert (await client.get("/api/sites")).status_code == 401
        session.expire_all()
        assert (await session.get(UserSession, sid)).revoked_reason == "max_age"

        # The admin ticks Monitoring station on the Users page.
        await login(client, admin_email)
        r = await client.put(f"/api/users/{op_id}", json={"monitoring_station": True})
        assert r.status_code == 200 and r.json()["monitoring_station"] is True
        listed = {u["email"]: u for u in (await client.get("/api/users")).json()}
        assert listed["desk@twgsecurity.com"]["monitoring_station"] is True

        await login(client, "desk@twgsecurity.com")
        assert '"idleMin": 0' in (await client.get("/")).text                # no idle watcher on their pages
        row = await _row(session, op_id)
        row.created_at = sessions.utcnow() - timedelta(hours=30)
        await session.commit()
        assert (await client.get("/api/sites")).status_code == 200            # past max age: still signed in

        # Created with it ticked; and switched off again.
        await login(client, admin_email)
        r = await client.post("/api/users", json={"email": "wall@twgsecurity.com", "password": "Wall-screen-pass-123!", "monitoring_station": True})
        assert r.status_code in (200, 201), r.text
        session.expire_all()
        assert (await session.scalar(select(User).where(User.email == "wall@twgsecurity.com"))).monitoring_station
        assert (await client.put(f"/api/users/{op_id}", json={"monitoring_station": False})).json()["monitoring_station"] is False
    finally:
        await settings_row(session, session_max_h=0, idle_timeout_min=0)

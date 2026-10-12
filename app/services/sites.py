"""Site connection helpers: relay host building and the "Connect" test."""

import re

import httpx

from app.config import get_settings
from app.nx.client import NXClient

UUID_RE = re.compile(r"^\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?$")
RELAY_RE = re.compile(r"^https?://([0-9a-f-]{36})\.relay[\w-]*\.vmsproxy\.com", re.I)


class ConnectError(Exception):
    def __init__(self, message: str, status: str = "offline"):
        super().__init__(message)
        self.status = status


def resolve_host(value: str) -> tuple[str, str | None]:
    """Accepts a bare Nx Cloud id or a full URL. Returns (host_url, cloud_id)."""
    value = (value or "").strip().rstrip("/")
    m = UUID_RE.match(value)
    if m:
        cloud_id = m.group(1).lower()
        return f"https://{cloud_id}.relay.vmsproxy.com", cloud_id
    if not value.startswith(("https://", "http://")):
        raise ValueError("Enter an Nx Cloud ID (UUID) or a full https:// URL")
    m = RELAY_RE.match(value)
    return value, (m.group(1).lower() if m else None)


def make_client(host: str, user: str, password: str) -> NXClient:
    return NXClient(host, user, password, timeout=get_settings().nx_timeout_s)


def describe_http_error(exc: Exception) -> ConnectError:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return ConnectError("NX rejected the username or password", "auth_error")
        return ConnectError(f"NX returned HTTP {code}")
    if isinstance(exc, httpx.TimeoutException):
        return ConnectError("Timed out reaching the NX server")
    if isinstance(exc, httpx.HTTPError):
        return ConnectError(f"Could not reach the NX server ({exc.__class__.__name__})")
    return ConnectError(str(exc) or exc.__class__.__name__)


async def probe(client: NXClient) -> dict:
    """Logs in and reads site identity + camera count. Raises ConnectError."""
    try:
        await client._login()
        info = await client.get_site_info()
        devices = await client.list_devices()
    except Exception as exc:  # noqa: BLE001 — every failure becomes a readable message
        raise describe_http_error(exc) from exc
    cameras = [d for d in devices or [] if (d.get("deviceType") or "Camera").lower() != "server"]
    return {
        "nx_site_name": info.get("name", ""),
        "nx_version": info.get("version", ""),
        "cloud_id": info.get("cloudId") or client.relay_cloud_id(),
        "camera_count": len(cameras),
        "devices": {(d.get("id") or "").strip("{}"): d.get("name", "") for d in devices or []},
    }

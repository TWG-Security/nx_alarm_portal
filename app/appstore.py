"""App Store Connect API: TestFlight invites for the iPhone app (POST /api/apps/invite, app/routers/apps.py).

An ES256 JWT (kid = the key's ID, iss = the issuer ID, aud appstoreconnect-v1, 20 min) from the .p8 at ASC_KEY_PATH;
the beta group is looked up by name under the app (platform_settings.asc_app_id / asc_beta_group). Apple emails the
TestFlight invitation itself when a tester is added to a group with an internal/external build available.

invite() answers "invited" | "already_tester", or raises InviteError with Apple's own error text.
Not yet run against Apple (2026-10-21): written from the App Store Connect API docs, tested against a fake.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings

TOKEN_S = 1200                 # Apple's maximum lifetime
_token: tuple[str, float, str] | None = None
_groups: dict[tuple[str, str], str] = {}


class InviteError(Exception):
    pass


@dataclass
class AscConfig:
    issuer_id: str
    key_id: str
    key_pem: bytes
    app_id: str
    group: str


def key_status() -> dict:
    """For the Platform page: is the key installed (never the key itself)?"""
    cfg = get_settings()
    path = Path(cfg.asc_key_path) if cfg.asc_key_path else None
    return {"installed": bool(path and path.is_file()), "key_id": cfg.asc_key_id}


def config(row) -> AscConfig | None:
    """From platform_settings + the key file; None when anything is missing."""
    cfg = get_settings()
    if not (row and row.asc_issuer_id and row.asc_app_id and cfg.asc_key_id and cfg.asc_key_path):
        return None
    try:
        pem = Path(cfg.asc_key_path).read_bytes()
    except OSError:
        return None
    return AscConfig(row.asc_issuer_id.strip(), cfg.asc_key_id, pem, row.asc_app_id.strip(),
                     (row.asc_beta_group or "TWG Testers").strip())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token(c: AscConfig) -> str:
    global _token
    if _token and _token[2] == f"{c.issuer_id}/{c.key_id}" and time.time() - _token[1] < TOKEN_S - 120:
        return _token[0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    now = int(time.time())
    head = _b64(json.dumps({"alg": "ES256", "kid": c.key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iss": c.issuer_id, "iat": now, "exp": now + TOKEN_S, "aud": "appstoreconnect-v1"},
                           separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(c.key_pem, password=None)
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    _token = (jwt, time.time(), f"{c.issuer_id}/{c.key_id}")
    return jwt


def _client(c: AscConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=get_settings().asc_api_base, timeout=20,
                             headers={"Authorization": f"Bearer {token(c)}", "Content-Type": "application/json"})


def _apple_error(r: httpx.Response) -> str:
    try:
        e = (r.json().get("errors") or [{}])[0]
        return e.get("detail") or e.get("title") or f"HTTP {r.status_code}"
    except ValueError:
        return f"HTTP {r.status_code}"


async def _check(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise InviteError(f"App Store Connect ({what}): {_apple_error(r)}")
    return r.json() if r.content else {}


async def group_id(gh: httpx.AsyncClient, c: AscConfig) -> str:
    key = (c.app_id, c.group)
    if key not in _groups:
        r = await gh.get("/v1/betaGroups", params={"filter[app]": c.app_id, "filter[name]": c.group, "limit": 50})
        found = [g for g in (await _check(r, "beta groups"))["data"] if (g.get("attributes") or {}).get("name") == c.group]
        if not found:
            raise InviteError(f'App Store Connect has no beta group named "{c.group}" for this app')
        _groups[key] = found[0]["id"]
    return _groups[key]


def _names(name: str, email: str) -> tuple[str, str]:
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return email.split("@")[0][:50], ""
    return parts[0][:50], (parts[1] if len(parts) > 1 else "")[:50]


async def invite(c: AscConfig, email: str, name: str = "") -> str:
    async with _client(c) as gh:
        gid = await group_id(gh, c)
        r = await gh.get("/v1/betaTesters", params={"filter[email]": email, "filter[betaGroups]": gid, "limit": 1})
        if (await _check(r, "testers"))["data"]:
            return "already_tester"
        first, last = _names(name, email)
        r = await gh.post("/v1/betaTesters", json={"data": {
            "type": "betaTesters", "attributes": {"email": email, "firstName": first, "lastName": last},
            "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": gid}]}}}})
        if r.status_code == 409:
            # A tester already (another group or app): add them to this group.
            r2 = await gh.get("/v1/betaTesters", params={"filter[email]": email, "limit": 1})
            data = (await _check(r2, "testers"))["data"]
            if not data:
                raise InviteError(f"App Store Connect: {_apple_error(r)}")
            r3 = await gh.post(f"/v1/betaGroups/{gid}/relationships/betaTesters",
                               json={"data": [{"type": "betaTesters", "id": data[0]["id"]}]})
            await _check(r3, "add to group")
            return "invited"
        await _check(r, "invite")
        return "invited"

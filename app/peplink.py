"""Peplink InControl2 (IC2) API client: GPS positions of Peplink routers, used for live guard locations.

Verified 2026-10-13 against TWG's IC2 (through the TWG MCP gateway):
  - GET /rest/o/{org}/g/{group}/d/{device}/loc -> [{la, lo, sp (m/s), at (m), ts, isExist, isStatic}]
  - GET /rest/o/{org}/g/{group}/d/{device}     -> device detail incl. gps_support / gps_exist,
    latitude / longitude / location_timestamp and group_timezone_label (e.g. "America/New_York").
    This payload also holds the router's admin password (admin_conf): only the fields below are ever kept.
  - Timestamps ("2026-10-13T13:57:52") are the GROUP's local time with no zone: converted with the
    group's time zone. Routers report a new position to IC2 about every 1-2 minutes.
  - GET /rest/o/{org}/loc (all devices' locations, one call per organization) is used to list GPS routers.
Not verified with the portal's own credentials yet: the token call (OAuth2 client credentials at
/api/oauth2/token) and the response envelope {"stat": "ok", "response": ...}; both are handled loosely.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.config import get_settings

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REQUEST_MAX_S = 15.0           # hard cap per request, whatever the transport does with TIMEOUT
DEVICE_FIELDS = ("id", "group_id", "sn", "name", "status", "product_name", "gps_support", "gps_exist",
                 "latitude", "longitude", "location_timestamp", "group_timezone_label", "group_name")


class PeplinkError(Exception):
    """A problem talking to InControl2, worded for the Platform page."""


@dataclass
class Fix:
    lat: float
    lng: float
    at: datetime            # UTC
    speed: float | None     # m/s
    alt: float | None       # m


def to_utc(ts: str, tz: str | None) -> datetime | None:
    """IC2's zone-less local timestamp -> UTC, using the group's time zone (UTC if unknown)."""
    if not ts:
        return None
    try:
        local = datetime.fromisoformat(ts.replace("Z", ""))
    except ValueError:
        return None
    if local.tzinfo is None:
        try:
            local = local.replace(tzinfo=ZoneInfo(tz) if tz else timezone.utc)
        except (ZoneInfoNotFoundError, ValueError):
            local = local.replace(tzinfo=timezone.utc)
    return local.astimezone(timezone.utc)


def newest_fix(points: list, tz: str | None) -> Fix | None:
    """The latest real position in an IC2 points list (entries with isExist false have no position)."""
    best = None
    for p in points or []:
        if not isinstance(p, dict) or p.get("la") is None or p.get("lo") is None:
            continue
        at = to_utc(str(p.get("ts") or ""), tz)
        if at is None:
            continue
        fix = Fix(float(p["la"]), float(p["lo"]), at, _num(p.get("sp")), _num(p.get("at")))
        if best is None or fix.at > best.at:
            best = fix
    return best


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class Client:
    """One per set of credentials. The access token is cached until shortly before it expires."""

    def __init__(self, client_id: str, client_secret: str, base: str | None = None, transport=None):
        self.client_id, self.client_secret = client_id, client_secret
        self.base = (base or get_settings().peplink_api_base).rstrip("/")
        self._token, self._expires = "", 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": "TWG-Alarm-Portal"}, transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                r = await asyncio.wait_for(self._http.post(f"{self.base}/api/oauth2/token", data={
                    "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"}),
                    REQUEST_MAX_S)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                raise PeplinkError(f"Couldn't reach InControl2 ({type(e).__name__})") from None
            body = _json(r)
            if r.status_code >= 400 or not body.get("access_token"):
                raise PeplinkError("InControl2 refused the Client ID / Client Secret"
                                   + (f" ({body.get('error_description') or body.get('error') or r.status_code})" if body else ""))
            self._token = body["access_token"]
            self._expires = time.monotonic() + max(60, int(body.get("expires_in") or 3600) - 120)
            return self._token

    async def get(self, path: str, **params):
        for attempt in (1, 2):
            tok = await self.token()
            try:
                r = await asyncio.wait_for(self._http.get(f"{self.base}{path}", params=params or None,
                                                          headers={"Authorization": f"Bearer {tok}"}), REQUEST_MAX_S)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                raise PeplinkError(f"InControl2 didn't answer ({type(e).__name__})") from None
            if r.status_code == 401 and attempt == 1:
                self._token = ""                       # expired early: get a new one once
                continue
            body = _json(r)
            if r.status_code >= 400 or body.get("stat") == "fail":
                msg = body.get("message") or body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
                raise PeplinkError(f"InControl2: {msg} ({r.status_code}, {path.split('?')[0]})")
            return body["response"] if isinstance(body, dict) and "response" in body else body
        raise PeplinkError("InControl2 keeps refusing the access token")

    # ---- the calls the portal uses

    async def organizations(self) -> list[dict]:
        return [{"id": o.get("id"), "name": o.get("name")} for o in (await self.get("/rest/o") or []) if isinstance(o, dict)]

    async def org_locations(self, org_id: str) -> list[dict]:
        """Every device in an organization with its last position (name, ids, online status, points)."""
        rows = await self.get(f"/rest/o/{org_id}/loc")
        return [{"id": d.get("id"), "group_id": d.get("group_id"), "sn": d.get("sn"), "name": d.get("name"),
                 "status": d.get("status"), "product_name": d.get("product_name"), "points": d.get("points") or []}
                for d in rows or [] if isinstance(d, dict)]

    async def device(self, org_id: str, group_id: int, device_id: int) -> dict:
        d = await self.get(f"/rest/o/{org_id}/g/{group_id}/d/{device_id}") or {}
        return {k: d.get(k) for k in DEVICE_FIELDS}           # never the admin password etc.

    async def device_points(self, org_id: str, group_id: int, device_id: int) -> list[dict]:
        rows = await self.get(f"/rest/o/{org_id}/g/{group_id}/d/{device_id}/loc")
        return rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"response": body}

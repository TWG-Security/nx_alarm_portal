"""Fleetio API client: TWG's vans (and any company's fleet vehicles) on the map from Fleetio's last known positions.

From Fleetio's published OpenAPI spec (version 2025-05-05, https://developer.fleetio.com/schemas/2025-05-05.yaml),
not yet verified against a live account:
  - Base https://secure.fleetio.com/api; every call sends "Authorization: Token <API key>" and "Account-Token: <id>"
    (both from https://secure.fleetio.com/api_keys).
  - GET /vehicles?per_page=100[&start_cursor=...] -> {records: [VehicleSummary], next_cursor, ...}; archived vehicles
    are left out. Each record has id, name, license_plate, vin, year, make, model, group_name, vehicle_status_name,
    is_sample and current_location_entry {date, address, geolocation {latitude, longitude}} (null when Fleetio has no
    position yet). GET /vehicles/{id} returns the same for one vehicle.
  - Positions come from whatever feeds Fleetio (a telematics integration, Fleetio Go on the driver's phone, fuel /
    inspection entries), so how fresh they are depends on that feed; `date` says when each was recorded.
  - Rate limits depend on the Fleetio plan; a 429 carries Retry-After (seconds).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import get_settings

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REQUEST_MAX_S = 15.0
PAGE = 100                 # Fleetio's largest page
MAX_PAGES = 20             # 2,000 vehicles: far beyond a security company's fleet, and a cap on a runaway loop


class FleetioError(Exception):
    """A problem talking to Fleetio, worded for the Sites page."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class Vehicle:
    id: str
    name: str
    describe: str              # "2021 Ford Transit · ABC1234" (shown under the name)
    plate: str
    lat: float | None
    lng: float | None
    at: datetime | None        # when Fleetio recorded the position (UTC)
    address: str


def _parse_time(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def vehicle(rec: dict) -> Vehicle | None:
    """One Fleetio vehicle record -> Vehicle (None for Fleetio's sample data or a record without an id)."""
    if not isinstance(rec, dict) or rec.get("id") is None or rec.get("is_sample"):
        return None
    loc = rec.get("current_location_entry") or {}
    geo = loc.get("geolocation") or {}
    lat, lng = geo.get("latitude"), geo.get("longitude")
    ok = isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and -90 <= lat <= 90 and -180 <= lng <= 180 \
        and not (lat == 0 and lng == 0)
    plate = str(rec.get("license_plate") or "").strip()
    model = " ".join(str(x) for x in (rec.get("year"), rec.get("make"), rec.get("model")) if x)
    return Vehicle(id=str(rec["id"]), name=str(rec.get("name") or f"Vehicle {rec['id']}").strip()[:100],
                   describe=" · ".join(x for x in (model, plate) if x)[:200], plate=plate[:40],
                   lat=float(lat) if ok else None, lng=float(lng) if ok else None,
                   at=_parse_time(loc.get("date")) if ok else None, address=str(loc.get("address") or "")[:300])


class Client:
    def __init__(self, account_token: str, api_key: str, base: str | None = None, transport=None):
        self._base = (base or get_settings().fleetio_api_base).rstrip("/")
        self._http = httpx.AsyncClient(timeout=TIMEOUT, transport=transport, headers={
            "User-Agent": "TWG-Alarm-Portal", "Accept": "application/json",
            "Authorization": f"Token {api_key}", "Account-Token": account_token})

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None):
        try:
            r = await asyncio.wait_for(self._http.get(f"{self._base}{path}", params=params), REQUEST_MAX_S)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise FleetioError(f"Fleetio didn't answer ({type(e).__name__})") from None
        if r.status_code == 401:
            raise FleetioError("Fleetio refused the API key or Account Token (check both on secure.fleetio.com/api_keys)")
        if r.status_code == 403:
            raise FleetioError("This Fleetio API key isn't allowed to read vehicles (check its permissions in Fleetio)")
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "60"))
            except ValueError:
                wait = 60.0
            raise FleetioError(f"Fleetio's rate limit: try again in {int(wait)} s", retry_after_s=wait)
        if r.status_code >= 400:
            raise FleetioError(f"Fleetio answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise FleetioError("Fleetio sent something that isn't JSON") from None

    async def vehicles(self) -> list[Vehicle]:
        """Every (non-archived, non-sample) vehicle with its last known position."""
        out, cursor = [], None
        for _ in range(MAX_PAGES):
            page = await self._get("/vehicles", {"per_page": PAGE, **({"start_cursor": cursor} if cursor else {})})
            records = page.get("records") if isinstance(page, dict) else page
            if not isinstance(records, list):
                raise FleetioError("Fleetio's vehicle list wasn't in the expected format")
            out += [v for v in map(vehicle, records) if v is not None]
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor:
                break
        return out

    async def vehicle(self, vehicle_id: str) -> Vehicle | None:
        return vehicle(await self._get(f"/vehicles/{int(vehicle_id)}"))
